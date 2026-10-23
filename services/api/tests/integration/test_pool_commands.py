import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from tests.fixtures import fixture_id, scenario
from tests.integration.conftest import local_test_url
from tests.integration.scenarios import clone_business_data


@pytest.fixture
async def cohort(access_seed):
    return await clone_business_data(access_seed, "abc", pool_opt_in=True)


def business(cohort, letter):
    return cohort(scenario()["businesses"]["abc".index(letter)]["id"])


def need(cohort, letter):
    return cohort(scenario()["businesses"]["abc".index(letter)]["need_id"])


async def current(client, cohort, auth_headers, pool, letter):
    response = await client.get(
        f"/v1/businesses/{business(cohort, letter)}/pools/{pool}/offer",
        headers=auth_headers("owner-" + letter),
    )
    assert response.status_code == 200
    return response.json()


async def discovery(client, cohort, auth_headers):
    response = await client.post(
        f"/v1/businesses/{business(cohort, 'a')}/pools/discover",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"need_id": str(need(cohort, "a")), "expected_revision": 1},
    )
    assert response.status_code == 200
    return response.json()["offer"]["pool_id"]


async def approve(
    client, cohort, auth_headers, pool, letter, cap, *, key=None, version=None, patch=None
):
    offer = await current(client, cohort, auth_headers, pool, letter)
    return await client.post(
        f"/v1/businesses/{business(cohort, letter)}/approvals",
        headers=auth_headers("owner-" + letter) | {"Idempotency-Key": key or str(uuid4())},
        json={
            "need_id": str(need(cohort, letter)),
            "pool_id": pool,
            "cap": cap,
            "expires_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat(),
            "expected_evaluation_revision": version or offer["evaluation_revision"],
            **(patch or {}),
        },
    )


async def withdraw(client, cohort, auth_headers, pool, letter):
    offer = await current(client, cohort, auth_headers, pool, letter)
    return await client.post(
        f"/v1/businesses/{business(cohort, letter)}/pools/{pool}/withdraw",
        headers=auth_headers("owner-" + letter) | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": offer["evaluation_revision"]},
    )


async def ready(client, cohort, auth_headers):
    pool = await discovery(client, cohort, auth_headers)
    approvals = {}
    for letter, cap in zip("abc", ("276.00", "285.00", "276.00"), strict=True):
        result = await approve(client, cohort, auth_headers, pool, letter, cap)
        assert result.status_code == 200 and result.json()["status"] == "approved"
        approvals[letter] = result.json()["approval_id"]
    assert (await current(client, cohort, auth_headers, pool, "a"))["release_state"] == "ready"
    return pool, approvals


async def test_bound_approval_withdrawal_and_fresh_consent_are_persisted(api, cohort, auth_headers):
    client, database = api
    pool, ids = await ready(client, cohort, auth_headers)
    response = await withdraw(client, cohort, auth_headers, pool, "c")
    assert response.status_code == 200 and response.json()["status"] == "withdrawn"
    a = await current(client, cohort, auth_headers, pool, "a")
    b = await current(client, cohort, auth_headers, pool, "b")
    assert a["decision_delivered_cost"] == b["decision_delivered_cost"] == "285.00"
    assert a["approval_state"] == "void" and "CAP_EXCEEDED" in a["failure_codes"]
    assert b["approval_state"] == "valid" and b["release_state"] == "waiting_for_approvals"
    fresh = await approve(client, cohort, auth_headers, pool, "a", "285.00")
    assert fresh.status_code == 200 and fresh.json()["status"] == "approved"
    assert fresh.json()["approval_id"] != ids["a"]
    assert fresh.json()["offer"]["release_state"] == "ready"
    old = await client.get("/v1/hub/approvals/" + ids["a"], headers=auth_headers("owner-a"))
    assert old.json()["cap"] == "276.00" and old.json()["state"] == "void"
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select count(*) as n from collective.purchase_orders where business_id=any(%s)",
                ([business(cohort, letter) for letter in "abc"],),
            )
        ).fetchone()
        assert row["n"] == 0
        events = await (
            await conn.execute(
                "select event_type from collective.events where business_id=%s and entity_id=%s",
                (business(cohort, "b"), ids["b"]),
            )
        ).fetchall()
        assert "approval_stands" in {e["event_type"] for e in events}


async def test_decline_cascade_voids_remaining_285_cap_at_315(api, cohort, auth_headers):
    client, _ = api
    pool, _ = await ready(client, cohort, auth_headers)
    await withdraw(client, cohort, auth_headers, pool, "c")
    response = await withdraw(client, cohort, auth_headers, pool, "a")
    assert response.status_code == 200
    b = await current(client, cohort, auth_headers, pool, "b")
    assert b["decision_delivered_cost"] == "315.00" and b["approval_state"] == "void"
    assert b["member_count"] == 1 and b["release_state"] == "ineligible"


