from datetime import datetime
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from collective.api.dto import (
    EvaluateNeed,
    NeedComparisons,
    NeedOptions,
    NeedView,
    OfferComparison,
    SelectNeed,
)
from collective.api.errors import DomainError
from collective.domain.baselines import comparable_quote, historical_cost, solo_cost
from collective.domain.costs import (
    CostBreakdown,
    DeliveryContext,
    applicable_fee_rule,
    calculate_cost,
)
from collective.domain.quantities import (
    QuantityConstraints,
    QuantityNeed,
    quantity_fits,
    suggest_quantities,
)
from collective.domain.replenishment import LeadTime, UsageInput, estimate_replenishment
from collective.domain.types import NeedSnapshot
from collective.repositories import rules
from collective.repositories.events import Event, append_event
from collective.repositories.ledger import current_stock, historical_basis
from collective.repositories.locks import lock_cohorts
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.repositories.quotes import confirmed_quotes


async def load_need(
    connection: AsyncConnection, business: UUID, need_id: UUID, *, lock=False
) -> dict:
    query = """select n.*,b.pool_opt_in,s.name,sr.perishable,a.approval_id,a.approval_state,
        coalesce(m.confirmed and m.spec_id=n.spec_id and m.spec_revision=n.spec_revision
          and not exists(select 1 from collective.sku_mappings newer
            where newer.business_id=m.business_id and newer.supplier_id=m.supplier_id
              and newer.supplier_sku=m.supplier_sku
              and newer.confirmed and newer.revision>m.revision),false) as mapping_confirmed
        from collective.reorder_needs n join collective.businesses b on b.id=n.business_id
        join collective.canonical_specs s on s.id=n.spec_id
        join collective.spec_revisions sr on (sr.spec_id,sr.revision)=(n.spec_id,n.spec_revision)
        left join collective.sku_mappings m
          on (m.business_id,m.id,m.revision)=(n.business_id,n.mapping_id,n.mapping_revision)
        left join lateral(select id as approval_id,state as approval_state
          from collective.bound_approvals where business_id=n.business_id and need_id=n.id
          order by approved_at desc,id desc limit 1) a on true
        where n.business_id=%s and n.id=%s"""
    if lock:
        query += " for update of n"
    row = await (await connection.execute(query, (business, need_id))).fetchone()
    if row is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    return row


def snapshot(row: dict) -> NeedSnapshot:
    fields = set(NeedSnapshot.model_fields) - {"order_window", "arrival_window"}
    return NeedSnapshot(
        **{k: row[k] for k in fields},
        order_window={"start": row["order_start"], "end": row["order_end"]},
        arrival_window={"start": row["arrival_start"], "end": row["arrival_end"]},
    )


def view(row: dict) -> NeedView:
    need = snapshot(row)
    return NeedView(
        **{k: getattr(need, k) for k in NeedView.model_fields if k in NeedSnapshot.model_fields},
        name=row["name"],
        approval_id=row.get("approval_id"),
        approval_state=row.get("approval_state"),
    )


async def options(
    connection: AsyncConnection, row: dict, as_of: datetime, *, pool_id: UUID | None = None
) -> NeedOptions:
    need = snapshot(row)
    stock = await current_stock(
        connection, need.business_id, need.spec_id, need.spec_revision, as_of
    )
    usage = await rules.usage_input(connection, row)
    failures = []
    if row["lead_days"] is None or row["buffer_days"] is None:
        raise DomainError(
            "MISSING_LEAD_TIME", "Confirm lead time and buffer before evaluating this need."
        )
    replenishment = estimate_replenishment(
        stock, usage, LeadTime(days=row["lead_days"], buffer_days=row["buffer_days"]), as_of
    )
    failures.extend(replenishment.failure_codes)
    if row["max_stock"] is None:
        failures.append("MISSING_STORAGE_CAPACITY")
    if not need.mapping_confirmed:
        failures.append("UNCONFIRMED_SPEC")
    if as_of >= need.order_window.end:
        failures.append("ORDER_WINDOW_EXPIRED")
    own_reservation = (
        need.state == "reserved" and pool_id is not None and need.reserved_pool_id == pool_id
    )
    if need.state in {"reserved", "released", "stale"} and not own_reservation:
        failures.append("NEED_COMMITTED" if need.state != "stale" else "STALE_NEED")
    candidates = []
    stated_valid = False
    if not failures:
        records = await confirmed_quotes(connection, need.business_id, need.spec_id)
        quotes = [r.quote for r in records if r.quote and comparable_quote(need, r.quote)]
        constraints = QuantityConstraints(
            max_stock=row["max_stock"],
            stock_at_arrival=replenishment.stock_at_arrival,
            usage_per_day=replenishment.daily_rate,
            perishable=row["perishable"],
            shelf_life_days=row["shelf_life_days"],
        )
        stated_valid = quantity_fits(need.quantity, need.pack_multiple, constraints)
        candidates = suggest_quantities(
            QuantityNeed(
                quantity=need.quantity,
                pack_multiple=need.pack_multiple,
                selected=need.state == "selected" or own_reservation,
            ),
            quotes,
            constraints,
            as_of,
            await rules.fee_rules(connection, need.business_id),
        )
        if not candidates:
            failures.append("NO_ELIGIBLE_QUOTE" if stated_valid else "QUANTITY_CONSTRAINT_FAILED")
    return NeedOptions(
        need=view(row),
        evaluated_at=as_of,
        replenishment=replenishment,
        options=tuple(candidates),
        stated_quantity_valid=stated_valid,
        failure_codes=tuple(failures),
    )


