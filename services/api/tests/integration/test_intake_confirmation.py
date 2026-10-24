import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from collective.adapters.models import prepared_text
from collective.agents import intake
from collective.agents.schemas import IntakeReading
from collective.jobs.runner import Runner
from collective.repositories import jobs
from collective.services import document_records, intake_jobs
from tests.fixtures import ROOT
from tests.integration.conftest import local_test_url
from tests.integration.test_liaison_jobs import (
    cache,
    command,
    proposal,
    source,
)
from tests.integration.test_liaison_jobs import (
    context as context,
)


async def prepared(ctx, name="delivered-invoice.png"):
    response = await ctx["client"].post(
        ctx["root"] + "/sources",
        headers=ctx["auth"]("owner-a") | {"Idempotency-Key": str(uuid4())},
        files={
            "file": (
                name,
                (ROOT / "fixtures/sources" / name).read_bytes(),
                "application/pdf" if name.endswith("pdf") else "image/png",
            )
        },
    )
    assert response.status_code == 200, response.text
    value = response.json()["source"]
    ready = await command(
        ctx,
        f"/sources/{value['id']}/prepare",
        {
            "source_sha256": value["sha256"],
            "reviewed": True,
        },
    )
    assert ready.status_code == 200, ready.text
    return value, ready.json()["job"]


async def run(ctx, identity, gateway=None):
    async with ctx["database"].transaction("coordinator", None) as conn:
        await conn.execute(
            "update collective.jobs set next_attempt_at='1900-01-01' where id=%s", (identity,)
        )

    async def handle(db, job, clock):
        assert str(job["id"]) == str(identity)
        await intake_jobs.handle(db, job, clock, settings=ctx["settings"], gateway=gateway)

    assert await Runner(ctx["database"], handlers={"intake": handle}).run_once()
    response = await ctx["client"].get(
        ctx["root"] + f"/jobs/{identity}", headers=ctx["auth"]("owner-a")
    )
    return response.json()


async def options(ctx):
    response = await ctx["client"].get(
        ctx["root"] + "/record-options", headers=ctx["auth"]("owner-a")
    )
    assert response.status_code == 200, response.text
    return response.json()


async def edited(ctx, current, *, mapped=True, reading=None, record_context=None):
    available = await options(ctx)
    selected = next(
        item
        for item in available["mappings"]
        if item["supplier_id"] == current["context"]["supplier_id"]
    )
    context = current["context"] | {
        "purpose": "delivered",
        "line_mappings": [{"line": 0, "id": selected["id"], "revision": selected["revision"]}]
        if mapped
        else [],
    }
    response = await command(
        ctx,
        f"/proposals/{current['id']}",
        {
            "expected_revision": current["current_revision"],
            "reading": reading or current["reading"],
            "context": record_context or context,
        },
        method="PATCH",
    )
    assert response.status_code == 200, response.text
    return response.json()


async def confirm(ctx, current, **kwargs):
    return await command(
        ctx,
        f"/proposals/{current['id']}/confirm",
        {
            "expected_revision": current["current_revision"],
            "acknowledged_flags": current["flags"],
        },
        **kwargs,
    )


async def purchase_proposal(ctx, name="delivered-invoice.png"):
    value, job = await prepared(ctx, name)
    completed = await run(ctx, job["id"])
    assert completed["status"] == "succeeded", completed
    assert completed["review_state"] == "pending_review"
    return value, job, await proposal(ctx, completed["result_reference"])


