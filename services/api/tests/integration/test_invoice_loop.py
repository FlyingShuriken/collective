import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg import errors

from collective.jobs.runner import Runner
from collective.repositories import jobs
from collective.services import invoice_checks
from collective.settings import Settings
from tests.fixtures import ROOT
from tests.integration.scenarios import clone_business_data
from tests.integration.test_intake_confirmation import (
    confirm,
    edited,
    purchase_proposal,
)
from tests.integration.test_liaison_jobs import (
    command,
    draft_cache,
    source,
)
from tests.integration.test_liaison_jobs import (
    run as run_liaison,
)
from tests.integration.test_pool_commands import approve, business, ready, withdraw
from tests.integration.test_release_concurrency import release


@pytest.fixture
async def context(api, access_seed, auth_headers):
    client, database = api
    cohort = await clone_business_data(access_seed, "abc", pool_opt_in=True)
    pool, _ = await ready(client, cohort, auth_headers)
    await withdraw(client, cohort, auth_headers, pool, "c")
    await approve(client, cohort, auth_headers, pool, "a", "285.00")
    response = await release(client, cohort, auth_headers, pool)
    assert response.status_code == 200, response.text
    order = response.json()["order"]
    assert order["amount"] == "285.00"
    return {
        "client": client,
        "database": database,
        "business": business(cohort, "a"),
        "root": f"/v1/businesses/{business(cohort, 'a')}",
        "auth": auth_headers,
        "settings": Settings(_env_file=ROOT / ".env", enable_worker=False),
        "order": order,
        "other_business": business(cohort, "b"),
    }


async def accepted(ctx, *, mapped=True, purpose="delivered"):
    value, _, proposed = await purchase_proposal(ctx)
    proposed = await edited(
        ctx,
        proposed,
        mapped=mapped,
        record_context=proposed["context"] | {"purpose": purpose, "line_mappings": []}
        if purpose != "delivered"
        else None,
    )
    response = await confirm(ctx, proposed)
    assert response.status_code == 200, response.text
    return value, response.json()


async def run(ctx, identity):
    async with ctx["database"].transaction("coordinator", None) as conn:
        await conn.execute(
            "update collective.jobs set next_attempt_at='1900-01-01' where id=%s", (identity,)
        )

    async def handle(db, job, clock):
        assert str(job["id"]) == str(identity)
        await invoice_checks.handle(db, job, clock)

    assert await Runner(ctx["database"], handlers={"invoice_check": handle}).run_once()
    return (
        await ctx["client"].get(ctx["root"] + f"/jobs/{identity}", headers=ctx["auth"]("owner-a"))
    ).json()


async def check(ctx, current):
    body = {
        "purchase_id": current["record_id"],
        "purchase_revision": current["record_revision"],
        "po_id": ctx["order"]["id"],
    }
    response = await command(ctx, "/invoice-checks", body)
    assert response.status_code == 200, response.text
    job = await run(ctx, response.json()["id"])
    assert job["status"] == "succeeded", job
    response = await ctx["client"].get(
        ctx["root"] + f"/invoice-checks/{job['result_reference']}", headers=ctx["auth"]("owner-a")
    )
    assert response.status_code == 200, response.text
    return response.json(), body


