from uuid import uuid4

from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from collective.api.dto import ReleaseResult
from collective.api.errors import DomainError
from collective.domain.approvals import bound_terms
from collective.repositories import pools
from collective.repositories.events import Event, append_event
from collective.repositories.mutations import previous_mutation, save_mutation
from collective.repositories.orders import own_order
from collective.services import needs, solo
from collective.services.pool_commands import ACTIVE, latest_bindings, lock_pool, recalculate


async def issue_order(connection, release_id, member, approval, evaluation_revision, as_of):
    terms = bound_terms(member)
    order_id = uuid4()
    snapshot = {
        "schema_version": "1",
        "terms": terms.model_dump(mode="json"),
        "quote": member.quote.model_dump(mode="json"),
        "cost": member.cost.model_dump(mode="json"),
        "evaluation_revision": evaluation_revision,
        "evaluated_at": as_of.isoformat(),
    }
    await connection.execute(
        """insert into collective.purchase_orders
        (id,business_id,release_id,need_id,approval_id,supplier_id,spec_id,spec_revision,
         quantity,amount,decision_amount,snapshot,issued_at)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            order_id,
            member.business_id,
            release_id,
            member.need_id,
            approval["id"],
            member.quote.supplier_id,
            member.need.spec_id,
            member.need.spec_revision,
            member.need.quantity,
            member.cost.agreed_cost,
            member.decision_cost,
            Jsonb(snapshot),
            as_of,
        ),
    )
    await append_event(
        connection,
        Event(
            business_id=member.business_id,
            actor_id=None,
            actor_kind="system",
            event_type="po_created",
            entity_id=order_id,
            entity_revision=1,
            correlation_id=release_id,
            summary="A simulated purchase order was created from your current bound approval.",
        ),
    )


async def issue_pool(connection, pool, evaluation, approvals, subject, business, as_of):
    release_id = uuid4()
    await connection.execute(
        """insert into collective.releases
        (id,pool_id,evaluation_revision,snapshot,created_at) values (%s,%s,%s,%s,%s)""",
        (
            release_id,
            pool["id"],
            pool["revision"],
            Jsonb(evaluation.model_dump(mode="json")),
            as_of,
        ),
    )
    for member in evaluation.members:
        await issue_order(
            connection, release_id, member, approvals[member.need_id], pool["revision"], as_of
        )
    final_revision = pool["revision"] + 1
    await connection.execute(
        "update collective.pools set state='released',revision=revision+1 where id=%s",
        (pool["id"],),
    )
    await connection.execute(
        "insert into collective.pool_evaluations values (%s,%s,%s,%s)",
        (
            pool["id"],
            final_revision,
            as_of,
            Jsonb(
                {
                    "schema_version": "2",
                    "evaluation": evaluation.model_dump(mode="json"),
                    "release_id": str(release_id),
                    "released_at": as_of.isoformat(),
                }
            ),
        ),
    )
    active = {m.need_id for m in evaluation.members}
    members = await (
        await connection.execute(
            "select * from collective.pool_members where pool_id=%s order by need_id", (pool["id"],)
        )
    ).fetchall()
    for member in members:
        offer = await pools.own_offer(connection, member["business_id"], pool["id"], as_of)
        if member["need_id"] in active:
            await connection.execute(
                """update collective.bound_approvals
                set state='released',latest_evaluation_revision=%s,revision=revision+1
                where id=%s and state='valid'""",
                (final_revision, approvals[member["need_id"]]["id"]),
            )
            await connection.execute(
                """update collective.reorder_needs set state='released',revision=revision+1
                where business_id=%s and id=%s""",
                (member["business_id"], member["need_id"]),
            )
            await connection.execute(
                """update collective.pool_members set state='released'
                where pool_id=%s and business_id=%s""",
                (pool["id"], member["business_id"]),
            )
            own = await needs.load_need(connection, member["business_id"], member["need_id"])
            offer = offer.model_copy(
                update={
                    "need_revision": own["revision"],
                    "approval_state": "released",
                    "member_state": "released",
                    "approval_reason": "RELEASED",
                    "release_state": "released",
                }
            )
        offer = offer.model_copy(
            update={"evaluation_revision": final_revision, "evaluated_at": as_of}
        )
        await connection.execute(
            "insert into collective.member_evaluations values (%s,%s,%s,%s)",
            (
                pool["id"],
                member["business_id"],
                final_revision,
                Jsonb(offer.model_dump(mode="json")),
            ),
        )
    await append_event(
        connection,
        Event(
            business_id=business,
            actor_id=subject,
            actor_kind="owner",
            event_type="release_executed",
            entity_id=release_id,
            entity_revision=1,
            correlation_id=release_id,
            summary="Owner executed the approved simulated order release.",
        ),
    )
    pool["state"], pool["revision"] = "released", final_revision


async def release_pool(connection, subject, business, pool_id, request, key, clock):
    pool, own = await lock_pool(connection, subject, business, pool_id)
    route, payload = f"pools/{pool_id}/release", request.model_dump(mode="json")
    old = await previous_mutation(connection, subject, business, route, key, payload)
    if old:
        return ReleaseResult.model_validate(old)
    if pool["state"] != "released":
        if pool["revision"] != request.expected_revision:
            raise DomainError(
                "STALE_REVISION", "This offer changed. Refresh before execution.", 409
            )
        as_of = clock.now()
        evaluation, _ = await recalculate(connection, pool, as_of, force=True)
        member = await (
            await connection.execute(
                "select state from collective.pool_members where pool_id=%s and business_id=%s",
                (pool_id, business),
            )
        ).fetchone()
        if pool["state"] == "ready" and member["state"] in ACTIVE:
            approvals = await latest_bindings(connection, pool_id)
            try:
                async with connection.transaction():
                    await issue_pool(
                        connection, pool, evaluation, approvals, subject, business, as_of
                    )
                    await connection.execute("set constraints all immediate")
            except CheckViolation as exc:
                # A deadline can pass during inserts; roll back outputs and retain invalidation.
                if exc.diag.message_primary not in {
                    "Current bound approval required for PO",
                    "All current pool approvals required",
                    "Invalid release approvals",
                }:
                    raise
                pool = await (
                    await connection.execute(
                        "select * from collective.pools where id=%s", (pool_id,)
                    )
                ).fetchone()
                _, changed = await recalculate(connection, pool, clock.now())
                if not changed:
                    raise
    as_of = clock.now()
    order = await own_order(connection, business, own["id"])
    offer = await pools.own_offer(connection, business, pool_id, as_of)
    state = (
        "released"
        if order
        else (
            pool["state"]
            if pool["state"] in {"ready", "awaiting_approvals"} and offer.member_state in ACTIVE
            else "ineligible"
        )
    )
    result = ReleaseResult(
        state=state,
        need=needs.view(await needs.load_need(connection, business, own["id"])),
        offer=offer,
        order=order,
        failure_codes=offer.failure_codes,
    )
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result


async def release_solo(connection, subject, business, need_id, request, key, clock):
    row = await solo.lock_need(connection, subject, business, need_id)
    route, payload = f"needs/{need_id}/release", request.model_dump(mode="json")
    old = await previous_mutation(connection, subject, business, route, key, payload)
    if old:
        return ReleaseResult.model_validate(old)
    order = await own_order(connection, business, need_id)
    evaluation = None
    if order is None:
        if row["state"] != "selected" or row["reserved_pool_id"]:
            raise DomainError(
                "NEED_COMMITTED", "This need is not available for solo execution.", 409
            )
        approval = await solo.latest_binding(connection, business, need_id)
        if not approval or approval["pool_id"] or approval["id"] != request.approval_id:
            raise DomainError(
                "STALE_REVISION", "Review the current solo approval before execution.", 409
            )
        if row["revision"] != request.expected_revision:
            raise DomainError("STALE_REVISION", "This need changed. Refresh before execution.", 409)
        as_of = clock.now()
        evaluation = await solo.evaluate(connection, row, approval["quote_id"], as_of)
        await solo.revalidate(connection, approval, evaluation, as_of)
        if approval["state"] == "valid":
            try:
                async with connection.transaction():
                    release_id = uuid4()
                    await connection.execute(
                        """insert into collective.releases
                        (id,business_id,need_id,evaluation_revision,snapshot,created_at)
                        values (%s,%s,%s,%s,%s,%s)""",
                        (
                            release_id,
                            business,
                            need_id,
                            row["revision"],
                            Jsonb(evaluation.model_dump(mode="json")),
                            as_of,
                        ),
                    )
                    await issue_order(
                        connection, release_id, evaluation, approval, row["revision"], as_of
                    )
                    await connection.execute(
                        """update collective.bound_approvals
                        set state='released',revision=revision+1 where id=%s and state='valid'""",
                        (approval["id"],),
                    )
                    await connection.execute(
                        """update collective.reorder_needs set state='released',revision=revision+1
                        where business_id=%s and id=%s""",
                        (business, need_id),
                    )
                    await append_event(
                        connection,
                        Event(
                            business_id=business,
                            actor_id=subject,
                            actor_kind="owner",
                            event_type="release_executed",
                            entity_id=release_id,
                            entity_revision=1,
                            correlation_id=release_id,
                            summary="Owner executed the approved simulated solo order.",
                        ),
                    )
                    await connection.execute("set constraints all immediate")
            except CheckViolation as exc:
                if exc.diag.message_primary not in {
                    "Current bound approval required for PO",
                    "Invalid release approvals",
                }:
                    raise
                as_of = clock.now()
                evaluation = await solo.evaluate(connection, row, approval["quote_id"], as_of)
                _, changed = await solo.revalidate(connection, approval, evaluation, as_of)
                if not changed:
                    raise
        order = await own_order(connection, business, need_id)
    result = ReleaseResult(
        state="released"
        if order
        else "awaiting_approvals"
        if evaluation and evaluation.eligible
        else "ineligible",
        need=needs.view(await needs.load_need(connection, business, need_id)),
        order=order,
        failure_codes=evaluation.failure_codes if evaluation else (),
    )
    await save_mutation(
        connection, subject, business, route, key, payload, result.model_dump(mode="json")
    )
    return result
