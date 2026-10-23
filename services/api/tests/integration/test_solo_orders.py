import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from tests.fixtures import fixture_id
from tests.integration.scenarios import clone_business_data
from tests.integration.test_pool_commands import business, need, revise_quote


@pytest.fixture
async def cohort(access_seed):
    return await clone_business_data(access_seed, "a", pool_opt_in=False, include_history=False)


async def approve(client, cohort, auth_headers, *, patch=None, key=None):
    return await client.post(
        f"/v1/businesses/{business(cohort, 'a')}/approvals",
        headers=auth_headers("owner-a") | {"Idempotency-Key": key or str(uuid4())},
        json={
            "need_id": str(need(cohort, "a")),
            "pool_id": None,
            "cap": "315.00",
            "expires_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat(),
            "expected_evaluation_revision": 1,
            "quote_id": str(cohort(fixture_id("quote-a-b"))),
            "expected_quote_revision": 1,
            "reviewed_decision_cost": "315.00",
            **(patch or {}),
        },
    )


async def execute(client, cohort, auth_headers, approval, *, key=None):
    return await client.post(
        f"/v1/businesses/{business(cohort, 'a')}/needs/{need(cohort, 'a')}/release",
        headers=auth_headers("owner-a") | {"Idempotency-Key": key or str(uuid4())},
        json={"approval_id": approval, "expected_revision": 1},
    )


async def test_day_one_solo_needs_no_history_or_pool_opt_in(api, cohort, auth_headers):
    client, database = api
    root = f"/v1/businesses/{business(cohort, 'a')}"
    before = await client.get(
        root + f"/needs/{need(cohort, 'a')}/comparisons", headers=auth_headers("owner-a")
    )
    assert before.json()["historical_comparable_cost"] is None
    approved = await approve(client, cohort, auth_headers)
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert approved.json()["need"]["approval_state"] == "valid" and approved.json()["offer"] is None
    approval = approved.json()["approval_id"]
    no_order = await client.get(
        root + f"/needs/{need(cohort, 'a')}/order", headers=auth_headers("owner-a")
    )
    assert no_order.status_code == 404
    key = str(uuid4())
    results = await asyncio.gather(
        *(execute(client, cohort, auth_headers, approval, key=key) for _ in range(2))
    )
    assert all(r.status_code == 200 and r.json()["state"] == "released" for r in results)
    assert results[0].json() == results[1].json()
    order = results[0].json()["order"]
    assert order["amount"] == order["decision_amount"] == "315.00"
    assert order["terms"]["quantity"] == "1000.000000"
    assert order["implementation"] == "simulated"
    assert (await execute(client, cohort, auth_headers, approval)).json() == results[0].json()
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select count(*) as n from collective.purchase_orders where business_id=%s",
                (business(cohort, "a"),),
            )
        ).fetchone()
        assert row["n"] == 1


@pytest.mark.parametrize(
    "patch,status,code",
    [
        ({"expected_evaluation_revision": 2}, 409, "STALE_REVISION"),
        ({"expected_quote_revision": 2}, 409, "STALE_REVISION"),
        ({"reviewed_decision_cost": "300.00"}, 409, "STALE_REVISION"),
        ({"cap": "314.99"}, 422, "CAP_EXCEEDED"),
        ({"owner_id": str(uuid4())}, 422, "INVALID_REQUEST"),
        ({"quantity": "2000"}, 422, "INVALID_REQUEST"),
    ],
)
async def test_solo_stale_or_forged_terms_cannot_create_permission(
    api, cohort, auth_headers, patch, status, code
):
    client, database = api
    response = await approve(client, cohort, auth_headers, patch=patch)
    assert response.status_code == status and response.json()["code"] == code
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select count(*) as n from collective.bound_approvals where business_id=%s",
                (business(cohort, "a"),),
            )
        ).fetchone()
        assert row["n"] == 0


async def test_cheaper_changed_terms_void_and_fresh_solo_consent_uses_new_record(
    api, cohort, auth_headers
):
    client, _ = api
    approved = await approve(client, cohort, auth_headers)
    old = approved.json()["approval_id"]
    await revise_quote(cohort, {"revision": 2, "delivery_fee": "0.00"})
    blocked = await execute(client, cohort, auth_headers, old)
    assert blocked.status_code == 200 and blocked.json()["state"] == "awaiting_approvals"
    assert blocked.json()["need"]["approval_state"] == "void"
    fresh = await approve(
        client,
        cohort,
        auth_headers,
        patch={"expected_quote_revision": 2, "reviewed_decision_cost": "300.00", "cap": "300.00"},
    )
    assert fresh.status_code == 200 and fresh.json()["approval_id"] != old
    issued = await execute(client, cohort, auth_headers, fresh.json()["approval_id"])
    assert issued.json()["order"]["amount"] == "300.00"
    original = await client.get("/v1/hub/approvals/" + old, headers=auth_headers("owner-a"))
    assert original.json()["cap"] == "315.00" and original.json()["state"] == "void"


async def test_solo_permission_can_be_withdrawn_but_not_reused(api, cohort, auth_headers):
    client, _ = api
    approval = (await approve(client, cohort, auth_headers)).json()["approval_id"]
    response = await client.post(
        f"/v1/businesses/{business(cohort, 'a')}/needs/{need(cohort, 'a')}/withdraw",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json={"expected_revision": 1, "approval_id": approval},
    )
    assert response.status_code == 200 and response.json()["need"]["approval_state"] == "withdrawn"
    blocked = await execute(client, cohort, auth_headers, approval)
    assert blocked.status_code == 200 and blocked.json()["state"] == "awaiting_approvals"
    assert blocked.json()["order"] is None


async def test_staff_and_other_owner_cannot_execute_solo(api, cohort, auth_headers):
    client, _ = api
    approval = (await approve(client, cohort, auth_headers)).json()["approval_id"]
    for name, status in (("staff", 403), ("owner-b", 404), ("admin", 404)):
        response = await client.post(
            f"/v1/businesses/{business(cohort, 'a')}/needs/{need(cohort, 'a')}/release",
            headers=auth_headers(name) | {"Idempotency-Key": str(uuid4())},
            json={"approval_id": approval, "expected_revision": 1},
        )
        assert response.status_code == status
