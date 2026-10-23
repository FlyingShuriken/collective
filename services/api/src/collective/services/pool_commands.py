from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from collective.api.dto import (
    ApprovalCommandResult,
    Mutation,
    PoolOffer,
    UnavailablePoolOffer,
)
from collective.api.errors import DomainError
from collective.domain.approvals import ApprovalBinding, bound_terms, evaluate_approval
from collective.domain.clock import Clock
from collective.domain.pools import MemberSnapshot, PoolEvaluation, evaluate_pool
from collective.repositories import pools
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.ledger import historical_basis
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.repositories.quotes import confirmed_quotes
from collective.services import needs
from collective.services.permissions import membership

ACTIVE = {"offered", "approved", "awaiting_reapproval"}


def binding(row: dict) -> ApprovalBinding:
    return ApprovalBinding.model_validate({key: row[key] for key in ApprovalBinding.model_fields})


async def lock_pool(connection: AsyncConnection, subject: UUID, business: UUID, pool: UUID):
    await membership(connection, subject, business, owner=True)
    member = await (
        await connection.execute(
            "select need_id from collective.pool_members where pool_id=%s and business_id=%s",
            (pool, business),
        )
    ).fetchone()
    if member is None:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    own = await needs.load_need(connection, business, member["need_id"])
    await set_context(connection, "coordinator", subject)
    await pools.lock_inputs(connection, own["spec_id"])
    await connection.execute(
        "select id from collective.bound_approvals where pool_id=%s order by id for update",
        (pool,),
    )
    await membership(connection, subject, business, owner=True)
    row = await (
        await connection.execute("select * from collective.pools where id=%s", (pool,))
    ).fetchone()
    return row, own


async def latest_bindings(connection: AsyncConnection, pool: UUID) -> dict[UUID, dict]:
    rows = await (
        await connection.execute(
            """select distinct on (need_id) * from collective.bound_approvals
            where pool_id=%s order by need_id,approved_at desc,id desc""",
            (pool,),
        )
    ).fetchall()
    return {row["need_id"]: row for row in rows}


async def lifecycle_event(connection, pool, row, state, reason, revision):
    await append_event(
        connection,
        Event(
            business_id=row["business_id"],
            actor_id=None,
            actor_kind="system",
            event_type="approval_stands" if state == "valid" else f"approval_{state}",
            entity_id=row["id"],
            entity_revision=revision,
            correlation_id=pool,
            summary=f"Bound approval {reason.lower().replace('_', ' ')}.",
        ),
    )


async def remove_member(connection, pool, member, state, as_of):
    await connection.execute(
        "update collective.pool_members set state=%s where pool_id=%s and business_id=%s",
        (state, pool, member["business_id"]),
    )
    await connection.execute(
        """update collective.reorder_needs set reserved_pool_id=null,
        state=case when order_end<=%s then 'stale' else 'selected' end,revision=revision+1
        where business_id=%s and id=%s and reserved_pool_id=%s""",
        (as_of, member["business_id"], member["need_id"], pool),
    )
    member["state"] = state