async def test_image_readings_remain_proposed_and_keep_both_attempts(context):
    ctx = context
    value, job, current = await purchase_proposal(ctx)
    assert current["reading"]["lines"][0]["unit_price"] == "0.275"
    assert current["record_id"] is None and current["status"] == "pending_review"
    assert current["evidence"][0]["source_id"] == value["id"]
    assert "discount:missing" in current["flags"]
    async with ctx["database"].transaction("coordinator", None) as conn:
        rows = await (
            await conn.execute(
                """select * from collective_private.model_attempts
                where job_id=%s order by created_at,id""",
                (job["id"],),
            )
        ).fetchall()
        assert len(rows) == 2 and {row["attempt"]["tier"] for row in rows} == {"fast", "strong"}
        assert {row["validated"]["lines"][0]["unit_price"] for row in rows} == {"0.270", "0.275"}
        assert not (
            await conn.execute(
                "select id from collective.purchases where source_id=%s", (value["id"],)
            )
        ).rowcount
    for actor, status in (("staff", 200), ("owner-b", 404), ("admin", 404)):
        response = await ctx["client"].get(
            ctx["root"] + f"/proposals/{current['id']}", headers=ctx["auth"](actor)
        )
        assert response.status_code == status
        if actor == "staff":
            assert (
                response.json()["price_visibility"] == "hidden" and "reading" not in response.json()
            )
    assert (await confirm(ctx, current, actor="staff")).status_code == 403
    assert (
        await ctx["client"].get(ctx["root"] + "/record-options", headers=ctx["auth"]("staff"))
    ).status_code == 403


@pytest.mark.parametrize("mapped", [False, True])
async def test_human_confirmation_preserves_unknowns_and_is_source_idempotent(context, mapped):
    ctx = context
    value, job, current = await purchase_proposal(ctx, "delivered-invoice.pdf")
    current = await edited(ctx, current, mapped=mapped)
    responses = await asyncio.gather(confirm(ctx, current), confirm(ctx, current))
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["status"] == "confirmed"
    async with ctx["database"].transaction("coordinator", None) as conn:
        rows = await (
            await conn.execute(
                "select * from collective.purchases where source_id=%s", (value["id"],)
            )
        ).fetchall()
        assert len(rows) == 1 and rows[0]["sealed"]
        assert rows[0]["discount"] is None and rows[0]["surcharge"] is None
        assert rows[0]["total"] == Decimal("290")
        assert rows[0]["missing_fields"]["eligible_for_baseline"] is False
        line = await (
            await conn.execute(
                "select * from collective.purchase_lines where purchase_id=%s", (rows[0]["id"],)
            )
        ).fetchone()
        assert line["original_unit_price"] == Decimal("0.275")
        assert (line["quantity"] == Decimal("1000")) if mapped else line["quantity"] is None
        assert not (
            await conn.execute(
                "select id from collective.stock_observations where source_id=%s", (value["id"],)
            )
        ).rowcount
        assert (
            await conn.execute(
                """select id from collective.events
                where entity_id=%s and event_type='record_confirmed'""",
                (current["id"],),
            )
        ).rowcount == 1
    retry = await command(ctx, f"/sources/{value['id']}/intake", {"source_sha256": value["sha256"]})
    assert retry.status_code == 200 and retry.json()["id"] == job["id"]
    async with ctx["database"].transaction("coordinator", None) as conn:
        original = await (
            await conn.execute("select * from collective.jobs where id=%s", (job["id"],))
        ).fetchone()
        repeated = await jobs.enqueue(
            conn,
            business=ctx["business"],
            kind="intake",
            revision=1,
            key=str(uuid4()),
            payload=original["payload"] | {"preparation_job_id": job["id"]},
            due=original["next_attempt_at"],
        )
    result = await run(ctx, repeated["id"])
    assert result["result_reference"] == current["id"]
    listed = await ctx["client"].get(ctx["root"] + "/proposals", headers=ctx["auth"]("owner-a"))
    assert len(listed.json()["items"]) == 1


