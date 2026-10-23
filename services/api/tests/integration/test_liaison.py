import asyncio
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from tests.fixtures import scenario
from tests.integration.scenarios import clone_business_data


@pytest.fixture
async def draft(api, access_seed):
    _, database = api
    remap = await clone_business_data(access_seed, "a", pool_opt_in=False)
    business = remap(scenario()["businesses"][0]["id"])
    identity, conversation = uuid4(), uuid4()
    async with database.transaction("coordinator", None) as conn:
        await conn.execute(
            """insert into collective.conversations(id,business_id,supplier_id)
            values (%s,%s,%s)""",
            (conversation, business, scenario()["suppliers"][0]["id"]),
        )
        await conn.execute(
            """insert into collective.message_drafts
            (id,business_id,conversation_id,kind,text_content,recipient,input_context)
            values (%s,%s,%s,'rfq',%s,%s,%s)""",
            (
                identity,
                business,
                conversation,
                "Please quote 1000 pieces, including delivery and tax.",
                "Supplier A (simulated)",
                Jsonb({"provenance": "controlled test context"}),
            ),
        )
    return business, identity


async def saved(client, draft, auth_headers, actor="owner-a"):
    business, identity = draft
    return await client.get(
        f"/v1/businesses/{business}/drafts/{identity}", headers=auth_headers(actor)
    )


def approval(view):
    return {
        "expected_revision": view["revision"],
        "approved_hash": view["approved_hash"],
        "recipient": view["recipient"],
    }


async def send(client, draft, auth_headers, body, *, key=None, actor="owner-a"):
    business, identity = draft
    return await client.post(
        f"/v1/businesses/{business}/drafts/{identity}/send",
        headers=auth_headers(actor) | {"Idempotency-Key": key or str(uuid4())},
        json=body,
    )


async def test_exact_owner_send_is_simulated_and_deduplicated(api, draft, auth_headers):
    client, database = api
    view = (await saved(client, draft, auth_headers)).json()
    body, key = approval(view), str(uuid4())
    first = await send(client, draft, auth_headers, body, key=key)
    assert first.status_code == 200 and first.json()["simulated"] is True
    assert (await send(client, draft, auth_headers, body, key=key)).json() == first.json()
    assert (
        await send(client, draft, auth_headers, body | {"recipient": "changed"}, key=key)
    ).status_code == 409
    assert (await send(client, draft, auth_headers, body)).json() == first.json()
    assert (await saved(client, draft, auth_headers)).json()["status"] == "sent_simulated"
    business, identity = draft
    async with database.transaction("coordinator", None) as conn:
        rows = await (
            await conn.execute(
                "select * from collective.messages where business_id=%s and draft_id=%s",
                (business, identity),
            )
        ).fetchall()
        assert len(rows) == 1 and rows[0]["text_content"] == view["text_content"]
        assert rows[0]["recipient"] == view["recipient"]
        assert rows[0]["direction"] == "outbound_simulated"


async def test_altered_or_stale_send_requires_fresh_review(api, draft, auth_headers):
    client, _ = api
    view = (await saved(client, draft, auth_headers)).json()
    body = approval(view)
    for changed in (
        {"approved_hash": "0" * 64},
        {"recipient": "different supplier"},
        {"expected_revision": 2},
        {"text_content": "send this instead"},
    ):
        assert (await send(client, draft, auth_headers, body | changed)).status_code in {409, 422}
    business, identity = draft
    edit = {
        "expected_revision": 1,
        "recipient": "Supplier B (simulated)",
        "text_content": "Revised exact owner text.",
    }
    result = await client.patch(
        f"/v1/businesses/{business}/drafts/{identity}",
        headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
        json=edit,
    )
    assert result.status_code == 200 and result.json()["revision"] == 2
    assert (await send(client, draft, auth_headers, body)).status_code == 409
    assert (await send(client, draft, auth_headers, approval(result.json()))).status_code == 200
    assert (
        await client.patch(
            f"/v1/businesses/{business}/drafts/{identity}",
            headers=auth_headers("owner-a") | {"Idempotency-Key": str(uuid4())},
            json=edit | {"expected_revision": 3},
        )
    ).status_code == 409


async def test_draft_prices_and_send_are_protected(api, draft, auth_headers):
    client, _ = api
    view = (await saved(client, draft, auth_headers)).json()
    staff = await saved(client, draft, auth_headers, "staff")
    assert staff.status_code == 200
    assert staff.json()["price_visibility"] == "hidden"
    assert all(key not in staff.json() for key in ("text_content", "recipient", "approved_hash"))
    for actor, status in (("staff", 403), ("owner-b", 404), ("admin", 404)):
        assert (
            await send(client, draft, auth_headers, approval(view), actor=actor)
        ).status_code == status
        if actor != "staff":
            assert (await saved(client, draft, auth_headers, actor)).status_code == 404


async def test_racing_sends_have_one_receipt(api, draft, auth_headers):
    client, database = api
    view = (await saved(client, draft, auth_headers)).json()
    responses = await asyncio.gather(
        *(send(client, draft, auth_headers, approval(view)) for _ in range(2))
    )
    assert all(result.status_code == 200 for result in responses)
    assert responses[0].json() == responses[1].json()
    async with database.transaction("coordinator", None) as conn:
        assert (
            await conn.execute("select id from collective.messages where draft_id=%s", (draft[1],))
        ).rowcount == 1


async def test_audit_failure_rolls_back_the_simulated_send(api, draft, auth_headers, monkeypatch):
    from collective.services import drafts

    client, database = api
    view = (await saved(client, draft, auth_headers)).json()

    async def fail(*args, **kwargs):
        raise RuntimeError("Injected audit failure")

    monkeypatch.setattr(drafts, "append_event", fail)
    with pytest.raises(RuntimeError, match="Injected"):
        await send(client, draft, auth_headers, approval(view))
    assert (await saved(client, draft, auth_headers)).json()["status"] == "draft"
    async with database.transaction("coordinator", None) as conn:
        assert not (
            await conn.execute("select id from collective.messages where draft_id=%s", (draft[1],))
        ).rowcount