async def test_final_issued_tier_is_checked_after_confirmation_and_unmatched_stays_countable(
    context, tmp_path
):
    ctx = context
    value, current = await accepted(ctx)
    async with ctx["database"].transaction("coordinator", None) as conn:
        automatic = await (
            await conn.execute(
                """select * from collective.jobs where business_id=%s and kind='invoice_check'
            and payload->>'purchase_id'=%s""",
                (ctx["business"], current["record_id"]),
            )
        ).fetchone()
        assert automatic is not None and automatic["payload"]["po_id"] is None
    auto = await run(ctx, automatic["id"])
    unmatched = await ctx["client"].get(
        ctx["root"] + f"/invoice-checks/{auto['result_reference']}", headers=ctx["auth"]("owner-a")
    )
    assert (
        unmatched.json()["status"] == "unmatched"
        and unmatched.json()["result"]["total_variance"] is None
    )
    matched, body = await check(ctx, current)
    assert matched["status"] == "flagged" and matched["result"]["total_variance"] == "5.00"
    assert matched["review_status"] == "pending" and matched["recovery_amount"] is None
    assert "discount" in matched["result"]["missing_fields"]
    lines = [item for item in matched["result"]["differences"] if item["kind"] == "unit_price"]
    assert Decimal(lines[0]["agreed"]) == Decimal("0.270")
    assert Decimal(lines[0]["invoiced"]) == Decimal("0.275")
    assert matched["source_id"] == value["id"]
    repeated = await command(ctx, "/invoice-checks", body)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "succeeded"
    assert repeated.json()["result_reference"] == matched["id"]
    page = await ctx["client"].get(
        ctx["root"] + f"/invoice-checks?purchase_id={current['record_id']}",
        headers=ctx["auth"]("owner-a"),
    )
    assert {item["status"] for item in page.json()["items"]} == {"unmatched", "flagged"}
    options = await ctx["client"].get(
        ctx["root"]
        + f"/invoice-checks/order-options?purchase_id={current['record_id']}&purchase_revision=1",
        headers=ctx["auth"]("owner-a"),
    )
    assert [item["id"] for item in options.json()["orders"]] == [ctx["order"]["id"]]
    request = {
        "kind": "invoice_query",
        "reference_id": matched["id"],
        "expected_revision": 1,
        "supplier_id": matched["supplier_id"],
    }
    response = await command(ctx, "/drafts", request)
    assert response.status_code == 200, response.text
    gateway, facts = await draft_cache(ctx, response.json(), tmp_path)
    assert (
        facts["issued_terms"]["supplier_id"] == matched["supplier_id"]
        and "recovery_amount" not in facts
    )
    completed = await run_liaison(ctx, response.json()["id"], gateway=gateway)
    assert completed["status"] == "succeeded", completed
    draft = (
        await ctx["client"].get(
            ctx["root"] + f"/drafts/{completed['result_reference']}", headers=ctx["auth"]("owner-a")
        )
    ).json()
    assert draft["status"] == "draft"
    async with ctx["database"].transaction("coordinator", None) as conn:
        assert not (
            await conn.execute(
                "select id from collective.messages where business_id=%s", (ctx["business"],)
            )
        ).rowcount
        saved = await (
            await conn.execute(
                "select input_snapshot from collective.invoice_checks where id=%s", (matched["id"],)
            )
        ).fetchone()
        assert saved["input_snapshot"]["order"]["cost"]["tier_min_quantity"] == "2000.000000"
    sent = await command(
        ctx,
        f"/drafts/{draft['id']}/send",
        {
            "expected_revision": draft["revision"],
            "recipient": draft["recipient"],
            "approved_hash": draft["approved_hash"],
        },
    )
    assert sent.status_code == 200 and sent.json()["simulated"]
    unchanged = await ctx["client"].get(
        ctx["root"] + f"/invoice-checks/{matched['id']}", headers=ctx["auth"]("owner-a")
    )
    assert unchanged.json()["recovery_amount"] is None


async def test_price_privacy_cross_tenant_references_and_staff_review_boundaries(context):
    ctx = context
    _, current = await accepted(ctx)
    checked, body = await check(ctx, current)
    for actor, status in (("staff", 200), ("owner-b", 404), ("admin", 404)):
        response = await ctx["client"].get(
            ctx["root"] + f"/invoice-checks/{checked['id']}", headers=ctx["auth"](actor)
        )
        assert response.status_code == status
        if actor == "staff":
            assert (
                response.json()["price_visibility"] == "hidden" and "result" not in response.json()
            )
    assert (await command(ctx, "/invoice-checks", body, actor="staff")).status_code == 403
    assert (
        await command(
            ctx,
            f"/invoice-checks/{checked['id']}/review",
            {"expected_revision": 1, "action": "confirm", "confirmed_amount": "5.00"},
            actor="staff",
        )
    ).status_code == 403
    other = await ctx["client"].get(
        f"/v1/businesses/{ctx['other_business']}/ledger?kind=po", headers=ctx["auth"]("owner-b")
    )
    assert (
        await command(ctx, "/invoice-checks", body | {"po_id": other.json()["items"][0]["id"]})
    ).status_code == 404