async def test_staff_cannot_confirm_unknown_cost_or_bypass_limit_with_printed_total(context):
    ctx = context
    _, _, current = await purchase_proposal(ctx)
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await conn.execute(
            """update collective.businesses set staff_can_view_prices=true,staff_limit=290
            where id=%s""",
            (ctx["business"],),
        )
    current = await edited(ctx, current)
    assert (await confirm(ctx, current, actor="staff")).status_code == 403
    reading = current["reading"] | {
        "discount": "0.00",
        "surcharge": "0.00",
        "tax_rate": "0",
        "delivery_mode": "per_business",
    }
    current = await edited(ctx, current, reading=reading)
    assert (await confirm(ctx, current, actor="staff")).status_code == 200
    _, _, other = await purchase_proposal(ctx, "delivered-invoice.pdf")
    other = await edited(ctx, other, reading=reading | {"total": "1.00"})
    assert other["review_value"] == "290.00" and "total:mismatch" in other["flags"]
    assert (await confirm(ctx, other, actor="staff")).status_code == 403
    async with await AsyncConnection.connect(local_test_url("MIGRATION_DATABASE_URL")) as conn:
        await conn.execute(
            "update collective.businesses set staff_limit=289.99 where id=%s", (ctx["business"],)
        )
    other = await edited(ctx, other, reading=reading)
    assert (await confirm(ctx, other, actor="staff")).status_code == 403


async def test_stock_requires_human_mapping_time_and_does_not_accept_usage_claim(context):
    ctx = context
    value = await source(ctx, "stock-ms.txt")
    response = await ctx["client"].get(
        ctx["root"] + f"/sources/{value['id']}", headers=ctx["auth"]("owner-a")
    )
    result = await run(ctx, response.json()["job"]["id"])
    current = await proposal(ctx, result["result_reference"])
    assert current["kind"] == "stock" and current["reading"]["stock_observed_at"] is None
    assert (await confirm(ctx, current)).status_code == 422
    selected = (await options(ctx))["mappings"][0]
    response = await command(
        ctx,
        f"/proposals/{current['id']}",
        {
            "expected_revision": 1,
            "reading": current["reading"]
            | {
                "stock_observed_at": datetime.now(UTC).isoformat(),
                "reported_usage_per_day": "777",
            },
            "context": {"stock_mapping": {"id": selected["id"], "revision": selected["revision"]}},
        },
        method="PATCH",
    )
    assert response.status_code == 200, response.text
    current = response.json()
    assert (await confirm(ctx, current)).status_code == 200
    async with ctx["database"].transaction("coordinator", None) as conn:
        rows = await (
            await conn.execute(
                "select * from collective.stock_observations where source_id=%s", (value["id"],)
            )
        ).fetchall()
        assert len(rows) == 1 and rows[0]["quantity"] == Decimal("750")
        need = await (
            await conn.execute(
                "select usage_input from collective.reorder_needs where id=%s",
                (ctx["need"]["id"],),
            )
        ).fetchone()
        assert "777" not in str(need["usage_input"])


async def test_record_and_proposal_roll_back_when_audit_fails(context, monkeypatch):
    ctx = context
    _, _, current = await purchase_proposal(ctx)
    current = await edited(ctx, current)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(document_records, "append_event", fail)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        await confirm(ctx, current)
    assert (await proposal(ctx, current["id"]))["status"] == "pending_review"
    async with ctx["database"].transaction("coordinator", None) as conn:
        assert not (
            await conn.execute(
                "select id from collective.purchases where proposal_id=%s", (current["id"],)
            )
        ).rowcount