async def recalculate(
    connection: AsyncConnection, pool: dict, as_of: datetime, *, force=False
) -> tuple[PoolEvaluation | None, bool]:
    identity = pool["id"]
    stored = await (
        await connection.execute(
            "select snapshot from collective.pool_evaluations where pool_id=%s and revision=%s",
            (identity, pool["revision"]),
        )
    ).fetchone()
    origin = await (
        await connection.execute(
            "select snapshot from collective.pool_evaluations where pool_id=%s and revision=1",
            (identity,),
        )
    ).fetchone()
    origins = {UUID(m["need"]["id"]): m for m in origin["snapshot"]["members"]}
    members = await (
        await connection.execute(
            "select * from collective.pool_members where pool_id=%s order by need_id", (identity,)
        )
    ).fetchall()
    approvals = await latest_bindings(connection, identity)
    inputs, checks, missing, deadlines = [], {}, False, []
    for member in members:
        if member["state"] not in ACTIVE:
            continue
        row = await needs.load_need(connection, member["business_id"], member["need_id"])
        records = await confirmed_quotes(connection, row["business_id"], row["spec_id"])
        quote_id = UUID(origins[row["id"]]["quote"]["id"])
        record = next((r for r in records if r.row["id"] == quote_id), None)
        quote = record.quote if record else None
        valid_until = record.row["valid_until"] if record else None
        valid_until = valid_until or datetime.fromisoformat(
            origins[row["id"]]["quote"]["valid_until"]
        )
        deadline = min(row["order_end"], valid_until)
        approval = approvals.get(member["need_id"])
        # Expiry removes a reservation even when its old permission was already void.
        if as_of >= deadline or (approval and as_of >= approval["expires_at"]):
            await remove_member(connection, identity, member, "expired", as_of)
            if approval and approval["state"] == "valid":
                await connection.execute(
                    """update collective.bound_approvals set state='expired',revision=revision+1
                    where id=%s""",
                    (approval["id"],),
                )
                approval["state"] = "expired"
                approval["revision"] += 1
                await lifecycle_event(
                    connection,
                    identity,
                    approval,
                    "expired",
                    "APPROVAL_EXPIRED",
                    approval["revision"],
                )
            force = True
            continue
        deadlines.append(deadline)
        if quote is None:
            missing = True
            continue
        try:
            checked = await needs.options(connection, row, as_of, pool_id=identity)
            failures = checked.failure_codes if not checked.stated_quantity_valid else ()
        except (DomainError, ValidationError):
            failures = ("CURRENT_INPUTS_UNAVAILABLE",)
        if not await pools.supplier_mapping_confirmed(
            connection, row["business_id"], quote.supplier_id, quote.spec_id, quote.spec_revision
        ):
            failures = (*failures, "UNCONFIRMED_SUPPLIER_SPEC")
        checks[row["id"]] = failures
        inputs.append(
            MemberSnapshot(
                need=needs.snapshot(row),
                quote=quote,
                solo_quotes=tuple(r.quote for r in records if r.quote),
                history=await historical_basis(connection, needs.snapshot(row)),
            )
        )
    evaluation = None if missing else evaluate_pool(inputs, as_of, identity)
    if evaluation:
        evaluation = evaluation.model_copy(
            update={
                "members": tuple(
                    m.model_copy(
                        update={
                            "failure_codes": tuple(
                                dict.fromkeys((*m.failure_codes, *checks[m.need_id]))
                            )
                        }
                    )
                    for m in evaluation.members
                )
            }
        )
    evaluated = {m.need_id: m for m in evaluation.members} if evaluation else {}
    reasons = {}
    for member in members:
        approval = approvals.get(member["need_id"])
        if member["state"] not in ACTIVE or approval is None:
            continue
        if approval["state"] == "valid":
            current = evaluated.get(member["need_id"])
            result = evaluate_approval(binding(approval), current, as_of) if current else None
            state, reason = (
                (result.state, result.reason) if result else ("void", "CURRENT_INPUTS_UNAVAILABLE")
            )
            if state != "valid":
                await connection.execute(
                    """update collective.bound_approvals set state=%s,revision=revision+1
                    where id=%s""",
                    (state, approval["id"]),
                )
                approval["state"], approval["revision"] = state, approval["revision"] + 1
                await lifecycle_event(
                    connection, identity, approval, state, reason, approval["revision"]
                )
                force = True
            reasons[member["need_id"]] = reason
        else:
            previous = await (
                await connection.execute(
                    """select snapshot from collective.member_evaluations where pool_id=%s
                    and business_id=%s order by revision desc limit 1""",
                    (identity, member["business_id"]),
                )
            ).fetchone()
            reasons[member["need_id"]] = (
                previous["snapshot"].get("approval_reason") or "APPROVAL_NOT_VALID"
            )
        member["state"] = "approved" if approval["state"] == "valid" else "awaiting_reapproval"
    eligible = evaluation is not None and evaluation.eligible
    active = [m for m in members if m["state"] in ACTIVE]
    ready = eligible and all(m["state"] == "approved" for m in active)
    state = "ready" if ready else "awaiting_approvals" if eligible else "ineligible"
    economic = evaluation.model_dump(mode="json", exclude={"evaluated_at"}) if evaluation else None
    signature = {
        "evaluation": economic,
        "members": {str(m["need_id"]): m["state"] for m in members},
        "approvals": {str(k): str(v["id"]) + ":" + v["state"] for k, v in approvals.items()},
    }
    previous = stored["snapshot"]
    if "command_inputs" in previous:
        changed = previous["command_inputs"] != signature
    else:
        changed = economic != {
            k: previous[k] for k in PoolEvaluation.model_fields if k != "evaluated_at"
        }
    if not force and not changed:
        return evaluation, False
    revision = pool["revision"] + 1
    pool["deadline"] = min(deadlines, default=pool["deadline"])
    await connection.execute(
        "update collective.pools set state=%s,deadline=%s,revision=revision+1 where id=%s",
        (state, pool["deadline"], identity),
    )
    internal = {
        "schema_version": "2",
        "evaluation": evaluation.model_dump(mode="json") if evaluation else None,
        "command_inputs": signature,
    }
    await connection.execute(
        "insert into collective.pool_evaluations values (%s,%s,%s,%s)",
        (identity, revision, as_of, Jsonb(internal)),
    )
    deadline = min(
        [pool["deadline"]]
        + [approvals[m["need_id"]]["expires_at"] for m in active if m["need_id"] in approvals]
    )
    for member in members:
        approval = approvals.get(member["need_id"])
        own = await needs.load_need(connection, member["business_id"], member["need_id"])
        current = evaluated.get(member["need_id"])
        failures = list(current.failure_codes) if current else ["CURRENT_INPUTS_UNAVAILABLE"]
        if member["state"] not in ACTIVE:
            failures = ["MEMBER_" + member["state"].upper()]
        if approval and approval["state"] != "valid" and member["state"] in ACTIVE:
            failures.append(reasons.get(member["need_id"], "APPROVAL_NOT_VALID"))
        summary = dict(
            pool_id=identity,
            need_id=own["id"],
            need_revision=own["revision"],
            evaluation_revision=revision,
            evaluated_at=as_of,
            own_quantity=own["quantity"],
            base_unit=own["base_unit"],
            deadline=deadline,
            member_count=len(active),
            eligibility="eligible" if eligible and member["state"] in ACTIVE else "ineligible",
            failure_codes=tuple(dict.fromkeys(failures)),
            approval_id=approval["id"] if approval else None,
            approval_reason=reasons.get(member["need_id"]),
            approval_state=approval["state"] if approval else "not_approved",
            member_state=member["state"],
            release_state="ready"
            if ready
            else "waiting_for_approvals"
            if eligible
            else "ineligible",
        )
        if (
            current
            and member["state"] in ACTIVE
            and all(
                value is not None
                for value in (
                    current.cost.agreed_cost,
                    current.cost.expected_cost,
                    current.decision_cost,
                    current.historical_comparable_cost,
                    current.current_solo_comparable_cost,
                    current.historical_saving,
                    current.pooling_saving,
                    current.delivery_window,
                )
            )
        ):
            supplier = await (
                await connection.execute(
                    "select name from collective.suppliers where id=%s",
                    (current.quote.supplier_id,),
                )
            ).fetchone()
            offer = PoolOffer(
                **summary,
                supplier_id=current.quote.supplier_id,
                supplier_name=supplier["name"],
                quote_id=current.quote.id,
                quote_revision=current.quote.revision,
                delivery_window=current.delivery_window,
                credit_days=current.quote.credit_days,
                terms=bound_terms(current),
                conditions=current.quote.conditions,
                agreed_delivered_cost=current.cost.agreed_cost,
                expected_delivered_cost=current.cost.expected_cost,
                decision_delivered_cost=current.decision_cost,
                historical_comparable_cost=current.historical_comparable_cost,
                current_solo_comparable_cost=current.current_solo_comparable_cost,
                historical_saving=current.historical_saving,
                pooling_saving=current.pooling_saving,
            )
        else:
            offer = UnavailablePoolOffer(**(summary | {"eligibility": "unavailable"}))
        await connection.execute(
            "insert into collective.member_evaluations values (%s,%s,%s,%s)",
            (identity, member["business_id"], revision, Jsonb(offer.model_dump(mode="json"))),
        )
        await connection.execute(
            "update collective.pool_members set state=%s where pool_id=%s and business_id=%s",
            (member["state"], identity, member["business_id"]),
        )
        await append_event(
            connection,
            Event(
                business_id=member["business_id"],
                actor_id=None,
                actor_kind="system",
                event_type="pool_recalculated",
                entity_id=identity,
                entity_revision=revision,
                correlation_id=identity,
                summary="Your pooled option was recalculated from current confirmed inputs.",
            ),
        )
        if approval and member["state"] in ACTIVE:
            await connection.execute(
                """update collective.bound_approvals set latest_evaluation_revision=%s,
                revision=revision+1 where id=%s""",
                (revision, approval["id"]),
            )
            if approval["state"] == "valid":
                await lifecycle_event(
                    connection, identity, approval, "valid", "STANDS", approval["revision"] + 1
                )
    pool["revision"], pool["state"] = revision, state
    return evaluation, True


