from decimal import Decimal
from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from collective.api.dto import (
    HiddenInvoiceCheck,
    JobStatus,
    RequestInvoiceCheck,
    VisibleInvoiceCheck,
)
from collective.api.errors import DomainError
from collective.domain.invoices import (
    ConfirmedInvoice,
    InvoiceCheckResult,
    InvoiceLine,
    IssuedInvoiceOrder,
    check_invoice,
)
from collective.repositories import jobs, sources
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services.liaison_jobs import enqueue as enqueue_job
from collective.services.permissions import membership


async def reviewer(connection, subject, business, *, owner=False):
    access = await membership(connection, subject, business, owner=owner)
    if not access.can_view_prices:
        raise DomainError("SOURCE_RESTRICTED", "This account cannot review invoice prices.", 403)
    return access


async def purchase(connection, business, identity, revision):
    row = await (
        await connection.execute(
            """select * from collective.purchases where business_id=%s and id=%s
        and revision=%s and sealed and purpose='delivered'""",
            (business, identity, revision),
        )
    ).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Confirmed delivered invoice not found.", 404)
    return row


async def order(connection, business, identity):
    if identity is None:
        return None
    row = await (
        await connection.execute(
            "select * from collective.purchase_orders where business_id=%s and id=%s",
            (business, identity),
        )
    ).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Issued order not found.", 404)
    return row


async def enqueue(connection, business, body, key, as_of):
    await purchase(connection, business, body.purchase_id, body.purchase_revision)
    await order(connection, business, body.po_id)
    base = f"invoice:{business}:{body.purchase_id}:{body.purchase_revision}:{body.po_id}:invoice-v1"
    return await enqueue_job(
        connection,
        business,
        "invoice_check",
        body.purchase_revision,
        base,
        body.model_dump(mode="json") | {"version": "invoice-v1"},
        key,
        as_of,
    )


async def request(connection, subject, business, body, key, as_of):
    access = await reviewer(connection, subject, business)
    route, payload = "invoice-checks", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        return JobStatus.model_validate(prior)
    await purchase(connection, business, body.purchase_id, body.purchase_revision)
    await order(connection, business, body.po_id)
    await set_context(connection, "coordinator", subject)
    result = jobs.public_status(await enqueue(connection, business, body, key, as_of), as_of)
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind=access.role,
            event_type="invoice_check_requested",
            entity_id=result.id,
            entity_revision=1,
            correlation_id=result.id,
            summary="An invoice check was requested; discrepancy confirmation remains separate.",
        ),
    )
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result


