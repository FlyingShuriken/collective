from functools import partial
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from collective.api.errors import DomainError
from collective.domain.types import Instant, Snapshot
from collective.repositories import jobs, pools
from collective.repositories.events import Event, append_event
from collective.services import needs, solo
from collective.services.pool_commands import recalculate


class DueInput(Snapshot):
    need_id: UUID
    reason: Literal["reorder", "approval", "pool", "window"]
    reference_id: UUID
    due_at: Instant


async def handle_due(database, job, clock):
    payload = DueInput.model_validate(job["payload"])
    async with database.transaction("coordinator", None) as connection:
        row = await needs.load_need(connection, job["business_id"], payload.need_id)
        await pools.lock_inputs(connection, row["spec_id"])
        row = await needs.load_need(connection, job["business_id"], payload.need_id)
        if row["reserved_pool_id"]:
            await connection.execute(
                "select id from collective.bound_approvals where pool_id=%s order by id for update",
                (row["reserved_pool_id"],),
            )
        else:
            await connection.execute(
                """select id from collective.bound_approvals where business_id=%s and need_id=%s
                order by id for update""",
                (job["business_id"], payload.need_id),
            )
        as_of = clock.now()
        await jobs.hold_claim(connection, job, as_of)
        if as_of < payload.due_at:
            raise jobs.NotDue(payload.due_at)
        state = "superseded"
        approval = await solo.latest_binding(connection, job["business_id"], payload.need_id)
        current = row["revision"] == job["input_revision"] and row["state"] not in {
            "released",
            "stale",
        }
        pool = None
        if row["reserved_pool_id"]:
            pool = await (
                await connection.execute(
                    "select * from collective.pools where id=%s", (row["reserved_pool_id"],)
                )
            ).fetchone()
        if current and payload.reason == "reorder":
            if (
                row["state"] in {"suggested", "selected"}
                and row["order_start"] <= as_of < row["order_end"]
                and payload.reference_id == row["id"]
                and payload.due_at == row["order_start"]
                and not (approval and approval["state"] == "valid")
            ):
                try:
                    checked = await needs.options(connection, row, as_of)
                    ready = checked.stated_quantity_valid and not checked.failure_codes
                except (DomainError, ValidationError):
                    ready = False
                state = "ready_for_review" if ready else "needs_review"
                await append_event(
                    connection,
                    Event(
                        business_id=job["business_id"],
                        actor_id=None,
                        actor_kind="system",
                        event_type="reorder_notice",
                        entity_id=payload.need_id,
                        entity_revision=row["revision"],
                        correlation_id=job["id"],
                        summary="Your reorder window is open. "
                        "Review the current quantity and confirmed terms.",
                    ),
                )
        elif current:
            reference_valid = (
                (
                    payload.reason == "approval"
                    and approval
                    and approval["id"] == payload.reference_id
                    and approval["expires_at"] == payload.due_at
                    and approval["state"] in {"valid", "void"}
                )
                or (
                    payload.reason == "pool"
                    and pool
                    and pool["id"] == payload.reference_id
                    and pool["deadline"] == payload.due_at
                    and pool["state"] != "released"
                )
                or (payload.reason == "window" and row["order_end"] == payload.due_at)
            )
            if reference_valid:
                if pool and pool["state"] != "released":
                    await recalculate(connection, pool, as_of)
                else:
                    if approval and approval["state"] == "valid":
                        await solo.revalidate(connection, approval, None, as_of)
                    if row["order_end"] <= as_of and row["state"] in {"suggested", "selected"}:
                        await connection.execute(
                            """update collective.reorder_needs set state='stale',revision=revision+1
                            where business_id=%s and id=%s""",
                            (job["business_id"], payload.need_id),
                        )
                state = "recalculated"
        await jobs.complete(
            connection,
            job,
            {
                "state": state,
                "need_id": str(payload.need_id),
                "input_revision": job["input_revision"],
                "due_at": payload.due_at.isoformat(),
                "evaluated_at": as_of.isoformat(),
                "lateness_seconds": max(0, int((as_of - payload.due_at).total_seconds())),
            },
            clock.now(),
        )


HANDLERS = {"reorder": handle_due, "expiry": handle_due}


def configured_handlers(settings):
    from collective.services.intake_jobs import handle as handle_intake
    from collective.services.invoice_checks import handle as handle_invoice
    from collective.services.liaison_jobs import handle_draft, handle_quote

    return HANDLERS | {
        "invoice_check": handle_invoice,
        "intake": partial(handle_intake, settings=settings),
        "quote_parse": partial(handle_quote, settings=settings),
        "draft": partial(handle_draft, settings=settings),
    }
