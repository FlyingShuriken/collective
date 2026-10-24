import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation

from collective.repositories import jobs
from tests.fixtures import scenario


@pytest.fixture
def context(access_id):
    return {
        "business": access_id(scenario()["businesses"][0]["id"]),
        "kind": "test-" + uuid4().hex,
        "now": datetime.now(UTC),
        "key": uuid4().hex,
    }


async def enqueue(database, context):
    async with database.transaction("coordinator", None) as conn:
        return await jobs.enqueue(
            conn,
            business=context["business"],
            kind=context["kind"],
            revision=1,
            key=context["key"],
            payload={"entity_id": str(uuid4())} if "payload" not in context else context["payload"],
            due=context["now"],
        )


async def test_duplicate_enqueue_and_competing_claims_are_once_only(api, context):
    _, database = api
    context["payload"] = {"entity_id": str(uuid4())}
    first = await enqueue(database, context)
    again = await enqueue(database, context)
    assert first["id"] == again["id"]
    with pytest.raises(ValueError, match="conflicts"):
        await enqueue(database, context | {"payload": {"entity_id": str(uuid4())}})

    async def claim():
        async with database.transaction("coordinator", None) as conn:
            return await jobs.claim(conn, uuid4(), (context["kind"],), context["now"])

    results = await asyncio.gather(claim(), claim())
    assert len([r for r in results if r]) == 1


async def test_expired_lease_cannot_publish_after_reclaim(api, context):
    _, database = api
    await enqueue(database, context)
    async with database.transaction("coordinator", None) as conn:
        old = await jobs.claim(conn, uuid4(), (context["kind"],), context["now"])
    later = context["now"] + jobs.LEASE
    async with database.transaction("coordinator", None) as conn:
        new = await jobs.claim(conn, uuid4(), (context["kind"],), later)
    assert new["id"] == old["id"] and new["attempts"] == 2
    with pytest.raises(jobs.LeaseLost):
        async with database.transaction("coordinator", None) as conn:
            await jobs.complete(conn, old, {"state": "stale"}, later)
    async with database.transaction("coordinator", None) as conn:
        await jobs.complete(conn, new, {"state": "completed"}, later)


async def test_committed_result_survives_restart_before_acknowledgement(api, context):
    _, database = api
    context["payload"] = {"entity_id": str(uuid4())}
    await enqueue(database, context)
    async with database.transaction("coordinator", None) as conn:
        claim = await jobs.claim(conn, uuid4(), (context["kind"],), context["now"])
    async with database.transaction("coordinator", None) as conn:
        await jobs.complete(
            conn, claim, {"state": "completed", "result_key": context["key"]}, context["now"]
        )
    duplicate = await enqueue(database, context)
    assert (
        duplicate["status"] == "succeeded" and duplicate["result"]["result_key"] == context["key"]
    )
    async with database.transaction("coordinator", None) as conn:
        assert (
            await jobs.claim(conn, uuid4(), (context["kind"],), context["now"] + jobs.LEASE) is None
        )


async def test_retry_is_bounded_and_status_does_not_imply_confirmation(api, context):
    _, database = api
    await enqueue(database, context)
    now = context["now"]
    for attempt in range(1, 4):
        async with database.transaction("coordinator", None) as conn:
            claim = await jobs.claim(conn, uuid4(), (context["kind"],), now)
            assert claim["attempts"] == attempt
            await jobs.fail(conn, claim, now, retryable=True, code="RETRYABLE_FAILURE")
        now += timedelta(seconds=30)
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute("select * from collective.jobs where id=%s", (claim["id"],))
        ).fetchone()
        assert row["status"] == "failed" and row["result"] is None
        assert await jobs.claim(conn, uuid4(), (context["kind"],), now) is None


async def test_successful_job_inputs_and_result_are_immutable(api, context):
    _, database = api
    await enqueue(database, context)
    async with database.transaction("coordinator", None) as conn:
        claim = await jobs.claim(conn, uuid4(), (context["kind"],), context["now"])
        await jobs.complete(conn, claim, {"state": "completed"}, context["now"])
    with pytest.raises(CheckViolation):
        async with database.transaction("coordinator", None) as conn:
            await conn.execute("update collective.jobs set result='{}' where id=%s", (claim["id"],))


