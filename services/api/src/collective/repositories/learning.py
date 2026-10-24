"""Read only confirmed ledger evidence; never pass network source rows to an API."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from collective.domain.invoices import InvoiceCheckResult
from collective.domain.learning import (
    AccuracyContext,
    AccuracyEvidence,
    EvidenceReference,
    FeeContext,
    FeeEvidence,
    StockEvidence,
    UsageContext,
    accuracy_proposal,
    fee_proposal,
    usage_proposal,
)

MALAYSIA = ZoneInfo("Asia/Kuala_Lumpur")


async def collect(connection, scope, identity, as_of):
    businesses = await (
        await connection.execute(
            "select id from collective.businesses where "
            + ("id=%s" if scope == "private" else "network_id=%s")
            + " order by id",
            (identity,),
        )
    ).fetchall()
    ids = [b["id"] for b in businesses]
    stocks = await (
        await connection.execute(
            """select o.*,r.base_unit from collective.stock_observations o
        join collective.spec_revisions r on (r.spec_id,r.revision)=(o.spec_id,o.spec_revision)
        where o.business_id=any(%s) and o.source_id is not null and o.created_at<=%s
        and o.observed_at<=%s""",
            (ids, as_of, as_of),
        )
    ).fetchall()
    purchases = await (
        await connection.execute(
            """select p.*,s.sha256 as source_sha256 from collective.purchases p
        join collective.sources s on (s.business_id,s.id)=(p.business_id,p.source_id)
        where p.business_id=any(%s)
        and p.sealed and p.source_id is not null and p.created_at<=%s
        and p.document_date is not null and not exists(select 1 from collective.purchases n
          where (n.business_id,n.id)=(p.business_id,p.id) and n.sealed and n.revision>p.revision
          and n.created_at<=%s) order by p.created_at desc,p.id""",
            (ids, as_of, as_of),
        )
    ).fetchall()
    records = {"usage": [], "observed_fee": [], "quote_accuracy": []}
    references = {}
    for row in stocks:
        if (
            scope == "network"
            and row["observed_at"].astimezone(MALAYSIA).date() >= as_of.astimezone(MALAYSIA).date()
        ):
            continue
        ref = EvidenceReference(
            business_id=row["business_id"], source_id=row["source_id"], record_id=row["id"]
        )
        records["usage"].append(
            StockEvidence(
                reference=ref,
                context=UsageContext(
                    spec_id=row["spec_id"],
                    spec_revision=row["spec_revision"],
                    base_unit=row["base_unit"],
                ),
                quantity=row["quantity"],
                at=row["observed_at"],
                kind=row["kind"],
                confirmed=True,
            )
        )
        references[ref] = (None, None)
    seen_sources, seen_invoices = set(), set()
    for row in purchases:
        at = datetime.combine(row["document_date"], time.min, tzinfo=MALAYSIA)
        # Historical aggregates do not include today's transactions or any current quote.
        if at > as_of or (scope == "network" and at.date() >= as_of.astimezone(MALAYSIA).date()):
            continue
        source_key = (row["business_id"], row["source_sha256"])
        invoice_key = (
            row["business_id"],
            row["supplier_id"],
            row["document_number"],
            row["document_date"],
        )
        if source_key in seen_sources or (row["document_number"] and invoice_key in seen_invoices):
            continue
        seen_sources.add(source_key)
        if row["document_number"]:
            seen_invoices.add(invoice_key)
        ref = EvidenceReference(
            business_id=row["business_id"],
            source_id=row["source_id"],
            record_id=row["id"],
            revision=row["revision"],
        )
        references[ref] = (row["id"], row["revision"])
        checks = await (
            await connection.execute(
                """select * from collective.invoice_checks where business_id=%s and purchase_id=%s
            and purchase_revision=%s and created_at<=%s and result<>'{}'::jsonb
            order by created_at,id""",
                (row["business_id"], row["id"], row["revision"], as_of),
            )
        ).fetchall()
        # A collection fee needs its explicitly matched, frozen collection location.
        latest_check = checks[-1] if checks else None
        order = (latest_check["input_snapshot"].get("order") or {}) if latest_check else {}
        if (
            not latest_check
            or not latest_check["po_id"]
            or (str(order.get("id")), str(order.get("business_id")))
            != (str(latest_check["po_id"]), str(row["business_id"]))
        ):
            order = {}
        terms = order.get("terms", {})
        location = (
            terms.get("delivery_location") if row["delivery_mode"] == "collection_point" else None
        )
        comparable_mode = row["delivery_mode"] == "per_business" or (
            row["delivery_mode"] == terms.get("delivery_mode") == "collection_point" and location
        )
        if row["delivery_zone"] and row["currency"] == "MYR" and comparable_mode:
            records["observed_fee"].append(
                FeeEvidence(
                    reference=ref,
                    context=FeeContext(
                        supplier_id=row["supplier_id"],
                        zone=row["delivery_zone"],
                        delivery_mode=row["delivery_mode"],
                        delivery_location=location,
                    ),
                    amount=row["delivery_fee"],
                    at=at,
                    confirmed=True,
                )
            )
        for check in checks:
            check_ref = ref.model_copy(update={"record_id": check["id"], "revision": 1})
            references[check_ref] = (row["id"], row["revision"])
            records["quote_accuracy"].append(
                AccuracyEvidence(
                    reference=check_ref,
                    context=AccuracyContext(supplier_id=row["supplier_id"]),
                    purchase_id=row["id"],
                    purchase_revision=row["revision"],
                    po_id=check["po_id"],
                    at=at,
                    checked_at=check["created_at"],
                    confirmed=True,
                    result=InvoiceCheckResult.model_validate(check["result"]),
                )
            )
    calculations = []
    for kind, propose in (
        ("usage", usage_proposal),
        ("observed_fee", fee_proposal),
        ("quote_accuracy", accuracy_proposal),
    ):
        for context in sorted(
            {r.context for r in records[kind]}, key=lambda c: c.model_dump_json()
        ):
            result = propose(records[kind], context, as_of)
            if result and (scope == "private" or result.eligible_network):
                calculations.append(result)
    return calculations, references
