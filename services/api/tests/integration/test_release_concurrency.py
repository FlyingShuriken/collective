import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from collective.repositories.pools import lock_inputs
from tests.integration.conftest import local_test_url
from tests.integration.scenarios import clone_business_data
from tests.integration.test_pool_commands import (
    approve,
    business,
    current,
    need,
    ready,
    revise_quote,
    withdraw,
    write_quote,
)


@pytest.fixture
async def cohort(access_seed):
    return await clone_business_data(access_seed, "abc", pool_opt_in=True)


async def release(client, cohort, auth_headers, pool, *, key=None, version=None, letter="a"):
    if version is None:
        version = (await current(client, cohort, auth_headers, pool, letter))["evaluation_revision"]
    return await client.post(
        f"/v1/businesses/{business(cohort, letter)}/pools/{pool}/release",
        headers=auth_headers("owner-" + letter) | {"Idempotency-Key": key or str(uuid4())},
        json={"expected_revision": version},
    )


async def orders(database, pool):
    async with database.transaction("coordinator", None) as conn:
        return await (
            await conn.execute(
                """select po.* from collective.purchase_orders po
            join collective.releases r on r.id=po.release_id where r.pool_id=%s
            order by po.business_id""",
                (pool,),
            )
        ).fetchall()


async def test_final_two_member_orders_use_issued_tier_and_are_once_only(api, cohort, auth_headers):
    client, database = api
    pool, bindings = await ready(client, cohort, auth_headers)
    await withdraw(client, cohort, auth_headers, pool, "c")
    assert not await orders(database, pool)
    blocked = await release(client, cohort, auth_headers, pool, letter="b")
    assert blocked.status_code == 200 and blocked.json()["state"] == "awaiting_approvals"
    fresh = await approve(client, cohort, auth_headers, pool, "a", "285.00")
    assert fresh.status_code == 200
    key, version = str(uuid4()), fresh.json()["offer"]["evaluation_revision"]
    result = await release(client, cohort, auth_headers, pool, key=key, version=version)
    assert result.status_code == 200 and result.json()["state"] == "released"
    own = result.json()["order"]
    assert own["amount"] == own["decision_amount"] == "285.00"
    assert own["terms"]["quantity"] == "1000.000000"
    again = await release(client, cohort, auth_headers, pool, key=key, version=version)
    other_key = await release(client, cohort, auth_headers, pool, version=version)
    assert again.json() == other_key.json() == result.json()
    rows = await orders(database, pool)
    assert len(rows) == 2 and {str(row["amount"]) for row in rows} == {"285.00"}
    assert bindings["a"] not in {str(row["approval_id"]) for row in rows}
    assert {str(row["snapshot"]["cost"]["unit_price"]) for row in rows} == {"0.270000"}
    assert (await current(client, cohort, auth_headers, pool, "a"))["release_state"] == "released"
    late = await withdraw(client, cohort, auth_headers, pool, "b")
    assert late.status_code == 409 and late.json()["code"] == "ALREADY_RELEASED"


@pytest.mark.parametrize("same_key", [True, False])
async def test_racing_release_commands_create_one_complete_set(api, cohort, auth_headers, same_key):
    client, database = api
    pool, _ = await ready(client, cohort, auth_headers)
    version = (await current(client, cohort, auth_headers, pool, "a"))["evaluation_revision"]
    key = str(uuid4())
    results = await asyncio.gather(
        *(
            release(
                client,
                cohort,
                auth_headers,
                pool,
                version=version,
                key=key if same_key else str(uuid4()),
            )
            for _ in range(2)
        )
    )
    assert all(r.status_code == 200 for r in results)
    assert results[0].json() == results[1].json()
    assert len(await orders(database, pool)) == 3


async def test_release_discovers_changed_terms_and_commits_void(api, cohort, auth_headers):
    client, database = api
    pool, _ = await ready(client, cohort, auth_headers)
    await revise_quote(cohort, {"revision": 2, "delivery_fee": "0.00"})
    response = await release(client, cohort, auth_headers, pool)
    assert response.status_code == 200 and response.json()["state"] == "awaiting_approvals"
    assert response.json()["offer"]["approval_state"] == "void"
    assert not await orders(database, pool)
    assert (await current(client, cohort, auth_headers, pool, "a"))["approval_state"] == "void"


