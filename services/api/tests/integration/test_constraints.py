from uuid import uuid4

import psycopg
import pytest

from collective.repositories.db import Database, set_context


async def test_negative_limit_and_unknown_supplier_are_rejected(db, tenants):
    with pytest.raises(psycopg.errors.CheckViolation):
        async with db.transaction():
            await db.execute(
                "update collective.businesses set staff_limit=-1 where id=%s", (tenants["a"],)
            )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        async with db.transaction():
            await db.execute(
                "insert into collective.business_suppliers (business_id,supplier_id) "
                "values (%s,%s)",
                (tenants["a"], uuid4()),
            )


async def test_membership_policy_is_not_recursive_and_tenant_cannot_escalate(db, tenants):
    await set_context(db, "tenant", tenants["owner_a"])
    cursor = await db.execute("select business_id from collective.business_memberships")
    assert await cursor.fetchall() == [{"business_id": tenants["a"]}]
    cursor = await db.execute("select id from collective.businesses")
    assert await cursor.fetchall() == [{"id": tenants["a"]}]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.transaction():
            await db.execute("update collective.business_memberships set role='owner'")


async def test_real_backend_has_no_superuser_bypass_or_table_ownership(runtime_url):
    database = Database(runtime_url.get_secret_value(), max_size=1)
    await database.open()
    try:
        for subject in (uuid4(), uuid4()):
            async with database.transaction("tenant", subject) as connection:
                cursor = await connection.execute(
                    "select collective_private.subject_id() as subject, current_user as role"
                )
                assert await cursor.fetchone() == {"subject": subject, "role": "collective_tenant"}
        async with database.pool.connection() as connection:
            cursor = await connection.execute(
                "select nullif(current_setting('request.jwt.claims',true),'') as subject, "
                "current_user as role"
            )
            assert await cursor.fetchone() == {"subject": None, "role": "collective_backend"}
        async with database.transaction("coordinator", None) as connection:
            cursor = await connection.execute("select collective_private.subject_id() as subject")
            assert (await cursor.fetchone())["subject"] is None
    finally:
        await database.close()


async def test_public_data_api_roles_have_no_application_schema_access(db):
    cursor = await db.execute("""
        select has_schema_privilege('anon','collective','usage') as anon,
               has_schema_privilege('authenticated','collective','usage') as authenticated
    """)
    assert await cursor.fetchone() == {"anon": False, "authenticated": False}
    cursor = await db.execute(
        "select bool_and(relrowsecurity and relforcerowsecurity) as protected from pg_class c "
        "join pg_namespace n on n.oid=c.relnamespace "
        "where n.nspname='collective' and c.relkind='r'"
    )
    assert (await cursor.fetchone())["protected"] is True
