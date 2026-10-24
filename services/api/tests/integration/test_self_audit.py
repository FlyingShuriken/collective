from datetime import UTC, datetime

from collective.repositories.db import set_context
from collective.services.permissions import membership
from collective.services.self_audit import project
from tests.integration.test_intake_confirmation import confirm, edited, purchase_proposal
from tests.integration.test_invoice_loop import accepted, check, command, source
from tests.integration.test_invoice_loop import context as context


async def audit(ctx, actor="owner-a", business=None):
    response = await ctx["client"].get(
        f"/v1/businesses/{business or ctx['business']}/self-audit", headers=ctx["auth"](actor)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_empty_history_has_counts_and_no_invented_period(db, tenants):
    await set_context(db, "tenant", tenants["owner_a"])
    access = await membership(db, tenants["owner_a"], tenants["a"])
    result = await project(db, tenants["a"], access, datetime.now(UTC))
    assert result.purchase_records == result.delivered_records == result.checked_records == 0
    assert result.period_start is result.period_end is None
    assert result.confirmed_variance_amount == result.documented_recovery_amount == 0


async def test_audit_distinguishes_unmatched_confirmed_variance_and_documented_recovery(context):
    ctx = context
    before = await audit(ctx)
    _, _, proposed = await purchase_proposal(ctx)
    corrected = await edited(
        ctx,
        proposed,
        reading=proposed["reading"]
        | {
            "discount": "0.00",
            "surcharge": "0.00",
        },
    )
    response = await confirm(ctx, corrected)
    assert response.status_code == 200, response.text
    current = response.json()
    pending = await audit(ctx)
    assert pending["delivered_records"] == before["delivered_records"] + 1
    assert pending["unmatched_records"] == before["unmatched_records"] + 1
    checked, _ = await check(ctx, current)
    assert checked["result"]["complete"], checked["result"]
    measured = await audit(ctx)
    assert measured["checked_records"] == 1 and measured["mismatched_records"] == 1
    assert measured["unmatched_records"] == 0
    assert (
        measured["confirmed_variance_amount"] == "0"
        and measured["documented_recovery_amount"] == "0"
    )
    path = f"/invoice-checks/{checked['id']}/review"
    confirmed = await command(
        ctx,
        path,
        {
            "expected_revision": 1,
            "action": "confirm",
            "confirmed_amount": "5.00",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    reviewed = await audit(ctx)
    assert reviewed["confirmed_variances"] == 1 and reviewed["confirmed_variance_amount"] == "5.00"
    assert reviewed["documented_recoveries"] == 0 and reviewed["documented_recovery_amount"] == "0"
    proof = await source(ctx, text="SYNTHETIC credit note AUDIT-001: RM5.00 for DEMO-B-001.")
    recovered = await command(
        ctx,
        path,
        {
            "expected_revision": 2,
            "action": "recover",
            "resolution": {
                "kind": "credit_note",
                "source_id": proof["id"],
                "source_sha256": proof["sha256"],
                "document_reference": "AUDIT-001",
                "amount": "5.00",
            },
        },
    )
    assert recovered.status_code == 200, recovered.text
    final = await audit(ctx)
    assert final["checked_records"] == final["mismatched_records"] == 1
    assert final["confirmed_variance_amount"] == final["documented_recovery_amount"] == "5.00"
    assert final["documented_recoveries"] == 1
    assert final["period_start"] and final["period_end"]
    staff = await audit(ctx, "staff")
    assert staff["price_visibility"] == "hidden"
    assert "confirmed_variance_amount" not in staff and "documented_recovery_amount" not in staff
    denied = await ctx["client"].get(ctx["root"] + "/self-audit", headers=ctx["auth"]("owner-b"))
    assert denied.status_code == 404


async def test_partial_check_keeps_confirmed_variance_outside_complete_check_denominator(context):
    ctx = context
    _, current = await accepted(ctx)
    checked, _ = await check(ctx, current)
    assert not checked["result"]["complete"]
    confirmed = await command(
        ctx,
        f"/invoice-checks/{checked['id']}/review",
        {
            "expected_revision": 1,
            "action": "confirm",
            "confirmed_amount": "5.00",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    result = await audit(ctx)
    assert result["checked_records"] == result["mismatched_records"] == 0
    assert result["incomplete_checks"] == 1 and result["unmatched_records"] == 0
    assert result["confirmed_variances"] == 1 and result["confirmed_variance_amount"] == "5.00"
    assert result["documented_recoveries"] == 0
