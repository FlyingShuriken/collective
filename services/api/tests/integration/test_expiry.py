from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from collective.jobs.handlers import handle_due
from collective.repositories import jobs
from collective.services import needs
from tests.integration.scenarios import clone_business_data
from tests.integration.test_pool_commands import business, current, discovery, need, ready
from tests.integration.test_release_concurrency import orders, release


@pytest.fixture
async def cohort(access_seed):
    return await clone_business_data(access_seed, "abc", pool_opt_in=True)


class Clock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value


async def queued(database, cohort, *, reason, reference, due, letter="a"):
    async with database.transaction("coordinator", None) as conn:
        row = await needs.load_need(conn, business(cohort, letter), need(cohort, letter))
        kind = "test-due-" + uuid4().hex
        job = await jobs.enqueue(
            conn,
            business=row["business_id"],
            kind=kind,
            revision=row["revision"],
            key=uuid4().hex,
            payload={
                "need_id": str(row["id"]),
                "reason": reason,
                "reference_id": str(reference),
                "due_at": due.isoformat(),
            },
            due=due,
        )
        return job


async def claimed(database, job, as_of):
    async with database.transaction("coordinator", None) as conn:
        return await jobs.claim(conn, uuid4(), (job["kind"],), as_of)


async def test_expiry_job_reprices_and_cannot_repeat_its_output(api, cohort, auth_headers):
    client, database = api
    pool, ids = await ready(client, cohort, auth_headers)
    async with database.transaction("coordinator", None) as conn:
        approval = await (
            await conn.execute(
                "select expires_at from collective.bound_approvals where id=%s", (ids["a"],)
            )
        ).fetchone()
    due = approval["expires_at"]
    job = await queued(database, cohort, reason="approval", reference=ids["a"], due=due)
    claim = await claimed(database, job, due)
    await handle_due(database, claim, Clock(due))
    a = await current(client, cohort, auth_headers, pool, "a")
    b = await current(client, cohort, auth_headers, pool, "b")
    assert a["approval_state"] == a["member_state"] == "expired"
    assert b["decision_delivered_cost"] == "285.00" and b["approval_state"] == "valid"
    assert not await orders(database, pool)
    with pytest.raises(jobs.LeaseLost):
        await handle_due(database, claim, Clock(due))
    async with database.transaction("coordinator", None) as conn:
        events = await (
            await conn.execute(
                """select id from collective.events
                where entity_id=%s and event_type='approval_expired'""",
                (ids["a"],),
            )
        ).fetchall()
        assert len(events) == 1


async def test_delayed_job_cannot_bypass_synchronous_release_checks(
    api, cohort, auth_headers, monkeypatch
):
    from collective.api import order_routes

    client, database = api
    pool, ids = await ready(client, cohort, auth_headers)
    async with database.transaction("coordinator", None) as conn:
        due = (
            await (
                await conn.execute(
                    "select expires_at from collective.bound_approvals where id=%s", (ids["a"],)
                )
            ).fetchone()
        )["expires_at"]
    job = await queued(database, cohort, reason="approval", reference=ids["a"], due=due)
    monkeypatch.setattr(order_routes, "SystemClock", lambda: Clock(due))
    response = await release(client, cohort, auth_headers, pool)
    assert response.status_code == 200 and response.json()["state"] == "ineligible"
    assert not await orders(database, pool)
    late = await claimed(database, job, due + timedelta(minutes=1))
    await handle_due(database, late, Clock(due + timedelta(minutes=1)))
    async with database.transaction("coordinator", None) as conn:
        result = await (
            await conn.execute("select result from collective.jobs where id=%s", (job["id"],))
        ).fetchone()
        assert result["result"]["state"] == "superseded"


async def test_unapproved_pool_deadline_releases_all_reservations(api, cohort, auth_headers):
    client, database = api
    pool = await discovery(client, cohort, auth_headers)
    async with database.transaction("coordinator", None) as conn:
        due = (
            await (
                await conn.execute("select deadline from collective.pools where id=%s", (pool,))
            ).fetchone()
        )["deadline"]
    job = await queued(database, cohort, reason="pool", reference=pool, due=due)
    await handle_due(database, await claimed(database, job, due), Clock(due))
    async with database.transaction("coordinator", None) as conn:
        members = await (
            await conn.execute(
                "select state from collective.pool_members where pool_id=%s", (pool,)
            )
        ).fetchall()
        assert {m["state"] for m in members} == {"expired"}
        for letter in "abc":
            row = await needs.load_need(conn, business(cohort, letter), need(cohort, letter))
            assert row["reserved_pool_id"] is None and row["state"] == "stale"
    assert not await orders(database, pool)