async def test_recovery_requires_separate_document_and_never_follows_a_query_or_flag(context):
    ctx = context
    value, current = await accepted(ctx)
    checked, _ = await check(ctx, current)
    path = f"/invoice-checks/{checked['id']}/review"
    proof = await source(
        ctx, text="SYNTHETIC Supplier B credit note CN-001: RM5.00 correction for DEMO-B-001."
    )
    resolution = {
        "kind": "credit_note",
        "source_id": proof["id"],
        "source_sha256": proof["sha256"],
        "document_reference": "CN-001",
        "amount": "5.00",
    }
    assert (
        await command(
            ctx, path, {"expected_revision": 1, "action": "recover", "resolution": resolution}
        )
    ).status_code == 422
    assert (
        await command(
            ctx, path, {"expected_revision": 1, "action": "confirm", "confirmed_amount": "5.01"}
        )
    ).status_code == 422
    confirmed = await command(
        ctx, path, {"expected_revision": 1, "action": "confirm", "confirmed_amount": "5.00"}
    )
    assert confirmed.status_code == 200 and confirmed.json()["recovery_amount"] is None
    for invalid in [
        resolution | {"source_id": value["id"], "source_sha256": value["sha256"]},
        resolution | {"source_sha256": "0" * 64},
        resolution | {"amount": "5.01"},
    ]:
        assert (
            await command(
                ctx, path, {"expected_revision": 2, "action": "recover", "resolution": invalid}
            )
        ).status_code == 422
    body = {"expected_revision": 2, "action": "recover", "resolution": resolution}
    key = str(uuid4())
    responses = await asyncio.gather(*(command(ctx, path, body, key=key) for _ in range(2)))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["recovery_amount"] == "5.00" and responses[0].json()["revision"] == 3
    assert (await command(ctx, path, body)).status_code == 409
    async with ctx["database"].transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select history from collective.invoice_checks where id=%s", (checked["id"],)
            )
        ).fetchone()
        assert row["history"]["1"]["review_status"] == "pending"
        assert row["history"]["2"]["recovery_amount"] is None


async def test_unmapped_invoice_cannot_pass_or_be_reviewed_as_recovered(context):
    ctx = context
    _, current = await accepted(ctx, mapped=False)
    checked, _ = await check(ctx, current)
    assert (
        checked["status"] == "unmatched"
        and "UNRESOLVED_LINE_MAPPING" in checked["result"]["failure_codes"]
    )
    response = await command(
        ctx,
        f"/invoice-checks/{checked['id']}/review",
        {"expected_revision": 1, "action": "confirm", "confirmed_amount": "0.00"},
    )
    assert response.status_code == 422


async def test_check_output_job_completion_and_event_are_atomic(context, monkeypatch):
    ctx = context
    _, current = await accepted(ctx)
    body = {
        "purchase_id": current["record_id"],
        "purchase_revision": 1,
        "po_id": ctx["order"]["id"],
    }
    response = await command(ctx, "/invoice-checks", body)
    original = invoice_checks.append_event

    async def fail(*args, **kwargs):
        raise RuntimeError("injected invoice audit failure")

    monkeypatch.setattr(invoice_checks, "append_event", fail)
    result = await run(ctx, response.json()["id"])
    assert result["status"] == "failed" and result["result_reference"] is None
    async with ctx["database"].transaction("coordinator", None) as conn:
        assert not (
            await conn.execute(
                "select id from collective.invoice_checks where purchase_id=%s",
                (current["record_id"],),
            )
        ).rowcount
    monkeypatch.setattr(invoice_checks, "append_event", original)
    retry = await command(ctx, "/invoice-checks", body)
    assert retry.json()["id"] != result["id"]
    assert (await run(ctx, retry.json()["id"]))["status"] == "succeeded"


