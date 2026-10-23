from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from collective.domain.approvals import BoundTerms
from tests.fixtures import fixture_id, scenario
from tests.integration.conftest import local_test_url


@pytest.fixture
async def hub_approval(access_seed, access_id):
    data = scenario()
    business = data["businesses"][0]
    now = datetime.now(UTC)
    terms = BoundTerms(
        need_id=access_id(business["need_id"]),
        need_revision=1,
        supplier_id=access_id(data["suppliers"][0]["id"]),
        spec_id=access_id(data["spec"]["id"]),
        spec_revision=1,
        quantity="1000",
        base_unit="piece",
        delivery_mode="per_business",
        delivery_location=None,
        delivery_window={"start": now + timedelta(days=4), "end": now + timedelta(days=5)},
        credit_days=30,
        conditions_fingerprint="f" * 64,
    )
    approval_id = uuid4()
    url = SecretStr(local_test_url("MIGRATION_DATABASE_URL"))
    async with await AsyncConnection.connect(url.get_secret_value()) as conn:
        await conn.execute(
            """insert into collective.bound_approvals
            (id,business_id,need_id,owner_id,terms,cap,expires_at,approved_at,
             evaluation_revision,quote_id,quote_revision)
            values (%s,%s,%s,%s,%s,315,%s,%s,1,%s,1)""",
            (
                approval_id,
                access_id(business["id"]),
                terms.need_id,
                UUID(access_seed["accounts"]["owner-a"]["id"]),
                Jsonb(terms.model_dump(mode="json")),
                now + timedelta(hours=1),
                now,
                access_id(fixture_id("quote-a-a")),
            ),
        )
    yield approval_id
    async with await AsyncConnection.connect(url.get_secret_value()) as conn:
        await conn.execute(
            "update collective.bound_approvals set state='void',revision=revision+1 where id=%s",
            (approval_id,),
        )


async def test_mock_hub_uses_verified_identity_and_real_assignments(api, auth_headers):
    client, _ = api
    for name in ("owner-a", "staff", "admin"):
        actual = (await client.get("/v1/me", headers=auth_headers(name))).json()
        for path in ("identity", "roles"):
            hub = await client.get("/v1/hub/" + path, headers=auth_headers(name))
            assert hub.status_code == 200
            assert hub.json() == actual | {"implementation": "mock"}
    services = await client.get("/v1/hub/services", headers=auth_headers("owner-a"))
    assert services.json() == [
        {
            "name": "collective",
            "description": "Restocking workspace and human approvals.",
            "implementation": "mock",
        }
    ]
    detail = await client.get("/v1/hub/services/collective", headers=auth_headers("owner-a"))
    assert detail.json() == services.json()[0]
    assert (
        await client.get("/v1/hub/services/unknown", headers=auth_headers("owner-a"))
    ).status_code == 404
    assert (await client.get("/v1/hub/identity")).status_code == 401


async def test_approval_reference_is_real_scoped_and_price_filtered(
    api, auth_headers, hub_approval
):
    client, _ = api
    path = "/v1/hub/approvals/" + str(hub_approval)
    owner = await client.get(path, headers=auth_headers("owner-a"))
    assert owner.status_code == 200
    assert owner.json()["id"] == str(hub_approval)
    assert owner.json()["cap"] == "315.00"
    assert owner.json()["price_visibility"] == "visible"
    staff = await client.get(path, headers=auth_headers("staff"))
    assert staff.status_code == 200
    assert staff.json()["price_visibility"] == "hidden"
    assert "cap" not in staff.json() and "terms" not in staff.json()
    for name in ("owner-b", "admin"):
        other = await client.get(path, headers=auth_headers(name))
        assert other.status_code == 404
        assert other.json()["code"] == "NOT_FOUND"
    absent = await client.get("/v1/hub/approvals/" + str(uuid4()), headers=auth_headers("owner-a"))
    assert absent.status_code == 404
