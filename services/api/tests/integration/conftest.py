import os
from uuid import uuid4

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg.rows import dict_row
from pydantic import SecretStr

from collective.repositories.db import require_local_database
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