async def test_partial_po_or_audit_failure_rolls_back_every_output(
    api, cohort, auth_headers, monkeypatch
):
    from collective.services import releases

    client, database = api
    pool, _ = await ready(client, cohort, auth_headers)
    original = releases.append_event
    count = 0

    async def fail(connection, event):
        nonlocal count
        if event.event_type == "po_created":
            count += 1
            if count == 2:
                raise RuntimeError("injected partial release failure")
        return await original(connection, event)

    monkeypatch.setattr(releases, "append_event", fail)
    with pytest.raises(RuntimeError, match="injected partial release failure"):
        await release(client, cohort, auth_headers, pool)
    assert count == 2 and not await orders(database, pool)
    assert (await current(client, cohort, auth_headers, pool, "a"))["release_state"] == "ready"
    async with database.transaction("coordinator", None) as conn:
        rows = await (
            await conn.execute("select id from collective.releases where pool_id=%s", (pool,))
        ).fetchall()
        assert not rows


async def test_unauthorised_or_withdrawn_member_cannot_execute(api, cohort, auth_headers):
    client, database = api
    pool, _ = await ready(client, cohort, auth_headers)
    for name, status in (("staff", 403), ("owner-b", 404), ("admin", 404)):
        response = await client.post(
            f"/v1/businesses/{business(cohort, 'a')}/pools/{pool}/release",
            headers=auth_headers(name) | {"Idempotency-Key": str(uuid4())},
            json={"expected_revision": 4},
        )
        assert response.status_code == status
    await withdraw(client, cohort, auth_headers, pool, "c")
    await approve(client, cohort, auth_headers, pool, "a", "285.00")
    response = await release(client, cohort, auth_headers, pool, letter="c")
    assert response.status_code == 200 and response.json()["state"] == "ineligible"
    assert not await orders(database, pool)


async def test_withdrawal_racing_release_cannot_issue_an_unapproved_subset(
    api, cohort, auth_headers
):
    client, database = api
    pool, _ = await ready(client, cohort, auth_headers)
    results = await asyncio.gather(
        release(client, cohort, auth_headers, pool, version=4),
        client.post(
            f"/v1/businesses/{business(cohort, 'c')}/pools/{pool}/withdraw",
            headers=auth_headers("owner-c") | {"Idempotency-Key": str(uuid4())},
            json={"expected_revision": 4},
        ),
    )
    rows = await orders(database, pool)
    if rows:
        assert len(rows) == 3 and {str(r["amount"]) for r in rows} == {"255.00"}
        assert results[0].json()["state"] == "released" and results[1].status_code == 409
    else:
        assert results[1].status_code == 200 and results[0].status_code == 409
        assert (await current(client, cohort, auth_headers, pool, "a"))["approval_state"] == "void"


async def wait_for_advisory_wait(connection, pid):
    for _ in range(100):
        row = await (
            await connection.execute("select wait_event from pg_stat_activity where pid=%s", (pid,))
        ).fetchone()
        if row and row["wait_event"] == "advisory":
            return
        await asyncio.sleep(0.01)
    pytest.fail("The competing writer did not reach the cohort lock")


async def test_quote_edit_waiting_on_release_cannot_rewrite_issued_terms(
    api, cohort, auth_headers, monkeypatch
):
    from collective.services import releases

    client, database = api
    pool, _ = await ready(client, cohort, auth_headers)
    locked, resume = asyncio.Event(), asyncio.Event()
    original = releases.issue_pool

    async def hold(*args):
        locked.set()
        await resume.wait()
        await original(*args)

    monkeypatch.setattr(releases, "issue_pool", hold)
    request = asyncio.create_task(release(client, cohort, auth_headers, pool, version=4))
    await asyncio.wait_for(locked.wait(), 2)
    async with await AsyncConnection.connect(
        local_test_url("MIGRATION_DATABASE_URL"), row_factory=dict_row
    ) as writer:
        spec = (
            await (
                await writer.execute(
                    "select spec_id from collective.reorder_needs where id=%s", (need(cohort, "a"),)
                )
            ).fetchone()
        )["spec_id"]
        # The fixture quote writer follows the same locks as the forthcoming confirmation gate.
        pending = asyncio.create_task(lock_inputs(writer, spec))
        async with await AsyncConnection.connect(
            local_test_url("MIGRATION_DATABASE_URL"), row_factory=dict_row, autocommit=True
        ) as observer:
            await wait_for_advisory_wait(observer, writer.info.backend_pid)
        resume.set()
        response = await request
        assert response.status_code == 200 and response.json()["state"] == "released"
        await pending
        await write_quote(writer, cohort, {"revision": 2, "delivery_fee": "0.00"})
    rows = await orders(database, pool)
    assert len(rows) == 3
    assert {r["snapshot"]["quote"]["revision"] for r in rows} == {1}
    assert {r["snapshot"]["cost"]["delivery_fee"] for r in rows} == {"15.00"}


