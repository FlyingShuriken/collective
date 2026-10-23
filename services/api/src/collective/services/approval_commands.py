from uuid import uuid4

from psycopg.types.json import Jsonb

from collective.api.dto import ApprovalCommandResult, ApproveOrder
from collective.api.errors import DomainError
from collective.domain.approvals import ApprovalError, bind_approval
from collective.repositories import pools
from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.services import needs
from collective.services.pool_commands import latest_bindings, lock_pool, recalculate


async def approve(connection, subject, business, request: ApproveOrder, key, clock):
    pool, own = await lock_pool(connection, subject, business, request.pool_id)
    if own["id"] != request.need_id:
        raise DomainError("NOT_FOUND", "Resource not found.", 404)
    payload, route = request.model_dump(mode="json"), "approvals"
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
        await set_context(connection, "tenant", subject)
        await connection.execute(
            """insert into collective.bound_approvals
            (id,business_id,need_id,owner_id,pool_id,terms,cap,expires_at,approved_at,
             evaluation_revision,quote_id,quote_revision,state)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'valid')""",
            (
                binding.id,
                business,
                own["id"],
                subject,
                pool["id"],
                Jsonb(binding.terms.model_dump(mode="json")),
                binding.cap,
                binding.expires_at,
                binding.approved_at,
                binding.evaluation_revision,
                binding.quote_id,
                binding.quote_revision,
            ),
        )
        await append_event(
            connection,
            Event(
                business_id=business,
                actor_id=subject,
                actor_kind="owner",
                event_type="approval_bound",
                entity_id=binding.id,
                entity_revision=1,
                correlation_id=pool["id"],
                summary="Owner bound their cap and expiry to the reviewed purchasing terms.",
            ),
        )
        await set_context(connection, "coordinator", subject)
        await recalculate(connection, pool, as_of, force=True)
        offer = await pools.own_offer(connection, business, pool["id"], as_of)
        result = ApprovalCommandResult(
            status="approved",
            offer=offer,
            need=needs.view(own),
            approval_id=binding.id,
            failure_codes=offer.failure_codes,
        )
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
