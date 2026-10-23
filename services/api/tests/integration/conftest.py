import os
import runpy
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import psycopg
import pytest
from dotenv import dotenv_values
from psycopg.rows import dict_row
from pydantic import SecretStr

from collective.main import create_app
from collective.repositories.db import require_local_database
from collective.settings import Settings
from tests.fixtures import ROOT


def local_test_url(name):
    value = os.environ.get(name) or dotenv_values(ROOT / ".env").get(name)
    if not value:
        pytest.fail(f"Set {name} to the disposable local Supabase database")
    return require_local_database(value)


@pytest.fixture
def runtime_url():
    return SecretStr(local_test_url("TEST_DATABASE_URL"))


@pytest.fixture
async def db():
    connection = await psycopg.AsyncConnection.connect(
        local_test_url("MIGRATION_DATABASE_URL"),
        autocommit=True,
        row_factory=dict_row,
    )
    async with connection:
        async with connection.transaction(force_rollback=True):
            yield connection


@pytest.fixture
async def tenants(db):
    identities = {name: uuid4() for name in ("a", "b", "owner_a", "owner_b", "staff")}
    for letter in "ab":
        await db.execute(
            "insert into collective.businesses (id,name,zone,locale) values (%s,%s,'sepang','en')",
            (identities[letter], "Test " + letter),
        )
        await db.execute(
            "insert into auth.users (id) values (%s)", (identities["owner_" + letter],)
        )
        await db.execute(
            "insert into collective.business_memberships values (%s,%s,'owner')",
            (identities[letter], identities["owner_" + letter]),
        )
    await db.execute("insert into auth.users (id) values (%s)", (identities["staff"],))
    await db.execute(
        "insert into collective.business_memberships values (%s,%s,'staff')",
        (identities["a"], identities["staff"]),
    )
    return identities


@pytest.fixture(scope="session")
def access_seed():
    # Isolate persistent HTTP/Storage fixtures from the walkthrough's active needs.
    seed = runpy.run_path(str(ROOT / "scripts/seed_demo.py"))["seed"]
    run = "access-" + uuid4().hex[:8]
    first = seed(run)
    repeated = seed(run)
    assert first["anchor"] == repeated["anchor"]
    assert {k: v["id"] for k, v in first["accounts"].items()} == {
        k: v["id"] for k, v in repeated["accounts"].items()
    }
    settings = Settings(_env_file=ROOT / ".env")
    tokens = {}
    refresh_tokens = {}
    with httpx.Client(base_url=str(settings.supabase_url), timeout=10) as client:
        for name, account in first["accounts"].items():
            response = client.post(
                "/auth/v1/token?grant_type=password",
                headers={"apikey": settings.supabase_publishable_key},
                json={"email": account["email"], "password": account["password"]},
            )
            assert response.status_code == 200, "Local seeded sign-in failed"
            tokens[name] = SecretStr(response.json()["access_token"])
            refresh_tokens[name] = SecretStr(response.json()["refresh_token"])
    return {
        "run": run,
        "anchor": first["anchor"],
        "accounts": {k: {"id": v["id"], "email": v["email"]} for k, v in first["accounts"].items()},
        "tokens": tokens,
        "refresh_tokens": refresh_tokens,
    }


@pytest.fixture
def access_id(access_seed):
    return lambda value: uuid5(NAMESPACE_URL, f"orbyte-run:{access_seed['run']}:{UUID(str(value))}")


@pytest.fixture
async def api(access_seed):
    app = create_app(Settings(_env_file=ROOT / ".env", enable_worker=False))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://collective.test"
        ) as client:
            yield client, app.state.database


@pytest.fixture
def auth_headers(access_seed):
    return lambda name: {
        "Authorization": "Bearer " + access_seed["tokens"][name].get_secret_value()
    }