async def comparisons(connection: AsyncConnection, row: dict, as_of: datetime) -> NeedComparisons:
    need = snapshot(row)
    records = await confirmed_quotes(connection, need.business_id, need.spec_id)
    basis = await historical_basis(connection, need)
    fees = await rules.fee_rules(connection, need.business_id)
    offers = []
    for record in records:
        raw, quote = record.row, record.quote
        failures = [] if need.mapping_confirmed else ["UNCONFIRMED_SPEC"]
        if quote is None:
            cost = CostBreakdown(
                quote_id=raw["id"],
                quote_revision=raw["revision"],
                failure_codes=("MISSING_QUOTE_TERMS",),
            )
        elif not comparable_quote(need, quote):
            cost = CostBreakdown(
                quote_id=quote.id,
                quote_revision=quote.revision,
                failure_codes=("INCOMPARABLE_QUOTE",),
            )
        else:
            cost = calculate_cost(
                need.quantity,
                quote,
                DeliveryContext(
                    aggregate_quantity=need.quantity,
                    member_count=1,
                    as_of=as_of,
                    collection_location=need.delivery_location,
                    collection_shares=(quote.delivery_fee,)
                    if quote.delivery_fee is not None
                    else (),
                    own_collection_share=quote.delivery_fee,
                ),
                applicable_fee_rule(quote, fees),
            )
        failures.extend(cost.failure_codes)
        offers.append(
            OfferComparison(
                quote_id=raw["id"],
                quote_revision=raw["revision"],
                supplier_id=raw["supplier_id"],
                supplier_name=raw["supplier_name"],
                credit_days=raw["credit_days"],
                conditions=raw["conditions"],
                valid_until=raw["valid_until"],
                cost=cost,
                missing_fields=record.missing_fields,
                failure_codes=tuple(failures),
                accuracy_warnings=await rules.accuracy_warnings(
                    connection, need.business_id, raw["supplier_id"]
                ),
            )
        )
    solo = solo_cost(need, tuple(r.quote for r in records if r.quote), as_of, fees)
    return NeedComparisons(
        need=view(row),
        evaluated_at=as_of,
        historical_reference=basis.id if basis else None,
        historical_revision=basis.revision if basis else None,
        historical_comparable_cost=historical_cost(need, basis),
        best_solo_cost=solo.decision_cost if solo and need.mapping_confirmed else None,
        offers=tuple(offers),
        failure_codes=("NO_COMPARABLE_BASELINE",) if basis is None else (),
    )


async def context_locks(connection: AsyncConnection, business: UUID, spec: UUID) -> None:
    rows = await (
        await connection.execute(
            """select distinct supplier_id from collective.quote_revisions
            where business_id=%s and spec_id=%s""",
            (business, spec),
        )
    ).fetchall()
    await lock_cohorts(connection, {(r["supplier_id"], spec) for r in rows})


