from uuid import uuid4

from pydantic import ValidationError

from collective.api.dto import ApprovalCommandResult
from collective.api.errors import DomainError
from collective.domain.approvals import ApprovalError, bind_approval, evaluate_approval
from collective.domain.pools import MemberSnapshot, evaluate_solo
from collective.repositories import pools
from collective.repositories.approvals import insert_binding
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.ledger import historical_basis
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.repositories.quotes import confirmed_quotes
from collective.services import needs
from collective.services.permissions import membership
from collective.services.pool_commands import binding, lifecycle_event


async def lock_need(connection, subject, business, need_id):
    await membership(connection, subject, business, owner=True)
    row = await needs.load_need(connection, business, need_id)
    await set_context(connection, "coordinator", subject)
    await pools.lock_inputs(connection, row["spec_id"])
    await connection.execute(
        """select id from collective.bound_approvals
        where business_id=%s and need_id=%s order by id for update""",
        (business, need_id),
    )
    await membership(connection, subject, business, owner=True)
    return await needs.load_need(connection, business, need_id)


async def latest_binding(connection, business, need_id):
    return await (
        await connection.execute(
            """select * from collective.bound_approvals where business_id=%s and need_id=%s
        order by approved_at desc,id desc limit 1""",
            (business, need_id),
        )
    ).fetchone()


async def evaluate(connection, row, quote_id, as_of):
    records = await confirmed_quotes(connection, row["business_id"], row["spec_id"])
    quote = next((r.quote for r in records if r.row["id"] == quote_id), None)
    if quote is None:
        return None
    member = evaluate_solo(
        MemberSnapshot(
            need=needs.snapshot(row),
            quote=quote,
            solo_quotes=tuple(r.quote for r in records if r.quote),
            history=await historical_basis(connection, needs.snapshot(row)),
        ),
        as_of,
    )
    try:
        checked = await needs.options(connection, row, as_of)
        failures = checked.failure_codes if not checked.stated_quantity_valid else ()
    except (DomainError, ValidationError):
        failures = ("CURRENT_INPUTS_UNAVAILABLE",)
    if not await pools.supplier_mapping_confirmed(
        connection, row["business_id"], quote.supplier_id, quote.spec_id, quote.spec_revision
    ):
        failures = (*failures, "UNCONFIRMED_SUPPLIER_SPEC")
    return member.model_copy(
        update={"failure_codes": tuple(dict.fromkeys((*member.failure_codes, *failures)))}
    )


async def revalidate(connection, approval, evaluation, as_of):
    if approval["state"] != "valid":
        return "APPROVAL_NOT_VALID", False
    if as_of >= approval["expires_at"]:
        state, reason = "expired", "APPROVAL_EXPIRED"
    elif evaluation is None:
        state, reason = "void", "CURRENT_INPUTS_UNAVAILABLE"
    else:
        result = evaluate_approval(binding(approval), evaluation, as_of)
        state, reason = result.state, result.reason
    if state == "valid":
        return reason, False
    await connection.execute(
        "update collective.bound_approvals set state=%s,revision=revision+1 where id=%s",
        (state, approval["id"]),
    )
    approval["state"], approval["revision"] = state, approval["revision"] + 1
    await lifecycle_event(connection, approval["id"], approval, state, reason, approval["revision"])
    return reason, True


async def approve(connection, subject, business, request, key, clock):
    row = await lock_need(connection, subject, business, request.need_id)
    route, payload = "approvals", request.model_dump(mode="json", exclude_none=True)
    old = await previous_mutation(connection, subject, business, route, key, payload)
    if old:
        return ApprovalCommandResult.model_validate(old)
    if row["state"] != "selected" or row["reserved_pool_id"]:
        raise DomainError("NEED_COMMITTED", "Select an uncommitted quantity before approving.", 409)
    as_of = clock.now()
    previous = await latest_binding(connection, business, row["id"])
    changed = False
    if previous and previous["state"] == "valid":
        evaluation = await evaluate(connection, row, previous["quote_id"], as_of)
        reason, changed = await revalidate(connection, previous, evaluation, as_of)
        if not changed:
            raise DomainError("ALREADY_APPROVED", "Your existing approval still stands.", 409)
    if changed:
        result = ApprovalCommandResult(
            status="refresh_required",
            need=needs.view(await needs.load_need(connection, business, row["id"])),
            approval_id=previous["id"],
            failure_codes=(reason,),
        )
    else:
        evaluation = await evaluate(connection, row, request.quote_id, as_of)
        if row["revision"] != request.expected_evaluation_revision or (
            evaluation
            and (
                evaluation.quote.revision != request.expected_quote_revision
                or evaluation.decision_cost != request.reviewed_decision_cost
            )
        ):
            raise DomainError(
                "STALE_REVISION", "This quote or need changed. Refresh and review.", 409
            )
        if evaluation is None or not evaluation.eligible:
            raise DomainError("INELIGIBLE_OFFER", "This solo offer needs current confirmed inputs.")
        try:
            approved = bind_approval(
                evaluation,
                approval_id=uuid4(),
                owner_id=subject,
                cap=request.cap,
                expires_at=request.expires_at,
                as_of=as_of,
                pool_id=None,
                evaluation_revision=row["revision"],
            )
        except ApprovalError as exc:
            raise DomainError(exc.code, "Review the cap, quote and approval expiry.") from exc
        await insert_binding(connection, approved, approved.id)
        result = ApprovalCommandResult(
            status="approved",
            need=needs.view(await needs.load_need(connection, business, row["id"])),
            approval_id=approved.id,
        )
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result


async def withdraw(connection, subject, business, need_id, request, key, clock):
    row = await lock_need(connection, subject, business, need_id)
    route, payload = f"needs/{need_id}/withdraw", request.model_dump(mode="json")
    old = await previous_mutation(connection, subject, business, route, key, payload)
    if old:
        return ApprovalCommandResult.model_validate(old)
    approval = await latest_binding(connection, business, need_id)
    if row["state"] == "released":
        raise DomainError("ALREADY_RELEASED", "This order has already been issued.", 409)
    if row["reserved_pool_id"] or not approval or approval["pool_id"]:
        raise DomainError("NEED_COMMITTED", "Use the current pool's withdrawal command.", 409)
    if row["revision"] != request.expected_revision or approval["id"] != request.approval_id:
        raise DomainError("STALE_REVISION", "This approval changed. Refresh and review.", 409)
    as_of = clock.now()
    _, changed = await revalidate(
        connection,
        approval,
        await evaluate(connection, row, approval["quote_id"], as_of),
        as_of,
    )
    if approval["state"] == "valid":
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
                event_type="approval_withdrawn",
                entity_id=approval["id"],
                entity_revision=approval["revision"] + 1,
                correlation_id=approval["id"],
                summary="Owner withdrew their solo purchasing approval.",
            ),
        )
    result = ApprovalCommandResult(
        status="refresh_required" if changed else "withdrawn",
        need=needs.view(await needs.load_need(connection, business, need_id)),
        approval_id=approval["id"],
    )
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
