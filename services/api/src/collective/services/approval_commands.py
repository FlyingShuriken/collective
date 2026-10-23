from uuid import uuid4

from collective.api.dto import ApprovalCommandResult, ApproveOrder
from collective.api.errors import DomainError
from collective.domain.approvals import ApprovalError, bind_approval
from collective.repositories import pools
from collective.repositories.approvals import insert_binding
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services import needs, solo
from collective.services.pool_commands import latest_bindings, lock_pool, recalculate


async def approve(connection, subject, business, request: ApproveOrder, key, clock):
    if request.pool_id is None:
        return await solo.approve(connection, subject, business, request, key, clock)
    pool, own = await lock_pool(connection, subject, business, request.pool_id)
    if own["id"] != request.need_id:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    payload, route = request.model_dump(mode="json", exclude_none=True), "approvals"
    old = await previous_mutation(connection, subject, business, route, key, payload)
    if old:
        return ApprovalCommandResult.model_validate(old)
    if pool["state"] == "released":
        raise DomainError("ALREADY_RELEASED", "This pool has already been released.", 409)
    if pool["revision"] != request.expected_evaluation_revision:
        raise DomainError("STALE_REVISION", "This offer changed. Refresh before approving.", 409)
    as_of = clock.now()
    evaluation, changed = await recalculate(connection, pool, as_of)
    offer = await pools.own_offer(connection, business, pool["id"], as_of)
    if changed:
        # Ordinary invalidation is a committed result, not a rollback through an HTTP error.
        result = ApprovalCommandResult(
            status="refresh_required",
            offer=offer,
            need=needs.view(await needs.load_need(connection, business, own["id"])),
            approval_id=offer.approval_id,
            failure_codes=offer.failure_codes,
        )
    else:
        member = (
            next((m for m in evaluation.members if m.need_id == own["id"]), None)
            if evaluation
            else None
        )
        if not member or not evaluation.eligible:
            raise DomainError("INELIGIBLE_OFFER", "This offer needs review before approval.")
        existing = (await latest_bindings(connection, pool["id"])).get(own["id"])
        if existing and existing["state"] == "valid":
            raise DomainError("ALREADY_APPROVED", "Your existing approval still stands.", 409)
        try:
            binding = bind_approval(
                member,
                approval_id=uuid4(),
                owner_id=subject,
                cap=request.cap,
                expires_at=request.expires_at,
                as_of=as_of,
                pool_id=pool["id"],
                evaluation_revision=pool["revision"],
            )
        except ApprovalError as exc:
            raise DomainError(exc.code, "Review the cap, offer and approval expiry.") from exc
        await insert_binding(connection, binding, pool["id"])
        await recalculate(connection, pool, as_of, force=True)
        offer = await pools.own_offer(connection, business, pool["id"], as_of)
        result = ApprovalCommandResult(
            status="approved",
            offer=offer,
            need=needs.view(await needs.load_need(connection, business, own["id"])),
            approval_id=binding.id,
            failure_codes=offer.failure_codes,
        )
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