async def test_stale_cap_and_forged_binding_fields_create_no_approval(api, cohort, auth_headers):
    client, database = api
    pool = await discovery(client, cohort, auth_headers)
    low = await approve(client, cohort, auth_headers, pool, "a", "254.99")
    assert low.status_code == 422 and low.json()["code"] == "CAP_EXCEEDED"
    forged = await approve(
        client,
        cohort,
        auth_headers,
        pool,
        "a",
        "276.00",
        patch={"owner_id": str(uuid4()), "quantity": "9000"},
    )
    assert forged.status_code == 422
    a = await approve(client, cohort, auth_headers, pool, "a", "255.00")
    assert a.status_code == 200
    stale = await approve(client, cohort, auth_headers, pool, "b", "285.00", version=1)
    assert stale.status_code == 409
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select count(*) as n from collective.bound_approvals where pool_id=%s", (pool,)
            )
        ).fetchone()
        assert row["n"] == 1


async def test_changed_quote_commits_void_events_and_cannot_revive_permission(
    api, cohort, auth_headers
):
    client, database = api
    pool, ids = await ready(client, cohort, auth_headers)
    before = await current(client, cohort, auth_headers, pool, "a")
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await conn.execute(
            """insert into collective.quote_revisions select
            (jsonb_populate_record(null::collective.quote_revisions,to_jsonb(q)||
              jsonb_build_object('revision',2,'delivery_fee',0,'sealed',false))).*
            from collective.quote_revisions q where business_id=%s and id=%s and revision=1""",
            (business(cohort, "a"), cohort(fixture_id("quote-a-b"))),
        )
        await conn.execute(
            """insert into collective.quote_tiers
            select business_id,quote_id,2,min_quantity,unit_price from collective.quote_tiers
            where business_id=%s and quote_id=%s and quote_revision=1""",
            (business(cohort, "a"), cohort(fixture_id("quote-a-b"))),
        )
        await conn.execute(
            "update collective.quote_revisions set sealed=true where business_id=%s and revision=2",
            (business(cohort, "a"),),
        )
    attempted = await approve(
        client, cohort, auth_headers, pool, "a", "276.00", version=before["evaluation_revision"]
    )
    assert attempted.status_code == 200 and attempted.json()["status"] == "refresh_required"
    assert attempted.json()["offer"]["approval_state"] == "void"
    async with database.transaction("coordinator", None) as conn:
        original = await (
            await conn.execute(
                "select state,cap from collective.bound_approvals where id=%s", (ids["a"],)
            )
        ).fetchone()
        assert original["state"] == "void" and str(original["cap"]) == "276.00"
        logged = await (
            await conn.execute(
                """select count(*) as n from collective.events
                where entity_id=%s and event_type='approval_void'""",
                (ids["a"],),
            )
        ).fetchone()
        assert logged["n"] == 1
    again = await client.post(
        f"/v1/businesses/{business(cohort, 'a')}/pools/{pool}/reevaluate",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": attempted.json()["offer"]["evaluation_revision"]},
    )
    assert again.status_code == 200 and again.json()["offer"]["approval_state"] == "void"


async def test_staff_and_other_business_cannot_approve_or_withdraw(api, cohort, auth_headers):
    client, _ = api
    pool = await discovery(client, cohort, auth_headers)
    base = f"/v1/businesses/{business(cohort, 'a')}"
    body = {
        "need_id": str(need(cohort, "a")),
        "pool_id": pool,
        "cap": "276.00",
        "expires_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat(),
        "expected_evaluation_revision": 1,
    }
    for name, status in (("staff", 403), ("owner-b", 404), ("admin", 404)):
        headers = auth_headers(name) | {"Idempotency-Key": str(uuid4())}
        assert (
            await client.post(base + "/approvals", headers=headers, json=body)
        ).status_code == status
        assert (
            await client.post(
                base + f"/pools/{pool}/withdraw", headers=headers, json={"expected_revision": 1}
            )
        ).status_code == status


async def test_same_command_replays_once_and_changed_payload_is_rejected(api, cohort, auth_headers):
    client, database = api
    pool = await discovery(client, cohort, auth_headers)
    expiry, key = (datetime.now(UTC) + timedelta(hours=12)).isoformat(), str(uuid4())
    arguments = dict(key=key, version=1, patch={"expires_at": expiry})
    first = await approve(client, cohort, auth_headers, pool, "a", "276.00", **arguments)
    again = await approve(client, cohort, auth_headers, pool, "a", "276.00", **arguments)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    conflict = await approve(client, cohort, auth_headers, pool, "a", "285.00", **arguments)
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    async with database.transaction("coordinator", None) as conn:
        count = await (
            await conn.execute(
                "select count(*) as n from collective.bound_approvals where pool_id=%s", (pool,)
            )
        ).fetchone()
        assert count["n"] == 1