async def test_runner_commits_claim_before_processing(api, context):
    from collective.jobs.runner import Runner

    _, database = api
    await enqueue(database, context)

    class Clock:
        def now(self):
            return context["now"]

    async def handler(database, job, clock):
        async with database.transaction("coordinator", None) as conn:
            await conn.execute(
                "select id from collective.jobs where id=%s for update nowait", (job["id"],)
            )
            await jobs.complete(conn, job, {"state": "completed"}, clock.now())

    runner = Runner(database, clock=Clock(), handlers={context["kind"]: handler})
    assert await runner.run_once()
    assert not await runner.run_once()


async def test_handler_output_and_completion_roll_back_together(api, context):
    from collective.jobs.runner import RetryableFailure, Runner
    from collective.repositories.events import Event, append_event

    _, database = api
    job = await enqueue(database, context)

    class Clock:
        def now(self):
            return context["now"]

    async def failing(database, job, clock):
        async with database.transaction("coordinator", None) as conn:
            await jobs.hold_claim(conn, job, clock.now())
            await append_event(
                conn,
                Event(
                    business_id=context["business"],
                    actor_id=None,
                    actor_kind="system",
                    event_type="test_output",
                    entity_id=job["id"],
                    correlation_id=job["id"],
                    summary="An uncommitted test output.",
                ),
            )
            raise RetryableFailure("private error content")

    assert await Runner(database, clock=Clock(), handlers={context["kind"]: failing}).run_once()
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select status,result,error_code from collective.jobs where id=%s", (job["id"],)
            )
        ).fetchone()
        assert row == {"status": "queued", "result": None, "error_code": "RETRYABLE_FAILURE"}
        output = await (
            await conn.execute(
                "select id from collective.events where entity_id=%s and event_type='test_output'",
                (job["id"],),
            )
        ).fetchall()
        assert not output


async def test_job_status_is_private(api, context, auth_headers):
    client, database = api
    context["payload"] = {"private": "PRIVATE_VALUE_MUST_NOT_APPEAR"}
    job = await enqueue(database, context)
    root = f"/v1/businesses/{context['business']}/jobs"
    for name in ("owner-a", "staff"):
        response = await client.get(root, headers=auth_headers(name))
        assert response.status_code == 200 and "PRIVATE_VALUE_MUST_NOT_APPEAR" not in response.text
        own = next(r for r in response.json()["items"] if r["id"] == str(job["id"]))
        assert own["status"] == "queued"
        assert not {"payload", "result", "worker_id", "dedup_key"} & own.keys()
    for name in ("owner-b", "admin"):
        assert (await client.get(root, headers=auth_headers(name))).status_code == 404


async def test_lifespan_starts_and_stops_the_configured_worker(runtime_url, monkeypatch):
    from collective import main
    from collective.settings import Settings

    started, stopped = asyncio.Event(), asyncio.Event()

    class Worker:
        def __init__(self, database, *, settings):
            self.database = database
            assert settings.enable_worker is True

        async def run(self):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    monkeypatch.setattr(main, "Runner", Worker)
    app = main.create_app(Settings(database_url=runtime_url, enable_worker=True))
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 1)
    assert stopped.is_set()


async def test_repeated_crashed_leases_stop_after_three_attempts(api, context):
    _, database = api
    job = await enqueue(database, context)
    now = context["now"]
    for attempt in range(1, 4):
        async with database.transaction("coordinator", None) as conn:
            claim = await jobs.claim(conn, uuid4(), (context["kind"],), now)
            assert claim["attempts"] == attempt
        now += jobs.LEASE
    async with database.transaction("coordinator", None) as conn:
        assert await jobs.claim(conn, uuid4(), (context["kind"],), now) is None
        result = await (
            await conn.execute(
                "select status,result,error_code from collective.jobs where id=%s", (job["id"],)
            )
        ).fetchone()
        assert result == {"status": "failed", "result": None, "error_code": "LEASE_EXHAUSTED"}


async def test_null_result_cannot_be_marked_completed(api, context):
    _, database = api
    await enqueue(database, context)
    async with database.transaction("coordinator", None) as conn:
        claim = await jobs.claim(conn, uuid4(), (context["kind"],), context["now"])
    with pytest.raises(CheckViolation):
        async with database.transaction("coordinator", None) as conn:
            await jobs.complete(conn, claim, None, context["now"])