async def test_historical_records_do_not_queue_delivered_invoice_checks(context):
    ctx = context
    _, current = await accepted(ctx, purpose="historical")
    async with ctx["database"].transaction("coordinator", None) as conn:
        assert not (
            await conn.execute(
                """select id from collective.jobs where kind='invoice_check'
                and payload->>'purchase_id'=%s""",
                (current["record_id"],),
            )
        ).rowcount
    assert (
        await command(
            ctx,
            "/invoice-checks",
            {
                "purchase_id": current["record_id"],
                "purchase_revision": 1,
                "po_id": ctx["order"]["id"],
            },
        )
    ).status_code == 404


async def test_duplicate_worker_triggers_have_one_result_and_cannot_supply_human_review(context):
    ctx = context
    _, current = await accepted(ctx)
    checked, body = await check(ctx, current)
    async with ctx["database"].transaction("coordinator", None) as conn:
        first = await (
            await conn.execute(
                """select * from collective.jobs where kind='invoice_check'
                and result->>'invoice_check_id'=%s""",
                (checked["id"],),
            )
        ).fetchone()
        duplicate = await jobs.enqueue(
            conn,
            business=ctx["business"],
            kind="invoice_check",
            revision=1,
            key=str(uuid4()),
            payload=first["payload"],
            due=first["next_attempt_at"],
        )
    assert (await run(ctx, duplicate["id"]))["result_reference"] == checked["id"]
    async with ctx["database"].transaction("coordinator", None) as conn:
        assert (
            await conn.execute(
                "select id from collective.invoice_checks where purchase_id=%s and po_id=%s",
                (current["record_id"], ctx["order"]["id"]),
            )
        ).rowcount == 1
    with pytest.raises(errors.CheckViolation):
        async with ctx["database"].transaction("coordinator", None) as conn:
            await conn.execute(
                """insert into collective.invoice_checks
                (id,business_id,purchase_id,purchase_revision,status,differences,review_status)
                values (%s,%s,%s,1,'unmatched','[]','confirmed')""",
                (uuid4(), ctx["business"], current["record_id"]),
            )
    with pytest.raises(errors.InsufficientPrivilege):
        async with ctx["database"].transaction("coordinator", None) as conn:
            await conn.execute(
                "update collective.invoice_checks set review_status='confirmed' where id=%s",
                (checked["id"],),
            )


async def test_owner_review_and_audit_are_atomic_and_rejection_does_not_change_the_result(
    context, monkeypatch
):
    ctx = context
    _, current = await accepted(ctx)
    checked, _ = await check(ctx, current)
    path = f"/invoice-checks/{checked['id']}/review"
    original = invoice_checks.append_event

    async def fail(*args, **kwargs):
        raise RuntimeError("injected review audit failure")

    monkeypatch.setattr(invoice_checks, "append_event", fail)
    with pytest.raises(RuntimeError, match="review audit"):
        await command(
            ctx, path, {"expected_revision": 1, "action": "confirm", "confirmed_amount": "5.00"}
        )
    row = await ctx["client"].get(
        ctx["root"] + f"/invoice-checks/{checked['id']}", headers=ctx["auth"]("owner-a")
    )
    assert row.json()["revision"] == 1 and row.json()["confirmed_amount"] is None
    monkeypatch.setattr(invoice_checks, "append_event", original)
    response = await command(ctx, path, {"expected_revision": 1, "action": "reject"})
    assert response.status_code == 200, response.text
    assert (
        response.json()["review_status"] == "rejected"
        and response.json()["result"] == checked["result"]
    )
    assert (
        response.json()["confirmed_amount"] is None and response.json()["recovery_amount"] is None
    )
