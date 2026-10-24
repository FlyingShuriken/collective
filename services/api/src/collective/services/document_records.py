from decimal import Decimal, localcontext
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from collective.agents.schemas import IntakeReading
from collective.api.dto import DocumentProposal, ProposalSummary, RecordContext, RequestInvoiceCheck
from collective.api.errors import DomainError
from collective.domain.intake_checks import check_reading
from collective.domain.money import round_money
from collective.domain.types import NonNegative, whole_pieces
from collective.domain.units import PackMapping, SourceLine, canonical_unit, normalize_line
from collective.repositories import pools
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services import needs, proposals, solo
from collective.services.liaison_jobs import supplier
from collective.services.pool_commands import recalculate


async def mapping(connection, business, reference):
    row = await (
        await connection.execute(
            """select m.*,s.base_unit from collective.sku_mappings m
        join collective.spec_revisions s on (s.spec_id,s.revision)=(m.spec_id,m.spec_revision)
        where m.business_id=%s and m.id=%s and m.revision=%s and m.confirmed
        and not exists(select 1 from collective.sku_mappings n where n.business_id=m.business_id
          and n.supplier_id=m.supplier_id and n.supplier_sku=m.supplier_sku
          and n.confirmed and n.revision>m.revision)""",
            (business, reference.id, reference.revision),
        )
    ).fetchone()
    if row is None:
        raise DomainError("MAPPING_NOT_CURRENT", "Select a current confirmed item mapping.", 409)
    return row


async def mappings(connection, business, context):
    result = {}
    for selected in context.line_mappings:
        result[selected.line] = await mapping(connection, business, selected)
    stock = (
        await mapping(connection, business, context.stock_mapping)
        if context.stock_mapping
        else None
    )
    return result, stock


def normalized(line, selected, currency):
    if any(value is None for value in (line.quantity, line.unit, line.unit_price, currency)):
        return None
    try:
        return normalize_line(
            SourceLine(
                quantity=line.quantity,
                unit=canonical_unit(line.unit),
                base_unit=selected["base_unit"],
                unit_price=line.unit_price,
                currency=currency,
            ),
            PackMapping(
                base_unit=selected["base_unit"],
                factors=selected["pack_factors"],
                confirmed=True,
                source_unit=selected.get("source_unit"),
            ),
        )
    except ValidationError:
        return None


def stock_quantity(reading, selected):
    if reading.stock_quantity is None or reading.stock_unit is None or selected is None:
        return None
    # The zero price is a dimensional placeholder, never a recorded stock price.
    value = normalize_line(
        SourceLine(
            quantity="1",
            unit=canonical_unit(reading.stock_unit),
            unit_price="0",
            base_unit=selected["base_unit"],
            currency="MYR",
        ),
        PackMapping(
            base_unit=selected["base_unit"],
            factors=selected["pack_factors"],
            confirmed=True,
            source_unit=selected.get("source_unit"),
        ),
    )
    if value.quantity is None:
        return None
    with localcontext() as context:
        context.prec = 40
        quantity = TypeAdapter(NonNegative).validate_python(reading.stock_quantity * value.quantity)
    if selected["base_unit"] == "piece":
        whole_pieces(quantity)
    return quantity


async def stock_value(connection, business, selected, quantity, po_id=None):
    if quantity is None or selected is None:
        return None
    if po_id:
        row = await (
            await connection.execute(
                """select amount as total,quantity from collective.purchase_orders
            where business_id=%s and id=%s and spec_id=%s and spec_revision=%s""",
                (business, po_id, selected["spec_id"], selected["spec_revision"]),
            )
        ).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Issued order not found for this item.", 404)
    else:
        row = await (
            await connection.execute(
                """select p.total,l.quantity from collective.purchases p
            join collective.purchase_lines l on (l.business_id,l.purchase_id,l.purchase_revision)=
              (p.business_id,p.id,p.revision)
            where p.business_id=%s and p.sealed and p.currency='MYR' and p.total is not null
              and l.spec_id=%s and l.spec_revision=%s and l.quantity>0
              and coalesce(p.missing_fields->>'eligible_for_baseline','true')='true'
              and not exists(select 1 from collective.purchases n where n.business_id=p.business_id
                and n.id=p.id and n.revision>p.revision and n.sealed)
              and (select count(*) from collective.purchase_lines x where
                (x.business_id,x.purchase_id,x.purchase_revision)=(p.business_id,p.id,p.revision))=1
            order by p.document_date desc nulls last,p.created_at desc,p.id limit 1""",
                (business, selected["spec_id"], selected["spec_revision"]),
            )
        ).fetchone()
    if row is None:
        return None
    with localcontext() as precision:
        precision.prec = 40
        return round_money(quantity * row["total"] / row["quantity"])


