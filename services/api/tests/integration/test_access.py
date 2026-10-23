from uuid import UUID

import httpx
import pytest
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from pydantic import SecretStr

from collective.api.errors import DomainError
from collective.services.permissions import membership
from collective.settings import Settings
from tests.fixtures import ROOT, fixture_id, scenario
from tests.integration.conftest import local_test_url


async def test_actual_jwt_and_membership_ignore_editable_metadata(api, access_seed, auth_headers):
    client, database = api
    owner = await client.get("/v1/me", headers=auth_headers("owner-a"))
    assert owner.status_code == 200
    assert owner.headers["cache-control"] == "no-store"
    assert owner.json()["subject"] == access_seed["accounts"]["owner-a"]["id"]
    assert [m["role"] for m in owner.json()["memberships"]] == ["owner"]
    staff = await client.get("/v1/me", headers=auth_headers("staff"))
    assert [m["role"] for m in staff.json()["memberships"]] == ["staff"]
    assert staff.json()["memberships"][0]["can_view_prices"] is False

    settings = Settings(_env_file=ROOT / ".env")
    async with httpx.AsyncClient(base_url=str(settings.supabase_url)) as auth:
        edited = await auth.put(
            "/auth/v1/user",
            headers=auth_headers("staff") | {"apikey": settings.supabase_publishable_key},
            json={
                "data": {
                    "role": "owner",
                    "business_id": owner.json()["memberships"][0]["business_id"],
                }
            },
        )
        assert edited.status_code == 200
        refreshed = await auth.post(
            "/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": settings.supabase_publishable_key},
            json={"refresh_token": access_seed["refresh_tokens"]["staff"].get_secret_value()},
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["user"]["user_metadata"]["role"] == "owner"
        forged_role_token = SecretStr(refreshed.json()["access_token"])
    after = await client.get(
        "/v1/me", headers={"Authorization": "Bearer " + forged_role_token.get_secret_value()}
    )
    assert after.json() == staff.json()
    async with database.transaction("tenant", UUID(access_seed["accounts"]["staff"]["id"])) as conn:
        with pytest.raises(DomainError, match="requires the business owner") as denied:
            await membership(
                conn,
                UUID(access_seed["accounts"]["staff"]["id"]),
                UUID(staff.json()["memberships"][0]["business_id"]),
                owner=True,
            )
        assert denied.value.status == 403


async def test_missing_and_forged_tokens_fail(api, auth_headers):
    client, _ = api
    for headers in ({}, {"Authorization": "Bearer invalid"}):
        response = await client.get("/v1/me", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHENTICATED"
    header = auth_headers("owner-a")["Authorization"]
    parts = header.split(" ")[1].split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    forged = await client.get("/v1/me", headers={"Authorization": "Bearer " + ".".join(parts)})
    assert forged.status_code == 401


async def test_storage_denies_other_business_staff_and_anonymous(
    access_seed, access_id, auth_headers
):
    settings = Settings(_env_file=ROOT / ".env")
    business_id = access_id(scenario()["businesses"][0]["id"])
    source_id = access_id(fixture_id("source-a"))
    key = f"collective-sources/{business_id}/{source_id}/original.png"
    async with httpx.AsyncClient(
        base_url=str(settings.supabase_url),
        headers={"apikey": settings.supabase_publishable_key},
    ) as client:
        permitted = await client.get(
            "/storage/v1/object/authenticated/" + key, headers=auth_headers("owner-a")
        )
        assert permitted.status_code == 200
        assert permitted.content.startswith(b"\x89PNG")
        public = await client.get("/storage/v1/object/public/" + key)
        assert public.status_code in {400, 401, 403, 404}
        for name in ("owner-b", "staff", "admin", None):
            headers = auth_headers(name) if name else {}
            response = await client.get("/storage/v1/object/authenticated/" + key, headers=headers)
            assert response.status_code in {400, 401, 403, 404}
            assert not response.content.startswith(b"\x89PNG")
            signed = await client.post(
                "/storage/v1/object/sign/" + key, headers=headers, json={"expiresIn": 30}
            )
            assert signed.status_code in {400, 401, 403, 404}
        overwrite = await client.put(
            "/storage/v1/object/" + key, headers=auth_headers("owner-a"), content=b"changed"
        )
        assert overwrite.status_code in {400, 401, 403, 404}
        exposed = await client.get(
            "/rest/v1/purchases?select=*",
            headers=auth_headers("owner-a") | {"Accept-Profile": "collective"},
        )
        assert exposed.status_code in {400, 403, 404, 406}


async def test_auth_allows_seeded_email_sign_in_but_disables_registration(access_seed):
    settings = Settings(_env_file=ROOT / ".env")
    async with httpx.AsyncClient(base_url=str(settings.supabase_url)) as client:
        response = await client.get(
            "/auth/v1/settings", headers={"apikey": settings.supabase_publishable_key}
        )
    assert response.status_code == 200
    assert response.json()["disable_signup"] is True
    assert response.json()["external"]["email"] is True


async def test_recycled_runtime_connection_does_not_retain_another_subject(api, access_seed):
    _, database = api
    for name in ("owner-a", "owner-b", "staff", "owner-a"):
        subject = UUID(access_seed["accounts"][name]["id"])
        async with database.transaction("tenant", subject) as conn:
            row = await (
                await conn.execute(
                    """select current_user as role,user_id
                    from collective.business_memberships"""
                )
            ).fetchone()
            assert row["role"] == "collective_tenant"
            assert row["user_id"] == subject
    async with database.transaction("coordinator", None) as conn:
        row = await (
            await conn.execute("select collective_private.subject_id() as subject")
        ).fetchone()
        assert row["subject"] is None


async def test_request_recovers_an_idle_connection_closed_by_postgres(api, auth_headers):
    client, database = api
    async with database.pool.connection() as conn:
        row = await (await conn.execute("select pg_backend_pid() as pid")).fetchone()
    operator = SecretStr(local_test_url("MIGRATION_DATABASE_URL"))
    async with await AsyncConnection.connect(operator.get_secret_value()) as conn:
        await conn.execute("select pg_terminate_backend(%s)", (row["pid"],))
    response = await client.get("/v1/me", headers=auth_headers("owner-a"))
    assert response.status_code == 200


async def test_repeated_seed_has_one_accepted_copy(access_seed, access_id):
    business_ids = [access_id(b["id"]) for b in scenario()["businesses"]]
    # Only the disposable operator can inspect cross-tenant fixture counts.
    url = SecretStr(local_test_url("MIGRATION_DATABASE_URL"))
    async with await AsyncConnection.connect(url.get_secret_value(), row_factory=dict_row) as conn:
        for table, expected in (
            ("purchases", 3),
            ("quote_revisions", 6),
            ("sources", 3),
            ("reorder_needs", 3),
        ):
            result = await conn.execute(
                sql.SQL("select count(*) as n from collective.{} where business_id=any(%s)").format(
                    sql.Identifier(table)
                ),
                (business_ids,),
            )
            assert (await result.fetchone())["n"] == expected
