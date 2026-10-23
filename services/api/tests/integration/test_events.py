from uuid import uuid4

import psycopg
import pytest

from collective.repositories.db import set_context
from collective.repositories.events import Event, append_event


def event(business):
    return Event(
        business_id=business,
        actor_id=None,
        actor_kind="system",
        event_type="test.recorded",
        entity_id=uuid4(),
        correlation_id=uuid4(),
        summary="Synthetic integration check",
    )


async def test_coordinator_can_append_but_cannot_update_or_delete(db, tenants):
    await set_context(db, "coordinator", None)
    event_id = await append_event(db, event(tenants["a"]))
    for command in (
        "update collective.events set summary='changed' where id=%s",
        "delete from collective.events where id=%s",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with db.transaction():
                await db.execute(command, (event_id,))


async def test_event_rolls_back_with_its_mutation(db, tenants):
    await set_context(db, "coordinator", None)
    with pytest.raises(RuntimeError, match="abort mutation"):
        async with db.transaction():
            event_id = await append_event(db, event(tenants["a"]))
            raise RuntimeError("abort mutation")
    cursor = await db.execute(
        "select count(*) as count from collective.events where id=%s", (event_id,)
    )
    assert (await cursor.fetchone())["count"] == 0


async def test_event_is_immutable_even_for_operator(db, tenants):
    event_id = await append_event(db, event(tenants["a"]))
    with pytest.raises(psycopg.errors.CheckViolation, match="Immutable record"):
        async with db.transaction():
            await db.execute(
                "update collective.events set summary='changed' where id=%s", (event_id,)
            )


async def test_tenant_does_not_see_other_business_event(db, tenants):
    event_id = await append_event(db, event(tenants["b"]))
    await set_context(db, "tenant", tenants["owner_a"])
    cursor = await db.execute("select * from collective.events where id=%s", (event_id,))
    assert await cursor.fetchone() is None