def purchase_value(reading, checks):
    if reading.total is None:
        return None
    with localcontext() as context:
        context.prec = 40
        try:
            goods = sum(
                (
                    round_money(line.quantity * line.unit_price)
                    for line in reading.lines
                    if line.quantity is not None and line.unit_price is not None
                ),
                Decimal(0),
            )
            known = round_money(
                max(
                    Decimal(0),
                    goods
                    + (reading.delivery_fee or 0)
                    + (reading.tax_amount or 0)
                    + (reading.surcharge or 0)
                    - (reading.discount or 0)
                    + sum(
                        (fee.amount for fee in reading.additional_fees if fee.amount is not None),
                        Decimal(0),
                    ),
                )
            )
            return max(reading.total, checks.calculated_total or 0, known)
        except ValueError:
            return None


async def checked(connection, business, reading, context, *, strict=False):
    checks = check_reading(reading)
    flags = list(checks.flags)
    if reading.kind not in {"purchase", "stock"}:
        return checks, flags + ["record_kind:missing"], {}, None, None
    try:
        lines, stock = await mappings(connection, business, context)
    except DomainError:
        if strict:
            raise
        lines, stock = {}, None
        flags.append("mapping:stale")
    if reading.kind == "purchase":
        if any(index >= len(reading.lines) for index in lines):
            raise DomainError("INVALID_LINE_MAPPING", "Select a mapping for an existing line.")
        if context.supplier_id is None:
            flags.append("supplier:missing")
        if context.purpose is None:
            flags.append("purpose:missing")
        for index, line in enumerate(reading.lines):
            selected = lines.get(index)
            if selected is None:
                flags.append(f"lines.{index}.mapping:missing")
                continue
            if selected["supplier_id"] != context.supplier_id or (
                line.supplier_sku and line.supplier_sku != selected["supplier_sku"]
            ):
                if strict:
                    raise DomainError(
                        "MAPPING_CONTEXT_MISMATCH", "Review the supplier and line mapping."
                    )
                flags.append(f"lines.{index}.mapping:context_mismatch")
            result = normalized(line, selected, reading.currency)
            if result is None or result.quantity is None:
                flags.append(f"lines.{index}.normalization:incomplete")
        return checks, list(dict.fromkeys(flags)), lines, stock, purchase_value(reading, checks)
    if context.stock_mapping is None:
        flags.append("stock_mapping:missing")
    try:
        quantity = stock_quantity(reading, stock)
        value = await stock_value(connection, business, stock, quantity, context.po_id)
    except (ValidationError, ValueError):
        quantity, value = None, None
    if quantity is None:
        flags.append("stock_quantity:normalization_required")
    if value is None:
        flags.append("stock_value:unknown")
    return checks, list(dict.fromkeys(flags)), lines, stock, value