async def evaluate(
    connection: AsyncConnection,
    subject: UUID,
    business: UUID,
    request: EvaluateNeed,
    key: str,
    as_of: datetime,
) -> NeedOptions:
    mapping = await (
        await connection.execute(
            """select m.*,s.base_unit,s.pack_multiple,b.zone
        from collective.sku_mappings m join collective.spec_revisions s
          on (s.spec_id,s.revision)=(m.spec_id,m.spec_revision)
        join collective.businesses b on b.id=m.business_id
        where m.business_id=%s and m.id=%s and m.revision=%s and m.confirmed""",
            (business, request.mapping_id, request.mapping_revision),
        )
    ).fetchone()
    if mapping is None:
        raise DomainError("UNCONFIRMED_SPEC", "Select an owner-confirmed product mapping.")
    if request.quantity % mapping["pack_multiple"]:
        raise DomainError("INVALID_PACK_MULTIPLE", "Quantity must match the confirmed pack size.")
    await context_locks(connection, business, mapping["spec_id"])
    payload = request.model_dump(mode="json")
    old = await previous_mutation(connection, subject, business, "needs/evaluate", key, payload)
    if old:
        return NeedOptions.model_validate(old)
    if request.order_window.end <= as_of or request.arrival_window.start < as_of:
        raise DomainError("INVALID_WINDOW", "Use an open order window and future delivery window.")
    identity = uuid4()
    await connection.execute(
        """insert into collective.reorder_needs
        (id,business_id,spec_id,spec_revision,mapping_id,mapping_revision,quantity,base_unit,pack_multiple,zone,
         saving_threshold,min_credit_days,delivery_mode,delivery_location,collection_share,
         order_start,order_end,arrival_start,arrival_end,state,selected_by,usage_input,
         lead_days,buffer_days,max_stock,shelf_life_days,provenance)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                'suggested',%s,%s,%s,%s,%s,%s,'human')""",
        (
            identity,
            business,
            mapping["spec_id"],
            mapping["spec_revision"],
            request.mapping_id,
            request.mapping_revision,
            request.quantity,
            mapping["base_unit"],
            mapping["pack_multiple"],
            mapping["zone"],
            request.saving_threshold,
            request.min_credit_days,
            request.delivery_mode,
            request.delivery_location,
            request.collection_share,
            request.order_window.start,
            request.order_window.end,
            request.arrival_window.start,
            request.arrival_window.end,
            subject,
            Jsonb(UsageInput(stated_daily_rate=request.usage_per_day).model_dump(mode="json")),
            request.lead_days,
            request.buffer_days,
            request.max_stock,
            request.shelf_life_days,
        ),
    )
    result = await options(connection, await load_need(connection, business, identity), as_of)
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind="owner",
            event_type="need_evaluated",
            entity_id=identity,
            entity_revision=1,
            correlation_id=uuid4(),
            summary="Evaluated an owner-stated replenishment need.",
        ),
    )
    await save_mutation(
        connection,
        subject,
        business,
        "needs/evaluate",
        key,
        payload,
        result.model_dump(mode="json"),
    )
    return result


async def select(
    connection: AsyncConnection,
    subject: UUID,
    business: UUID,
    need_id: UUID,
    request: SelectNeed,
    key: str,
    as_of: datetime,
) -> NeedView:
    row = await load_need(connection, business, need_id)
    await context_locks(connection, business, row["spec_id"])
    route, payload = f"needs/{need_id}/select", request.model_dump(mode="json")
    old = await previous_mutation(connection, subject, business, route, key, payload)
    if old:
        return NeedView.model_validate(old)
    row = await load_need(connection, business, need_id)
    if row["revision"] != request.expected_revision:
        raise DomainError("STALE_REVISION", "This need changed. Refresh before selecting.", 409)
    approval = await (
        await connection.execute(
            """select 1 from collective.bound_approvals
            where business_id=%s and need_id=%s and state='valid'""",
            (business, need_id),
        )
    ).fetchone()
    if row["state"] in {"reserved", "released"} or row["reserved_pool_id"] is not None or approval:
        raise DomainError(
            "NEED_COMMITTED", "This need is already reserved, approved or released.", 409
        )
    row = await load_need(connection, business, need_id, lock=True)
    result = await options(connection, row, as_of)
    stated = result.stated_quantity_valid and request.quantity == row["quantity"]
    if not stated and request.quantity not in {option.quantity for option in result.options}:
        raise DomainError("INVALID_QUANTITY_OPTION", "Choose a currently valid quantity option.")
    stock = await current_stock(connection, business, row["spec_id"], row["spec_revision"], as_of)
    await connection.execute(
        """update collective.reorder_needs set quantity=%s,state='selected',selected_by=%s,
        stock_snapshot=%s,revision=revision+1 where business_id=%s and id=%s""",
        (
            request.quantity,
            subject,
            Jsonb(stock.model_dump(mode="json")) if stock else None,
            business,
            need_id,
        ),
    )
    selected = view(await load_need(connection, business, need_id))
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind="owner",
            event_type="need_selected",
            entity_id=need_id,
            entity_revision=selected.revision,
            correlation_id=uuid4(),
            summary="Owner selected a fixed replenishment quantity.",
        ),
    )
    await save_mutation(
        connection, subject, business, route, key, payload, selected.model_dump(mode="json")
    )
    return selected