async def test_release_waits_for_quote_commit_and_uses_the_changed_terms(
    api, cohort, auth_headers, monkeypatch
):
    from collective.services import releases

    client, database = api
    pool, _ = await ready(client, cohort, auth_headers)
    started, pid = asyncio.Event(), []
    original = releases.lock_pool

    async def observe(connection, *args):
        pid.append(connection.info.backend_pid)
        started.set()
        return await original(connection, *args)

    monkeypatch.setattr(releases, "lock_pool", observe)
    async with await AsyncConnection.connect(
        local_test_url("MIGRATION_DATABASE_URL"), row_factory=dict_row
    ) as writer:
        spec = (
            await (
                await writer.execute(
                    "select spec_id from collective.reorder_needs where id=%s", (need(cohort, "a"),)
                )
            ).fetchone()
        )["spec_id"]
        await lock_inputs(writer, spec)
        await write_quote(writer, cohort, {"revision": 2, "delivery_fee": "0.00"})
        pending = asyncio.create_task(release(client, cohort, auth_headers, pool, version=4))
        await asyncio.wait_for(started.wait(), 2)
        async with await AsyncConnection.connect(
            local_test_url("MIGRATION_DATABASE_URL"), row_factory=dict_row, autocommit=True
        ) as observer:
            await wait_for_advisory_wait(observer, pid[0])
        await writer.commit()
        response = await pending
    assert response.status_code == 200 and response.json()["state"] == "awaiting_approvals"
    assert response.json()["offer"]["approval_state"] == "void"
    assert not await orders(database, pool)


async def test_release_checks_expiry_even_without_a_scheduled_job(
    api, cohort, auth_headers, monkeypatch
):
    from collective.api import order_routes
    from tests.integration.test_pool_commands import discovery

    client, database = api
    pool = await discovery(client, cohort, auth_headers)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    for letter in "abc":
        result = await approve(
            client,
            cohort,
            auth_headers,
            pool,
            letter,
            "285.00",
            patch={"expires_at": expiry.isoformat()} if letter == "a" else None,
        )
        assert result.status_code == 200

    class ExactExpiry:
        def now(self):
            return expiry

    monkeypatch.setattr(order_routes, "SystemClock", ExactExpiry)
    response = await release(client, cohort, auth_headers, pool, letter="a")
    assert response.status_code == 200 and response.json()["state"] == "ineligible"
    assert response.json()["offer"]["approval_state"] == "expired"
    assert not await orders(database, pool)


async def test_database_deadline_guard_rolls_back_outputs_and_commits_expiry(
    api, cohort, auth_headers, monkeypatch
):
    from collective.services import releases
    from tests.integration.test_pool_commands import discovery

    client, database = api
    pool = await discovery(client, cohort, auth_headers)
    expiry = datetime.now(UTC) + timedelta(seconds=2)
    for letter in "abc":
        response = await approve(
            client,
            cohort,
            auth_headers,
            pool,
            letter,
            "285.00",
            patch={"expires_at": expiry.isoformat()} if letter == "a" else None,
        )
        assert response.status_code == 200
    original = releases.issue_pool

    async def cross_deadline(*args):
        remaining = (expiry - datetime.now(UTC)).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining + 0.02)
        await original(*args)

    monkeypatch.setattr(releases, "issue_pool", cross_deadline)
    response = await release(client, cohort, auth_headers, pool, letter="a")
    assert response.status_code == 200 and response.json()["state"] == "ineligible"
    assert response.json()["offer"]["approval_state"] == "expired"
    assert not await orders(database, pool)


async def test_order_and_activity_projections_are_scoped_and_hide_prices(api, cohort, auth_headers):
    client, _ = api
    pool, _ = await ready(client, cohort, auth_headers)
    await release(client, cohort, auth_headers, pool)
    route = f"/v1/businesses/{business(cohort, 'a')}/needs/{need(cohort, 'a')}/order"
    own = await client.get(route, headers=auth_headers("owner-a"))
    staff = await client.get(route, headers=auth_headers("staff"))
    assert own.status_code == staff.status_code == 200
    assert own.json()["amount"] == "255.00" and own.json()["implementation"] == "simulated"
    assert staff.json()["price_visibility"] == "hidden"
    assert not {"amount", "decision_amount", "cost", "terms", "supplier_id"} & staff.json().keys()
    for account in ("owner-b", "admin"):
        assert (await client.get(route, headers=auth_headers(account))).status_code == 404
    root = f"/v1/businesses/{business(cohort, 'a')}/events"
    page = await client.get(root + "?limit=2", headers=auth_headers("staff"))
    assert page.status_code == 200 and page.json()["next_cursor"]
    second = await client.get(
        root + "?cursor=" + page.json()["next_cursor"], headers=auth_headers("staff")
    )
    first_ids = {r["id"] for r in page.json()["items"]}
    assert not first_ids & {r["id"] for r in second.json()["items"]}
    assert all("RM" not in r["summary"] for r in second.json()["items"])
    assert (await client.get(root, headers=auth_headers("owner-b"))).status_code == 404