async def view(connection, row, revision, access):
    reading = IntakeReading.model_validate(revision["payload"]["reading"])
    context = RecordContext.model_validate(revision["payload"]["context"])
    checks, flags, _, _, value = await checked(connection, row["business_id"], reading, context)
    flags = list(dict.fromkeys(flags + revision["flags"]))
    table = "purchases" if row["kind"] == "purchase" else "stock_observations"
    columns = "id,revision" if table == "purchases" else "id"
    record = await (
        await connection.execute(
            sql.SQL(
                """select {} from collective.{} where business_id=%s
                and proposal_id=%s and proposal_revision=%s"""
            ).format(sql.SQL(columns), sql.Identifier(table)),
            (row["business_id"], row["id"], row["current_revision"]),
        )
    ).fetchone()
    return DocumentProposal(
        **{key: row[key] for key in ProposalSummary.model_fields},
        **revision["payload"],
        checks=checks,
        flags=flags,
        evidence=revision["evidence"],
        model=revision["model"],
        prompt_version=revision["prompt_version"],
        schema_version=revision["schema_version"],
        record_id=record["id"] if record else None,
        record_revision=record.get("revision") if record else None,
        review_value=value,
    )


async def edit(connection, subject, business, identity, body, key):
    access = await proposals.editable(connection, subject, business, identity)
    route, payload = f"proposals/{identity}/edit", body.model_dump(mode="json")
    previous = await previous_mutation(connection, subject, business, route, key, payload)
    if previous:
        return DocumentProposal.model_validate(previous)
    row, revision = await proposals.load(connection, business, identity, lock=True)
    proposals.require_pending(row, body.expected_revision)
    old = IntakeReading.model_validate(revision["payload"]["reading"])
    if old.evidence != body.reading.evidence:
        raise DomainError("EVIDENCE_IMMUTABLE", "Preserve the saved source evidence.")
    if body.reading.kind not in {"purchase", "stock"}:
        raise DomainError("RECORD_KIND_REQUIRED", "Choose purchase or stock for manual review.")
    context = body.context or RecordContext.model_validate(revision["payload"]["context"])
    if context.supplier_id:
        await supplier(connection, business, context.supplier_id)
    await mappings(connection, business, context)
    checks = check_reading(body.reading)
    flags = list(checks.flags) + (
        ["evidence:review"] if "evidence:review" in revision["flags"] else []
    )
    number = row["current_revision"] + 1
    await connection.execute(
        """insert into collective.proposal_revisions
        (business_id,proposal_id,revision,payload,evidence,flags,agent,model,prompt_version,
         schema_version,edited_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            business,
            identity,
            number,
            Jsonb(
                revision["payload"]
                | {
                    "reading": body.reading.model_dump(mode="json"),
                    "context": context.model_dump(mode="json"),
                }
            ),
            Jsonb(revision["evidence"]),
            Jsonb(flags),
            revision["agent"],
            revision["model"],
            revision["prompt_version"],
            revision["schema_version"],
            subject,
        ),
    )
    await connection.execute(
        """update collective.proposals set current_revision=%s,kind=%s
        where business_id=%s and id=%s""",
        (number, body.reading.kind, business, identity),
    )
    row, revision = await proposals.load(connection, business, identity)
    await record_event(connection, subject, access, row, "proposal_edited")
    result = await view(connection, row, revision, access)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result


async def record_event(connection, subject, access, row, kind):
    await append_event(
        connection,
        Event(
            business_id=row["business_id"],
            actor_id=subject,
            actor_kind=access.role,
            event_type=kind,
            entity_id=row["id"],
            entity_revision=row["current_revision"],
            correlation_id=row["id"],
            summary="A reviewer edited a source record proposal."
            if kind == "proposal_edited"
            else "A reviewer confirmed a source record; purchasing permission remains separate.",
        ),
    )


async def insert(connection, table, values):
    await connection.execute(
        sql.SQL("insert into collective.{} ({}) values ({})").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ),
        list(values.values()),
    )


async def purchase(connection, row, reading, context, lines, flags, subject):
    if (
        context.supplier_id is None
        or context.purpose is None
        or not reading.lines
        or any(not line.description for line in reading.lines)
    ):
        raise DomainError("MISSING_RECORD_FIELDS", "Review the supplier, purpose and line names.")
    await supplier(connection, row["business_id"], context.supplier_id)
    identity = uuid4()
    fields = (
        "document_number",
        "document_date",
        "currency",
        "goods_total",
        "delivery_fee",
        "tax_rate",
        "discount",
        "surcharge",
        "total",
        "credit_days",
        "delivery_mode",
    )
    values = {name: getattr(reading, name) for name in fields}
    business_context = await (
        await connection.execute(
            "select zone from collective.businesses where id=%s",
            (row["business_id"],),
        )
    ).fetchone()
    unresolved = set(flags) - {"reading:review", "fields:review", "evidence:review"}
    values |= {
        "business_id": row["business_id"],
        "id": identity,
        "revision": 1,
        "supplier_id": context.supplier_id,
        "purpose": context.purpose,
        "delivery_zone": business_context["zone"],
        "source_id": row["source_id"],
        "proposal_id": row["id"],
        "proposal_revision": row["current_revision"],
        "confirmed_by": subject,
        "provenance": "human",
        "missing_fields": Jsonb({"flags": flags, "eligible_for_baseline": not unresolved}),
        "fees": Jsonb(
            [
                {
                    "label": "tax",
                    "amount": str(reading.tax_amount) if reading.tax_amount is not None else None,
                },
                *(fee.model_dump(mode="json") for fee in reading.additional_fees),
            ]
        ),
    }
    await insert(connection, "purchases", values)
    for index, line in enumerate(reading.lines):
        selected = lines.get(index)
        converted = normalized(line, selected, reading.currency) if selected else None
        complete = converted is not None and converted.quantity is not None
        await insert(
            connection,
            "purchase_lines",
            {
                "business_id": row["business_id"],
                "purchase_id": identity,
                "purchase_revision": 1,
                "line": index,
                "source_name": line.description,
                "supplier_sku": line.supplier_sku,
                "original_quantity": line.quantity,
                "original_unit": line.unit,
                "original_unit_price": line.unit_price,
                "line_total": line.line_total,
                "quantity": converted.quantity if complete else None,
                "base_unit": converted.base_unit if complete else None,
                "unit_price": converted.unit_price if complete else None,
                "spec_id": selected["spec_id"] if complete else None,
                "spec_revision": selected["spec_revision"] if complete else None,
                "mapping_id": selected["id"] if selected else None,
                "mapping_revision": selected["revision"] if selected else None,
            },
        )
    await connection.execute(
        "update collective.purchases set sealed=true where business_id=%s and id=%s",
        (row["business_id"], identity),
    )
    return identity


async def stock(connection, row, reading, context, selected, value, subject, as_of):
    try:
        quantity = stock_quantity(reading, selected)
    except (ValidationError, ValueError):
        quantity = None
    if quantity is None or reading.stock_observed_at is None or not reading.stock_description:
        raise DomainError("MISSING_RECORD_FIELDS", "Review the item, quantity, unit and time.")
    if reading.stock_observed_at > as_of:
        raise DomainError("FUTURE_STOCK", "Observed stock cannot be dated in the future.")
    await insert(
        connection,
        "stock_observations",
        {
            "id": uuid4(),
            "business_id": row["business_id"],
            "spec_id": selected["spec_id"],
            "spec_revision": selected["spec_revision"],
            "quantity": quantity,
            "observed_at": reading.stock_observed_at,
            "kind": context.stock_kind,
            "review_value": value,
            "po_id": context.po_id,
            "source_id": row["source_id"],
            "proposal_id": row["id"],
            "proposal_revision": row["current_revision"],
            "confirmed_by": subject,
            "provenance": "human",
        },
    )


async def confirm(connection, subject, business, identity, body, key, clock):
    await proposals.editable(connection, subject, business, identity)
    if body.quote_id is not None:
        raise DomainError("INVALID_RECORD_CONTEXT", "Quote revisions do not apply to this record.")
    row, revision = await proposals.load(connection, business, identity)
    context = RecordContext.model_validate(revision["payload"]["context"])
    lines, selected = await mappings(connection, business, context)
    specs = {item["spec_id"] for item in lines.values()}
    if selected:
        specs.add(selected["spec_id"])
    locked_revision = row["current_revision"]
    await set_context(connection, "coordinator", subject)
    approvals = await pools.lock_record_inputs(connection, specs)
    await set_context(connection, "tenant", subject)
    access = await proposals.editable(connection, subject, business, identity)
    route, payload = f"proposals/{identity}/confirm", body.model_dump(mode="json")
    prior = await previous_mutation(connection, subject, business, route, key, payload)
    if prior:
        return DocumentProposal.model_validate(prior)
    row, revision = await proposals.load(connection, business, identity, lock=True)
    if (
        row["current_revision"] != locked_revision
        or row["current_revision"] != body.expected_revision
    ):
        raise DomainError("STALE_REVISION", "Review the latest proposal revision.", 409)
    if row["status"] != "confirmed":
        proposals.require_pending(row, body.expected_revision)
        reading = IntakeReading.model_validate(revision["payload"]["reading"])
        _, flags, lines, selected, value = await checked(
            connection, business, reading, context, strict=True
        )
        flags = list(dict.fromkeys(flags + revision["flags"]))
        if not set(flags) <= set(body.acknowledged_flags):
            raise DomainError("REVIEW_FLAGS", "Review and acknowledge the unresolved fields.")
        if reading.kind not in {"purchase", "stock"}:
            raise DomainError("RECORD_KIND_REQUIRED", "Choose purchase or stock for manual review.")
        financial_unknown = reading.kind == "purchase" and (
            reading.currency != "MYR"
            or any(
                getattr(reading, name) is None
                for name in (
                    "goods_total",
                    "delivery_fee",
                    "tax_amount",
                    "discount",
                    "surcharge",
                    "total",
                )
            )
            or any(fee.amount is None for fee in reading.additional_fees)
            or any(line.quantity is None or line.unit_price is None for line in reading.lines)
            or any(flag.endswith((":mismatch", ":overflow", ":negative")) for flag in flags)
        )
        allowed = await (
            await connection.execute(
                "select collective_private.can_confirm(%s,%s) as allowed", (business, value)
            )
        ).fetchone()
        if not allowed["allowed"] or (access.role != "owner" and financial_unknown):
            raise DomainError("OWNER_REVIEW_REQUIRED", "This record requires owner review.", 403)
        as_of = clock.now()
        if reading.kind == "purchase":
            record_id = await purchase(connection, row, reading, context, lines, flags, subject)
            if context.purpose == "delivered":
                from collective.services import invoice_checks

                await set_context(connection, "coordinator", subject)
                await invoice_checks.enqueue(
                    connection,
                    business,
                    RequestInvoiceCheck(
                        purchase_id=record_id,
                        purchase_revision=1,
                    ),
                    str(identity),
                    as_of,
                )
                await set_context(connection, "tenant", subject)
        else:
            await stock(connection, row, reading, context, selected, value, subject, as_of)
        await connection.execute(
            "update collective.proposals set status='confirmed' where business_id=%s and id=%s",
            (business, identity),
        )
        row["status"] = "confirmed"
        await record_event(connection, subject, access, row, "record_confirmed")
        await set_context(connection, "coordinator", subject)
        active = await (
            await connection.execute(
                """select * from collective.pools where spec_id=any(%s)
                and state<>'released' order by id""",
                (list(specs),),
            )
        ).fetchall()
        from collective.services.rules import enqueue as enqueue_rules

        await enqueue_rules(connection, business, row["id"], row["current_revision"], as_of)
        for pool in active:
            await recalculate(connection, pool, as_of)
        for approval in approvals:
            if approval["pool_id"] is None and approval["state"] == "valid":
                need = await needs.load_need(
                    connection, approval["business_id"], approval["need_id"]
                )
                evaluation = await solo.evaluate(connection, need, approval["quote_id"], as_of)
                await solo.revalidate(connection, approval, evaluation, as_of)
        await set_context(connection, "tenant", subject)
    result = await view(connection, row, revision, access)
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