async def test_sql_enqueue_deduplicates_due_work(api, cohort):
    _, database = api
    async with database.transaction("coordinator", None) as conn:
        await conn.execute("select collective_private.enqueue_due(%s)", (datetime.now(UTC),))
        first = await (
            await conn.execute(
                "select id from collective.jobs where business_id=%s", (business(cohort, "a"),)
            )
        ).fetchall()
        await conn.execute("select collective_private.enqueue_due(%s)", (datetime.now(UTC),))
        again = await (
            await conn.execute(
                "select id from collective.jobs where business_id=%s", (business(cohort, "a"),)
            )
        ).fetchall()
        assert len(first) == 1 and {r["id"] for r in first} == {r["id"] for r in again}


async def test_reorder_job_remains_an_owner_review(api, cohort):
    _, database = api
    async with database.transaction("coordinator", None) as conn:
        row = await needs.load_need(conn, business(cohort, "a"), need(cohort, "a"))
    now = datetime.now(UTC)
    job = await queued(
        database, cohort, reason="reorder", reference=row["id"], due=row["order_start"]
    )
    await handle_due(database, await claimed(database, job, now), Clock(now))
    async with database.transaction("coordinator", None) as conn:
        result = await (
            await conn.execute("select result from collective.jobs where id=%s", (job["id"],))
        ).fetchone()
        assert result["result"]["state"] == "ready_for_review"
        assert not (
            await conn.execute(
                "select id from collective.bound_approvals where business_id=%s",
                (business(cohort, "a"),),
            )
        ).rowcount
        assert (await needs.load_need(conn, business(cohort, "a"), row["id"]))[
            "state"
        ] == "selected"


async def test_changed_input_cannot_publish_an_old_reorder_notice(api, cohort):
    _, database = api
    async with database.transaction("coordinator", None) as conn:
        row = await needs.load_need(conn, business(cohort, "a"), need(cohort, "a"))
    now = datetime.now(UTC)
    job = await queued(
        database, cohort, reason="reorder", reference=row["id"], due=row["order_start"]
    )
    claim = await claimed(database, job, now)
    async with database.transaction("coordinator", None) as conn:
        await conn.execute(
            """update collective.reorder_needs
            set state='stale',revision=revision+1 where id=%s""",
            (row["id"],),
        )
    await handle_due(database, claim, Clock(now))
    async with database.transaction("coordinator", None) as conn:
        result = await (
            await conn.execute("select result from collective.jobs where id=%s", (job["id"],))
        ).fetchone()
        assert result["result"]["state"] == "superseded"
        events = await (
            await conn.execute(
                """select id from collective.events
            where correlation_id=%s and event_type='reorder_notice'""",
                (job["id"],),
            )
        ).fetchall()
        assert not events


async def test_early_expiry_work_is_rescheduled_without_confirmation(api, cohort, auth_headers):
    from collective.jobs.runner import Runner

    client, database = api
    _, ids = await ready(client, cohort, auth_headers)
    async with database.transaction("coordinator", None) as conn:
        due = (
            await (
                await conn.execute(
                    "select expires_at from collective.bound_approvals where id=%s", (ids["a"],)
                )
            ).fetchone()
        )["expires_at"]
    job = await queued(database, cohort, reason="approval", reference=ids["a"], due=due)

    class ClockMovedBack:
        calls = 0

        def now(self):
            self.calls += 1
            return due if self.calls == 1 else due - timedelta(seconds=1)

    assert await Runner(
        database, clock=ClockMovedBack(), handlers={job["kind"]: handle_due}
    ).run_once()
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select status,result,next_attempt_at,error_code from collective.jobs where id=%s",
                (job["id"],),
            )
        ).fetchone()
        assert row == {
            "status": "queued",
            "result": None,
            "next_attempt_at": due,
            "error_code": "NOT_DUE",
        }