async def test_racing_approvals_need_one_fresh_review(api, cohort, auth_headers):
    client, database = api
    pool = await discovery(client, cohort, auth_headers)
    results = await asyncio.gather(
        *(
            approve(client, cohort, auth_headers, pool, letter, "276.00", version=1)
            for letter in "ab"
        )
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    async with database.transaction("coordinator", None) as conn:
        count = await (
            await conn.execute(
                "select count(*) as n from collective.bound_approvals where pool_id=%s", (pool,)
            )
        ).fetchone()
        assert count["n"] == 1


async def test_failed_audit_rolls_back_binding_and_shared_state(
    api, cohort, auth_headers, monkeypatch
):
    from collective.repositories import approvals

    client, database = api
    pool = await discovery(client, cohort, auth_headers)

    async def fail(*args):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(approvals, "append_event", fail)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        await approve(client, cohort, auth_headers, pool, "a", "276.00")
    assert (await current(client, cohort, auth_headers, pool, "a"))["evaluation_revision"] == 1
    async with database.transaction("coordinator", None) as conn:
        count = await (
            await conn.execute(
                "select count(*) as n from collective.bound_approvals where pool_id=%s", (pool,)
            )
        ).fetchone()
        assert count["n"] == 0


async def write_quote(conn, cohort, fields):
    await conn.execute(
        """insert into collective.quote_revisions select
        (jsonb_populate_record(null::collective.quote_revisions,to_jsonb(q)||%s)).*
        from collective.quote_revisions q where business_id=%s and id=%s and revision=1""",
        (
            Jsonb(fields | {"sealed": False}),
            business(cohort, "a"),
            cohort(fixture_id("quote-a-b")),
        ),
    )
    await conn.execute(
        """insert into collective.quote_tiers
        select business_id,quote_id,%s,min_quantity,unit_price from collective.quote_tiers
        where business_id=%s and quote_id=%s and quote_revision=1""",
        (fields["revision"], business(cohort, "a"), cohort(fixture_id("quote-a-b"))),
    )
    await conn.execute(
        """update collective.quote_revisions set sealed=true
        where business_id=%s and revision=%s""",
        (business(cohort, "a"), fields["revision"]),
    )


async def revise_quote(cohort, fields):
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await write_quote(conn, cohort, fields)


async def test_missing_terms_withhold_prices_and_restoring_them_cannot_revive_consent(
    api, cohort, auth_headers
):
    client, _ = api
    pool, ids = await ready(client, cohort, auth_headers)
    await revise_quote(cohort, {"revision": 2, "delivery_fee": None})
    result = await approve(client, cohort, auth_headers, pool, "a", "276.00")
    assert result.status_code == 200 and result.json()["status"] == "refresh_required"
    offer = result.json()["offer"]
    assert offer["price_visibility"] == "unavailable" and "decision_delivered_cost" not in offer
    assert offer["approval_state"] == "void" and offer["approval_id"] == ids["a"]
    await revise_quote(cohort, {"revision": 3, "delivery_fee": "15.00"})
    result = await client.post(
        f"/v1/businesses/{business(cohort, 'a')}/pools/{pool}/reevaluate",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": offer["evaluation_revision"]},
    )
    assert result.status_code == 200
    offer = result.json()["offer"]
    assert offer["decision_delivered_cost"] == "255.00" and offer["approval_state"] == "void"
    assert offer["release_state"] == "waiting_for_approvals"


async def test_exact_expiry_removes_one_member_and_reprices_the_remainder(
    api, cohort, auth_headers, monkeypatch
):
    from collective.api import pool_routes

    client, _ = api
    pool = await discovery(client, cohort, auth_headers)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    result = await approve(
        client, cohort, auth_headers, pool, "a", "276.00", patch={"expires_at": expiry.isoformat()}
    )
    assert result.status_code == 200
    for letter in "bc":
        assert (
            await approve(client, cohort, auth_headers, pool, letter, "285.00")
        ).status_code == 200

    class ExactExpiry:
        def now(self):
            return expiry

    monkeypatch.setattr(pool_routes, "SystemClock", ExactExpiry)
    expired = await current(client, cohort, auth_headers, pool, "a")
    assert expired["release_state"] == "ineligible" and "OFFER_EXPIRED" in expired["failure_codes"]
    result = await client.post(
        f"/v1/businesses/{business(cohort, 'b')}/pools/{pool}/reevaluate",
        headers=auth_headers("owner-b") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": expired["evaluation_revision"]},
    )
    assert result.status_code == 200
    b = result.json()["offer"]
    assert b["decision_delivered_cost"] == "285.00" and b["approval_state"] == "valid"
    assert b["release_state"] == "ready" and b["member_count"] == 2
    a = await current(client, cohort, auth_headers, pool, "a")
    assert a["member_state"] == a["approval_state"] == "expired"


async def test_expiry_discovered_by_withdrawal_is_committed(api, cohort, auth_headers, monkeypatch):
    from collective.api import pool_routes

    client, _ = api
    pool = await discovery(client, cohort, auth_headers)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    result = await approve(
        client, cohort, auth_headers, pool, "a", "276.00", patch={"expires_at": expiry.isoformat()}
    )
    assert result.status_code == 200

    class ExactExpiry:
        def now(self):
            return expiry

    monkeypatch.setattr(pool_routes, "SystemClock", ExactExpiry)
    response = await withdraw(client, cohort, auth_headers, pool, "a")
    assert response.status_code == 200 and response.json()["status"] == "refresh_required"
    assert response.json()["offer"]["approval_state"] == "expired"
    a = await current(client, cohort, auth_headers, pool, "a")
    assert a["member_state"] == "expired"