async def handle(database, job, clock):
    if job["payload"].get("version") != "invoice-v1":
        raise DomainError("STALE_CHECK_VERSION", "Request the current invoice check.", 409)
    body = RequestInvoiceCheck.model_validate(
        {k: v for k, v in job["payload"].items() if k != "version"}
    )
    if body.purchase_revision != job["input_revision"]:
        raise DomainError("STALE_CHECK_VERSION", "Review the invoice input revision.", 409)
    business = job["business_id"]
    async with database.transaction("coordinator", None) as connection:
        await connection.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"invoice:{business}:{body.purchase_id}:{body.purchase_revision}:{body.po_id}",),
        )
        await jobs.hold_claim(connection, job, clock.now())
        existing = await (
            await connection.execute(
                """select id from collective.invoice_checks where business_id=%s and purchase_id=%s
            and purchase_revision=%s and po_id is not distinct from %s""",
                (business, body.purchase_id, body.purchase_revision, body.po_id),
            )
        ).fetchone()
        if existing:
            await jobs.complete(
                connection, job, {"invoice_check_id": str(existing["id"])}, clock.now()
            )
            return
        row = await purchase(connection, business, body.purchase_id, body.purchase_revision)
        po = await order(connection, business, body.po_id)
        lines = await (
            await connection.execute(
                """select l.*,m.confirmed as mapping_confirmed from collective.purchase_lines l
            left join collective.sku_mappings m on (m.business_id,m.id,m.revision)=
              (l.business_id,l.mapping_id,l.mapping_revision)
            where l.business_id=%s and l.purchase_id=%s and l.purchase_revision=%s
            order by l.line""",
                (business, body.purchase_id, body.purchase_revision),
            )
        ).fetchall()
        tax, extras = None, row["fees"]
        if extras and extras[0].get("label") == "tax":
            tax, extras = extras[0].get("amount"), extras[1:]
        invoice = ConfirmedInvoice(
            **{
                k: row[k]
                for k in (
                    "id",
                    "business_id",
                    "revision",
                    "supplier_id",
                    "purpose",
                    "currency",
                    "goods_total",
                    "delivery_fee",
                    "discount",
                    "surcharge",
                    "total",
                )
            },
            confirmed=True,
            tax_amount=tax,
            tax_rate=row["tax_rate"],
            additional_fees=extras,
            lines=tuple(
                InvoiceLine(
                    **{k: line[k] for k in InvoiceLine.model_fields if k != "mapping_confirmed"},
                    mapping_confirmed=bool(line["mapping_confirmed"]),
                )
                for line in lines
            ),
        )
        issued = None
        if po:
            try:
                issued = IssuedInvoiceOrder(
                    id=po["id"],
                    business_id=business,
                    amount=po["amount"],
                    terms=po["snapshot"]["terms"],
                    cost=po["snapshot"]["cost"],
                )
            except (ValidationError, KeyError):
                pass
        checked = check_invoice(invoice, issued)
        identity = uuid4()
        snapshot = {
            "invoice": invoice.model_dump(mode="json"),
            "order": issued.model_dump(mode="json") if issued else None,
            "source_id": str(row["source_id"]) if row["source_id"] else None,
            "tolerances": checked.tolerances.model_dump(mode="json"),
            "version": checked.version,
        }
        await connection.execute(
            """insert into collective.invoice_checks
            (id,business_id,purchase_id,purchase_revision,po_id,status,differences,result,input_snapshot)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                identity,
                business,
                row["id"],
                row["revision"],
                body.po_id,
                checked.status,
                Jsonb([d.model_dump(mode="json") for d in checked.differences]),
                Jsonb(checked.model_dump(mode="json")),
                Jsonb(snapshot),
            ),
        )
        await append_event(
            connection,
            Event(
                business_id=business,
                actor_id=None,
                actor_kind="system",
                event_type="invoice_checked",
                entity_id=identity,
                entity_revision=1,
                correlation_id=job["id"],
                summary={
                    "unmatched": "An invoice needs a usable issued order and confirmed items.",
                    "passed": "An invoice check found no variance above its alert thresholds.",
                    "flagged": "An invoice check found differences requiring human review.",
                }[checked.status],
            ),
        )
        from collective.services.rules import enqueue as enqueue_rules

        await enqueue_rules(connection, business, identity, 1, clock.now())
        await jobs.complete(connection, job, {"invoice_check_id": str(identity)}, clock.now())


async def load(connection, business, identity, *, lock=False):
    row = await (
        await connection.execute(
            """select c.*,p.supplier_id,p.source_id,p.document_number
            from collective.invoice_checks c
        join collective.purchases p on (p.business_id,p.id,p.revision)=
          (c.business_id,c.purchase_id,c.purchase_revision)
        where c.business_id=%s and c.id=%s"""
            + (" for update of c" if lock else ""),
            (business, identity),
        )
    ).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Invoice check not found.", 404)
    return row


def view(row, access):
    summary = {k: row[k] for k in HiddenInvoiceCheck.model_fields if k != "price_visibility"}
    if not access.can_view_prices:
        return HiddenInvoiceCheck(**summary)
    return VisibleInvoiceCheck(
        **summary,
        result=InvoiceCheckResult.model_validate(row["result"]) if row["result"] else None,
        **{
            k: row[k]
            for k in (
                "supplier_id",
                "source_id",
                "document_number",
                "confirmed_amount",
                "recovery_amount",
                "resolution",
            )
        },
    )


async def review(connection, subject, business, identity, body, key):
    await reviewer(connection, subject, business, owner=True)
    route, payload = f"invoice-checks/{identity}/review", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        return VisibleInvoiceCheck.model_validate(prior)
    row = await load(connection, business, identity, lock=True)
    if row["revision"] != body.expected_revision:
        raise DomainError("STALE_REVISION", "Review the current invoice check.", 409)
    if row["status"] != "flagged" or not row["result"]:
        raise DomainError("NO_REVIEWABLE_DISCREPANCY", "Review a matched, flagged invoice first.")
    amount, source_id, resolution = (
        row["recovery_amount"],
        row["recovery_source_id"],
        row["resolution"],
    )
    confirmed, state = row["confirmed_amount"], row["review_status"]
    if body.action in {"confirm", "reject"}:
        if state != "pending":
            raise DomainError("REVIEW_CLOSED", "This discrepancy review is already recorded.", 409)
        if body.action == "confirm":
            result = InvoiceCheckResult.model_validate(row["result"])
            maximum = max(Decimal(0), result.total_variance or Decimal(0))
            if body.confirmed_amount > maximum:
                raise DomainError(
                    "INVALID_DISCREPANCY_AMOUNT", "Do not exceed the known total overcharge."
                )
            confirmed, state = body.confirmed_amount, "confirmed"
        else:
            state = "rejected"
    else:
        if state != "confirmed" or confirmed is None:
            raise DomainError(
                "DISCREPANCY_NOT_CONFIRMED", "Confirm the discrepancy before recording recovery."
            )
        # shortcut: one documented resolution per check; extend for multiple partial credits.
        if amount is not None:
            raise DomainError(
                "RESOLUTION_CLOSED", "A documented resolution is already recorded.", 409
            )
        proof = body.resolution
        source = await sources.load(connection, business, proof.source_id, lock=True)
        if (
            source["status"] != "available"
            or source["preparation_status"] != "ready"
            or source["sha256"] != proof.source_sha256
            or source["id"] == row["source_id"]
        ):
            raise DomainError(
                "DOCUMENTED_RESOLUTION_REQUIRED", "Review a separate correction or credit source."
            )
        if proof.amount > confirmed:
            raise DomainError(
                "INVALID_RECOVERY_AMOUNT", "Recovery cannot exceed the confirmed discrepancy."
            )
        used = await (
            await connection.execute(
                """select id from collective.invoice_checks
            where business_id=%s and recovery_source_id=%s""",
                (business, proof.source_id),
            )
        ).fetchone()
        if used:
            raise DomainError(
                "RECOVERY_SOURCE_USED", "This evidence already supports a recorded recovery.", 409
            )
        amount, source_id, resolution = proof.amount, proof.source_id, proof.model_dump(mode="json")
    await connection.execute(
        """update collective.invoice_checks set review_status=%s,reviewed_by=%s,confirmed_amount=%s,
        recovery_amount=%s,recovery_source_id=%s,resolution=%s,revision=revision+1
        where business_id=%s and id=%s""",
        (
            state,
            subject,
            confirmed,
            amount,
            source_id,
            Jsonb(resolution) if resolution else None,
            business,
            identity,
        ),
    )
    row = await load(connection, business, identity)
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind="owner",
            event_type="invoice_" + body.action,
            entity_id=identity,
            entity_revision=row["revision"],
            correlation_id=identity,
            summary={
                "confirm": "Owner confirmed an invoice discrepancy; no recovery is recorded yet.",
                "reject": "Owner rejected a suspected invoice discrepancy.",
                "recover": "Owner recorded a separate documented correction or credit.",
            }[body.action],
        ),
    )
    result = view(row, await membership(connection, subject, business, owner=True))
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