async def test_failed_reading_can_retry_without_rewriting_original_failure(context, tmp_path):
    ctx = context
    text = "SYNTHETIC new stock document without a supplied observation time."
    value = await source(ctx, text=text)
    response = await ctx["client"].get(
        ctx["root"] + f"/sources/{value['id']}", headers=ctx["auth"]("owner-a")
    )
    original = response.json()["job"]
    failed = await run(ctx, original["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "REPLAY_MISS"
    gateway = cache(
        tmp_path,
        prepared_text(text),
        intake.INTAKE,
        IntakeReading(
            kind="stock",
            stock_description="cups",
            stock_quantity="0",
            stock_unit="piece",
            confidence_label="high",
        ).model_dump(mode="json"),
    )
    response = await command(
        ctx, f"/sources/{value['id']}/intake", {"source_sha256": value["sha256"]}
    )
    assert response.status_code == 200 and response.json()["id"] != original["id"]
    result = await run(ctx, response.json()["id"], gateway)
    assert result["status"] == "succeeded" and result["result_reference"]
    saved = await ctx["client"].get(
        ctx["root"] + f"/jobs/{original['id']}", headers=ctx["auth"]("owner-a")
    )
    assert saved.json()["status"] == "failed"


async def test_supplier_reply_needs_quote_context_and_never_invents_a_purchase(context):
    ctx = context
    value = await source(ctx)
    response = await ctx["client"].get(
        ctx["root"] + f"/sources/{value['id']}", headers=ctx["auth"]("owner-a")
    )
    result = await run(ctx, response.json()["job"]["id"])
    assert result["status"] == "succeeded" and result["review_state"] == "needs_quote_context"
    assert result["result_reference"] is None
    async with ctx["database"].transaction("coordinator", None) as conn:
        assert not (
            await conn.execute(
                "select id from collective.proposals where source_id=%s", (value["id"],)
            )
        ).rowcount


async def test_human_kind_correction_rejection_and_immutable_evidence(context):
    ctx = context
    _, _, current = await purchase_proposal(ctx)
    path = f"/proposals/{current['id']}"
    rejected = await command(
        ctx,
        path,
        {
            "expected_revision": 1,
            "reading": current["reading"] | {"evidence": []},
        },
        method="PATCH",
    )
    assert rejected.status_code == 422 and rejected.json()["code"] == "EVIDENCE_IMMUTABLE"
    changed = await command(
        ctx,
        path,
        {
            "expected_revision": 1,
            "reading": current["reading"] | {"kind": "stock"},
        },
        method="PATCH",
    )
    assert changed.status_code == 200 and changed.json()["kind"] == "stock", changed.text
    assert changed.json()["current_revision"] == 2
    assert (await command(ctx, path + "/reject", {"expected_revision": 2})).json()[
        "status"
    ] == "rejected"
    assert (await confirm(ctx, changed.json())).status_code == 409
    assert (
        await command(
            ctx, path, {"expected_revision": 2, "reading": current["reading"]}, method="PATCH"
        )
    ).status_code == 409


async def test_new_stock_revalidates_existing_permission_without_issuing_an_order(context):
    ctx = context
    approved = await command(
        ctx,
        "/approvals",
        {
            "need_id": ctx["need"]["id"],
            "pool_id": None,
            "cap": "330.00",
            "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
            "expected_evaluation_revision": ctx["need"]["revision"],
            "quote_id": ctx["offer"]["quote_id"],
            "expected_quote_revision": ctx["offer"]["quote_revision"],
            "reviewed_decision_cost": "315.00",
        },
    )
    assert approved.status_code == 200, approved.text
    value = await source(ctx, "stock-ms.txt")
    response = await ctx["client"].get(
        ctx["root"] + f"/sources/{value['id']}", headers=ctx["auth"]("owner-a")
    )
    result = await run(ctx, response.json()["job"]["id"])
    current = await proposal(ctx, result["result_reference"])
    selected = (await options(ctx))["mappings"][0]
    response = await command(
        ctx,
        f"/proposals/{current['id']}",
        {
            "expected_revision": 1,
            "reading": current["reading"]
            | {
                "stock_quantity": "100000",
                "stock_observed_at": datetime.now(UTC).isoformat(),
            },
            "context": {"stock_mapping": {"id": selected["id"], "revision": selected["revision"]}},
        },
        method="PATCH",
    )
    assert response.status_code == 200, response.text
    assert (await confirm(ctx, response.json())).status_code == 200
    async with ctx["database"].transaction("coordinator", None) as conn:
        row = await (
            await conn.execute(
                "select state from collective.bound_approvals where id=%s",
                (approved.json()["approval_id"],),
            )
        ).fetchone()
        assert row["state"] == "void"
        assert not (
            await conn.execute(
                "select id from collective.purchase_orders where business_id=%s", (ctx["business"],)
            )
        ).rowcount
