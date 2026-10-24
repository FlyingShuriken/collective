from datetime import timedelta
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from collective.api.dto import JobStatus
from collective.repositories.events import Event, append_event

MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=5)


def public_status(row, as_of):
    reference = None
    if row["kind"] in {"intake", "quote_parse", "draft", "invoice_check"} and isinstance(
        row["result"], dict
    ):
        value = (
            row["result"].get("proposal_id")
            or row["result"].get("draft_id")
            or row["result"].get("invoice_check_id")
        )
        if value:
            reference = UUID(value)
    return JobStatus(
        **{
            k: row[k]
            for k in JobStatus.model_fields
            if k in row and k not in {"lease_expired", "result_reference", "review_state"}
        },
        lease_expired=bool(row["status"] == "running" and row["lease_until"] <= as_of),
        result_reference=reference,
        review_state=row["result"]["state"]
        if row["kind"] == "intake"
        and isinstance(row["result"], dict)
        and row["result"].get("state") in {"pending_review", "needs_quote_context", "unreadable"}
        else None,
    )


async def record_attempts(connection, job, attempts, validated=None):
    for index, attempt in enumerate(attempts):
        await connection.execute(
            """insert into collective_private.model_attempts
            (id,business_id,job_id,attempt,validated)
            values (%s,%s,%s,%s,%s) on conflict(id) do nothing""",
            (
                UUID(attempt["id"]),
                job["business_id"],
                job["id"],
                Jsonb(attempt),
                Jsonb(validated) if validated is not None and index == len(attempts) - 1 else None,
            ),
        )


class LeaseLost(Exception):
    pass


class NotDue(Exception):
    def __init__(self, due):
        self.due = due


async def enqueue(connection, *, business, kind, revision, key, payload, due, waiting_review=False):
    row = await (
        await connection.execute(
            """insert into collective.jobs
        (id,business_id,kind,input_revision,dedup_key,payload,next_attempt_at,status)
        values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict(dedup_key) do nothing returning *""",
            (
                uuid4(),
                business,
                kind,
                revision,
                key,
                Jsonb(payload),
                due,
                "waiting_review" if waiting_review else "queued",
            ),
        )
    ).fetchone()
    if row is None:
        row = await (
            await connection.execute("select * from collective.jobs where dedup_key=%s", (key,))
        ).fetchone()
        if (row["business_id"], row["kind"], row["input_revision"], row["payload"]) != (
            business,
            kind,
            revision,
            payload,
        ):
            raise ValueError("Job key conflicts with existing inputs")
    return row


async def event(connection, job, status):
    if job["business_id"] is None:
        return
    summary = {
        "succeeded": "Background evaluation completed. Required owner review remains separate.",
        "queued": "Background processing will retry; no purchasing permission was granted.",
        "failed": "Background processing needs review; no purchasing permission was granted.",
    }[status]
    await append_event(
        connection,
        Event(
            business_id=job["business_id"],
            actor_id=None,
            actor_kind="system",
            event_type="job_" + status,
            entity_id=job["id"],
            entity_revision=job["input_revision"],
            correlation_id=job["id"],
            summary=summary,
        ),
    )


async def claim(connection, worker, kinds, as_of):
    row = await (
        await connection.execute(
            """select * from collective.jobs where kind=any(%s) and
        ((status='queued' and next_attempt_at<=%s) or (status='running' and lease_until<=%s))
        order by coalesce(lease_until,next_attempt_at),id for update skip locked limit 1""",
            (list(kinds), as_of, as_of),
        )
    ).fetchone()
    if row is None:
        return None
    if row["attempts"] >= MAX_ATTEMPTS:
        await connection.execute(
            """update collective.jobs set status='failed',error_code='LEASE_EXHAUSTED',
            worker_id=null,lease_until=null,completed_at=%s where id=%s""",
            (as_of, row["id"]),
        )
        await event(connection, row, "failed")
        return None
    return await (
        await connection.execute(
            """update collective.jobs set status='running',worker_id=%s,lease_until=%s,
        attempts=attempts+1 where id=%s returning *""",
            (worker, as_of + LEASE, row["id"]),
        )
    ).fetchone()


async def hold_claim(connection, job, as_of):
    row = await (
        await connection.execute(
            """select id from collective.jobs where id=%s and status='running' and worker_id=%s
        and attempts=%s and lease_until>%s for update""",
            (job["id"], job["worker_id"], job["attempts"], as_of),
        )
    ).fetchone()
    if row is None:
        raise LeaseLost()


async def complete(connection, job, result, as_of):
    updated = await connection.execute(
        """update collective.jobs set status='succeeded',result=%s,completed_at=%s,
        error_code=null,worker_id=null,lease_until=null where id=%s and status='running'
        and worker_id=%s and attempts=%s and lease_until>%s""",
        (Jsonb(result), as_of, job["id"], job["worker_id"], job["attempts"], as_of),
    )
    if updated.rowcount != 1:
        raise LeaseLost()
    await event(connection, job, "succeeded")


async def fail(connection, job, as_of, *, retryable, code, retry_at=None):
    await hold_claim(connection, job, as_of)
    retry = retryable and job["attempts"] < MAX_ATTEMPTS
    status = "queued" if retry else "failed"
    await connection.execute(
        """update collective.jobs set status=%s,error_code=%s,worker_id=null,lease_until=null,
        next_attempt_at=%s,completed_at=%s where id=%s""",
        (
            status,
            code,
            retry_at or as_of + timedelta(seconds=5 * 2 ** (job["attempts"] - 1)),
            None if retry else as_of,
            job["id"],
        ),
    )
    await event(connection, job, status)