async def command(
    connection, subject, business, pool_id, request: Mutation, key, clock: Clock, *, withdraw=False
):
    pool, own = await lock_pool(connection, subject, business, pool_id)
    route = f"pools/{pool_id}/{'withdraw' if withdraw else 'reevaluate'}"
    payload = request.model_dump(mode="json")
    old = await previous_mutation(connection, subject, business, route, key, payload)
    if old:
        return ApprovalCommandResult.model_validate(old)
    if pool["state"] == "released":
        raise DomainError("ALREADY_RELEASED", "This pool has already been released.", 409)
    if pool["revision"] != request.expected_revision:
        raise DomainError("STALE_REVISION", "This offer changed. Refresh before continuing.", 409)
    as_of = clock.now()
    status = "recalculated"
    if withdraw:
        _, changed = await recalculate(connection, pool, as_of)
        member = await (
            await connection.execute(
                """select * from collective.pool_members
                where pool_id=%s and business_id=%s""",
                (pool_id, business),
            )
        ).fetchone()
        if member["state"] not in ACTIVE:
            if not changed:
                raise DomainError("MEMBER_NOT_ACTIVE", "This reservation is no longer active.", 409)
            status = "refresh_required"
        else:
            await remove_member(connection, pool_id, member, "withdrawn", as_of)
            approvals = await latest_bindings(connection, pool_id)
            approval = approvals.get(own["id"])
            if approval and approval["state"] == "valid":
                await connection.execute(
                    """update collective.bound_approvals set state='withdrawn',revision=revision+1
                    where id=%s""",
                    (approval["id"],),
                )
            await append_event(
                connection,
                Event(
                    business_id=business,
                    actor_id=subject,
                    actor_kind="owner",
                    event_type="pool_withdrawn",
                    entity_id=pool_id,
                    entity_revision=pool["revision"] + 1,
                    correlation_id=pool_id,
                    summary="Owner withdrew their reserved quantity from the pool.",
                ),
            )
            await recalculate(connection, pool, as_of, force=True)
            status = "withdrawn"
    else:
        await recalculate(connection, pool, as_of)
    offer = await pools.own_offer(connection, business, pool_id, as_of)
    result = ApprovalCommandResult(
        status=status,
        offer=offer,
        need=needs.view(await needs.load_need(connection, business, own["id"])),
        approval_id=offer.approval_id,
        failure_codes=offer.failure_codes,
    )
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
