import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from collective.api.dto import ApproveOrder, DiscoverPool, RequestDraft, SoloExecution
from collective.api.errors import DomainError
from collective.domain.clock import SystemClock
from collective.domain.invoices import (
    ConfirmedInvoice,
    InvoiceLine,
    IssuedInvoiceOrder,
    check_invoice,
)
from collective.jobs.runner import Runner
from collective.repositories import learning
from collective.repositories import rules as accepted_rules
from collective.repositories.db import set_context
from collective.services import liaison_jobs, needs, pool_discovery, releases, rules, solo
from collective.services.document_records import insert
from tests.fixtures import ROOT
from tests.unit.test_costs import quote


async def operator(db):
    await db.execute("reset role")


async def source(db, business, label):
    identity = uuid4()
    text = f"Private synthetic evidence {label} {identity}"
    await db.execute(
        """insert into collective.sources
        (id,business_id,kind,status,sha256,text_content,provenance)
        values (%s,%s,'text','available',%s,%s,'synthetic')""",
        (identity, business, hashlib.sha256(text.encode()).hexdigest(), text),
    )
    return identity


async def invoice(db, ids, business, amount, date, *, purpose="historical"):
    identity = uuid4()
    evidence = await source(db, business, "invoice")
    await insert(
        db,
        "purchases",
        {
            "id": identity,
            "business_id": business,
            "revision": 1,
            "supplier_id": ids["supplier"],
            "source_id": evidence,
            "document_number": "PRIVATE-" + identity.hex,
            "document_date": date,
            "currency": "MYR",
            "purpose": purpose,
            "goods_total": Decimal("300"),
            "delivery_fee": Decimal(amount),
            "tax_rate": Decimal(0),
            "discount": Decimal(0),
            "surcharge": Decimal(0),
            "total": Decimal("300") + Decimal(amount),
            "credit_days": 30,
            "delivery_mode": "per_business",
            "delivery_zone": "sepang",
            "provenance": "synthetic",
        },
    )
    await insert(
        db,
        "purchase_lines",
        {
            "business_id": business,
            "purchase_id": identity,
            "purchase_revision": 1,
            "line": 0,
            "source_name": "Private cups",
            "quantity": Decimal("1000"),
            "base_unit": "piece",
            "unit_price": Decimal("0.30"),
            "line_total": Decimal("300"),
            "spec_id": ids["spec"],
            "spec_revision": 1,
        },
    )
    await db.execute(
        "update collective.purchases set sealed=true where business_id=%s and id=%s",
        (business, identity),
    )
    return identity, evidence


@pytest.fixture
async def history(db, tenants):
    fixture = json.loads((ROOT / "fixtures/scenarios/five-business-history.json").read_text())
    ids = tenants | {name: uuid4() for name in ("network", "admin", "supplier", "spec", "family")}
    ids["now"] = datetime.now(UTC)
    await db.execute(
        "insert into collective.networks (id,name) values (%s,'Historical network')",
        (ids["network"],),
    )
    await db.execute("insert into auth.users(id) values (%s)", (ids["admin"],))
    await db.execute(
        "insert into collective.network_admins values (%s,%s)", (ids["network"], ids["admin"])
    )
    await db.execute(
        "insert into collective.suppliers(id,name) values (%s,'Supplier')", (ids["supplier"],)
    )
    await db.execute(
        "insert into collective.canonical_specs(id,name) values (%s,'Cups')", (ids["spec"],)
    )
    await db.execute(
        """insert into collective.spec_revisions
        (spec_id,revision,base_unit,pack_multiple,perishable,attributes)
        values (%s,1,'piece',100,false,'{}')""",
        (ids["spec"],),
    )
    ids["businesses"], ids["sources"], ids["purchases"] = [], [], []
    for position, key in enumerate(fixture["business_keys"]):
        business = ids["a"] if position == 0 else ids["b"] if position == 1 else uuid4()
        if position >= 2:
            await db.execute(
                """insert into collective.businesses(id,name,zone,locale)
                values (%s,%s,'sepang','en')""",
                (business, key),
            )
        await db.execute(
            "update collective.businesses set network_id=%s,pool_opt_in=true where id=%s",
            (ids["network"], business),
        )
        ids["businesses"].append(business)
        for template in fixture["invoices_per_business"]:
            purchase, evidence = await invoice(
                db,
                ids,
                business,
                template["delivery_fee"],
                datetime.fromisoformat(template["at"]).date(),
            )
            ids["purchases"].append(purchase)
            ids["sources"].append(evidence)
        if position >= 2:
            continue
        letter = "a" if position == 0 else "b"
        owner = ids["owner_" + letter]
        mapping, need_id = uuid4(), uuid4()
        ids["need_" + letter] = need_id
        await db.execute(
            "insert into collective.business_suppliers(business_id,supplier_id) values (%s,%s)",
            (business, ids["supplier"]),
        )
        await db.execute(
            """insert into collective.sku_mappings
            (business_id,id,revision,supplier_id,supplier_sku,source_name,spec_id,spec_revision,confirmed,provenance)
            values (%s,%s,1,%s,'CUP','Cups',%s,1,true,'synthetic')""",
            (business, mapping, ids["supplier"], ids["spec"]),
        )
        for index, (quantity, at) in enumerate(
            (
                ("1000", ids["now"] - timedelta(days=21)),
                ("0", ids["now"] - timedelta(days=1)),
                ("750", ids["now"] - timedelta(minutes=1)),
            )
        ):
            evidence = await source(db, business, f"stock {index}")
            ids["sources"].append(evidence)
            await db.execute(
                """insert into collective.stock_observations
                (id,business_id,spec_id,spec_revision,quantity,observed_at,source_id,provenance)
                values (%s,%s,%s,1,%s,%s,%s,'synthetic')""",
                (uuid4(), business, ids["spec"], quantity, at, evidence),
            )
        value = quote(
            id=uuid4(),
            business_id=business,
            supplier_id=ids["supplier"],
            spec_id=ids["spec"],
            offer_family=ids["family"],
            delivery_fee="0.00",
            valid_from=ids["now"] - timedelta(days=1),
            valid_until=ids["now"] + timedelta(days=1),
            delivery_window={
                "start": ids["now"] + timedelta(days=4),
                "end": ids["now"] + timedelta(days=5),
            },
        )
        ids["quote_" + letter] = value.id
        fields = value.model_dump(exclude={"confirmed", "tiers", "delivery_window"}) | {
            "arrival_start": value.delivery_window.start,
            "arrival_end": value.delivery_window.end,
            "provenance": "synthetic",
        }
        await insert(db, "quote_revisions", fields)
        for tier in value.tiers:
            await insert(
                db,
                "quote_tiers",
                {
                    "business_id": business,
                    "quote_id": value.id,
                    "quote_revision": 1,
                    "min_quantity": tier.min_quantity,
                    "unit_price": tier.unit_price,
                },
            )
        await db.execute(
            "update collective.quote_revisions set sealed=true where business_id=%s", (business,)
        )
        await insert(
            db,
            "reorder_needs",
            {
                "id": need_id,
                "business_id": business,
                "spec_id": ids["spec"],
                "spec_revision": 1,
                "quantity": Decimal("1000"),
                "base_unit": "piece",
                "pack_multiple": Decimal("100"),
                "mapping_id": mapping,
                "mapping_revision": 1,
                "zone": "sepang",
                "saving_threshold": Decimal("0.05"),
                "min_credit_days": 30,
                "delivery_mode": "per_business",
                "order_start": ids["now"],
                "order_end": ids["now"] + timedelta(hours=2),
                "arrival_start": value.delivery_window.start,
                "arrival_end": value.delivery_window.end,
                "state": "selected",
                "selected_by": owner,
                "usage_input": Jsonb({"stated_daily_rate": "125"}),
                "lead_days": Decimal("4"),
                "buffer_days": Decimal("2"),
                "max_stock": Decimal("1300"),
                "provenance": "synthetic",
            },
        )
    return ids


async def proposed(db, ids, *, scope="private", kind="observed_fee"):
    await set_context(db, "coordinator", None)
    target = ids["a"] if scope == "private" else ids["network"]
    created = await rules.propose(db, scope, target, datetime.now(UTC))
    return await (
        await db.execute(
            "select * from collective.learned_rules where id=any(%s) and kind=%s",
            (created, kind),
        )
    ).fetchone()


async def change(db, ids, rule, action, *, scope="private", subject=None):
    subject = subject or ids["owner_a"]
    await set_context(db, "tenant", subject)
    return await rules.change(
        db,
        subject,
        scope,
        ids["a"] if scope == "private" else ids["network"],
        rule["id"],
        action,
        rule["revision"],
        datetime.now(UTC),
    )


async def solo_approval(db, ids, *, cap="300.00"):
    await set_context(db, "tenant", ids["owner_a"])
    return await solo.approve(
        db,
        ids["owner_a"],
        ids["a"],
        ApproveOrder(
            need_id=ids["need_a"],
            pool_id=None,
            cap=cap,
            expires_at=ids["now"] + timedelta(hours=1),
            expected_evaluation_revision=1,
            quote_id=ids["quote_a"],
            expected_quote_revision=1,
            reviewed_decision_cost="300.00",
        ),
        str(uuid4()),
        SystemClock(),
    )


async def test_private_fee_proposal_has_evidence_dedupes_and_only_owner_can_activate(db, history):
    ids = history
    row = await proposed(db, ids)
    assert row["state"] == "proposed" and row["sealed"] and row["sample_count"] == 3
    assert len(row["proposed_value"]["evidence"]) == 3
    assert await rules.propose(db, "private", ids["a"], datetime.now(UTC)) == []
    need = await needs.load_need(db, ids["a"], ids["need_a"])
    assert (await needs.comparisons(db, need, datetime.now(UTC))).offers[
        0
    ].cost.decision_cost == 300
    for subject in (ids["staff"], ids["owner_b"], ids["admin"]):
        with pytest.raises(DomainError):
            await change(db, ids, row, "accept", subject=subject)
    accepted = await change(db, ids, row, "accept")
    assert accepted.state == "accepted" and len(accepted.history) == 1
    assert (await needs.comparisons(db, need, datetime.now(UTC))).offers[
        0
    ].cost.decision_cost == 315
    assert (await needs.options(db, need, datetime.now(UTC))).options[0].cost == 315
    repeated = await change(db, ids, row, "accept")
    assert repeated.revision == accepted.revision and repeated.history == accepted.history
    await set_context(db, "tenant", ids["staff"])
    page = await rules.listing(db, ids["staff"], "private", ids["a"], None, 25, datetime.now(UTC))
    assert all(item.price_visibility == "hidden" for item in page.items)
    assert "amount" not in page.model_dump_json() and "source_id" not in page.model_dump_json()


async def test_fee_acceptance_voids_bound_solo_cap_and_revocation_never_revives_permission(
    db, history
):
    ids = history
    approval = await solo_approval(db, ids)
    row = await proposed(db, ids)
    accepted = await change(db, ids, row, "accept")
    state = await (
        await db.execute(
            "select state from collective.bound_approvals where id=%s", (approval.approval_id,)
        )
    ).fetchone()
    assert state["state"] == "void"
    revoked = await change(db, ids, {"id": row["id"], "revision": accepted.revision}, "revoke")
    assert revoked.state == "revoked" and [h.state for h in revoked.history] == [
        "accepted",
        "revoked",
    ]
    need = await needs.load_need(db, ids["a"], ids["need_a"])
    assert (await needs.comparisons(db, need, datetime.now(UTC))).best_solo_cost == 300
    assert (await solo.latest_binding(db, ids["a"], ids["need_a"]))["state"] == "void"


async def test_usage_acceptance_recalculates_real_stock_constraints_and_revoke_uses_stated_rate(
    db, history
):
    ids = history
    approval = await solo_approval(db, ids)
    row = await proposed(db, ids, kind="usage")
    accepted = await change(db, ids, row, "accept")
    need = await needs.load_need(db, ids["a"], ids["need_a"])
    options = await needs.options(db, need, datetime.now(UTC))
    assert options.replenishment.daily_rate == 50
    assert options.replenishment.usage_rule_id == row["id"] and not options.stated_quantity_valid
    assert (await solo.latest_binding(db, ids["a"], ids["need_a"]))["state"] == "void"
    assert approval.approval_id is not None
    await change(db, ids, {"id": row["id"], "revision": accepted.revision}, "revoke")
    options = await needs.options(db, need, datetime.now(UTC))
    assert options.replenishment.daily_rate == 125 and options.stated_quantity_valid
    assert options.replenishment.usage_rule_id is None


async def test_three_business_network_is_empty_five_can_activate_without_raw_evidence(db, history):
    ids = history
    await db.execute(
        "update collective.businesses set network_id=null where id=any(%s)",
        (ids["businesses"][3:],),
    )
    assert await proposed(db, ids, scope="network") is None
    await set_context(db, "tenant", ids["admin"])
    page = await rules.listing(
        db, ids["admin"], "network", ids["network"], None, 25, datetime.now(UTC)
    )
    assert page.items == () and page.minimum_network_businesses == 5
    await operator(db)
    await db.execute(
        "update collective.businesses set network_id=%s where id=any(%s)",
        (ids["network"], ids["businesses"][3:]),
    )
    row = await proposed(db, ids, scope="network")
    assert row["distinct_businesses"] == 5 and row["sample_count"] == 15
    for subject in (ids["owner_a"], ids["staff"]):
        with pytest.raises(DomainError):
            await change(db, ids, row, "accept", scope="network", subject=subject)
    accepted = await change(db, ids, row, "accept", scope="network", subject=ids["admin"])
    assert accepted.value.amount == 15 and accepted.scope == "network"
    content = accepted.model_dump_json()
    for private_id in ids["businesses"] + ids["sources"] + ids["purchases"]:
        assert str(private_id) not in content
    assert "evidence" not in content and "quantity" not in content
    await set_context(db, "tenant", ids["admin"])
    assert await (await db.execute("select * from collective.rule_evidence")).fetchall() == []
    assert (await accepted_rules.fee_rules(db, ids["a"])) == ()  # Admin is not a business member.
    await set_context(db, "tenant", ids["owner_a"])
    assert (await accepted_rules.fee_rules(db, ids["a"]))[0].amount == 15


async def test_rejected_stale_and_superseded_rules_never_gain_silent_activation(db, history):
    ids = history
    first = await proposed(db, ids)
    rejected = await change(db, ids, first, "reject")
    assert rejected.state == "rejected" and await accepted_rules.fee_rules(db, ids["a"]) == ()
    with pytest.raises(DomainError, match="no longer"):
        await change(db, ids, {"id": first["id"], "revision": rejected.revision}, "accept")
    await operator(db)
    await invoice(db, ids, ids["a"], "35.00", datetime(2026, 10, 1).date())
    fresh = await proposed(db, ids)
    accepted = await change(db, ids, fresh, "accept")
    assert accepted.value.amount == 20
    await operator(db)
    await invoice(db, ids, ids["a"], "45.00", datetime(2026, 10, 2).date())
    newest = await proposed(db, ids)
    replacement = await change(db, ids, newest, "accept")
    assert replacement.value.amount == 25
    old = await (
        await db.execute("select * from collective.learned_rules where id=%s", (fresh["id"],))
    ).fetchone()
    assert old["state"] == "superseded" and old["accepted_by"] == ids["owner_a"]
    assert len(await accepted_rules.fee_rules(db, ids["a"])) == 1


async def test_database_freezes_rule_inputs_evidence_and_rejects_coordinator_acceptance(
    db, history
):
    ids = history
    row = await proposed(db, ids)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.transaction():
            await db.execute(
                """update collective.learned_rules
                set state='accepted',revision=revision+1 where id=%s""",
                (row["id"],),
            )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.transaction():
            await db.execute(
                """update collective.learned_rules
                set sample_count=99,revision=revision+1 where id=%s""",
                (row["id"],),
            )
    with pytest.raises(psycopg.errors.CheckViolation, match="already sealed"):
        async with db.transaction():
            await db.execute(
                """insert into collective.rule_evidence(rule_id,business_id,source_id)
                values (%s,%s,%s)""",
                (row["id"], ids["a"], ids["sources"][3]),
            )


async def test_accepted_fee_recalculates_pool_and_preserves_other_members_approvals(db, history):
    ids = history
    await set_context(db, "tenant", ids["owner_a"])
    result = await pool_discovery.discover(
        db,
        ids["owner_a"],
        ids["a"],
        DiscoverPool(need_id=ids["need_a"], expected_revision=1),
        str(uuid4()),
        SystemClock(),
    )
    assert result.state == "found" and result.offer.decision_delivered_cost == 270
    from collective.services import approval_commands

    current_revision = result.offer.evaluation_revision
    for letter in "ab":
        await set_context(db, "tenant", ids["owner_" + letter])
        approved = await approval_commands.approve(
            db,
            ids["owner_" + letter],
            ids[letter],
            ApproveOrder(
                need_id=ids["need_" + letter],
                pool_id=result.offer.pool_id,
                cap="275.00",
                expires_at=ids["now"] + timedelta(hours=1),
                expected_evaluation_revision=current_revision,
            ),
            str(uuid4()),
            SystemClock(),
        )
        assert approved.status == "approved"
        current_revision = approved.offer.evaluation_revision
    rule = await proposed(db, ids)
    await change(db, ids, rule, "accept")
    rows = await (
        await db.execute(
            "select business_id,state from collective.bound_approvals where pool_id=%s",
            (result.offer.pool_id,),
        )
    ).fetchall()
    assert {r["business_id"]: r["state"] for r in rows} == {ids["a"]: "void", ids["b"]: "valid"}


async def test_changed_evidence_requires_a_fresh_proposal_before_acceptance(db, history):
    ids = history
    row = await proposed(db, ids)
    await operator(db)
    await invoice(db, ids, ids["a"], "35.00", datetime(2026, 10, 1).date())
    with pytest.raises(DomainError) as error:
        await change(db, ids, row, "accept")
    assert error.value.code == "STALE_EVIDENCE"
    assert await accepted_rules.fee_rules(db, ids["a"]) == ()
    state = await (
        await db.execute("select state from collective.learned_rules where id=%s", (row["id"],))
    ).fetchone()
    assert state["state"] == "proposed"


async def test_private_rule_overrides_its_network_context_and_revocation_restores_network_basis(
    db, history
):
    ids = history
    await invoice(db, ids, ids["a"], "35.00", datetime(2026, 10, 1).date())
    network = await proposed(db, ids, scope="network")
    await change(db, ids, network, "accept", scope="network", subject=ids["admin"])
    private = await proposed(db, ids)
    accepted = await change(db, ids, private, "accept")
    own = await accepted_rules.fee_rules(db, ids["a"])
    other = await accepted_rules.fee_rules(db, ids["b"])
    assert len(own) == len(other) == 1
    assert own[0].id == private["id"] and own[0].amount == 20
    assert other[0].id == network["id"] and other[0].amount == Decimal("16.25")
    await change(db, ids, {"id": private["id"], "revision": accepted.revision}, "revoke")
    own = await accepted_rules.fee_rules(db, ids["a"])
    assert own[0].id == network["id"] and own[0].amount == Decimal("16.25")


async def test_database_checks_actual_network_evidence_not_a_claimed_business_count(db, history):
    ids = history
    private = await proposed(db, ids)
    fake = uuid4()
    value = private["proposed_value"] | {"eligible_network": True, "distinct_businesses": 5}
    value.pop("evidence")
    await db.execute(
        """insert into collective.learned_rules
        (id,network_id,scope,kind,context_key,evidence_digest,proposed_value,sample_count,distinct_businesses)
        values (%s,%s,'network','observed_fee','fake-context','fake-digest',%s,3,5)""",
        (fake, ids["network"], Jsonb(value)),
    )
    for business in ids["businesses"][:3]:
        evidence = await (
            await db.execute(
                "select id from collective.sources where business_id=%s limit 1",
                (business,),
            )
        ).fetchone()
        await db.execute(
            "insert into collective.rule_evidence(rule_id,business_id,source_id) values (%s,%s,%s)",
            (fake, business, evidence["id"]),
        )
    await db.execute(
        "update collective.learned_rules set sealed=true,revision=2 where id=%s", (fake,)
    )
    await set_context(db, "tenant", ids["admin"])
    now = datetime.now(UTC)
    with pytest.raises(psycopg.errors.CheckViolation, match="activation gate"):
        async with db.transaction():
            await db.execute(
                """update collective.learned_rules set state='accepted',revision=3,
                accepted_by=%s,accepted_at=%s,reviewed_by=%s,reviewed_at=%s where id=%s""",
                (ids["admin"], now, ids["admin"], now, fake),
            )


async def test_issued_order_and_unagreed_invoice_fee_survive_acceptance_and_revocation(db, history):
    ids = history
    approved = await solo_approval(db, ids)
    released = await releases.release_solo(
        db,
        ids["owner_a"],
        ids["a"],
        ids["need_a"],
        SoloExecution(approval_id=approved.approval_id, expected_revision=1),
        str(uuid4()),
        SystemClock(),
    )
    assert released.order.amount == 300
    original = await (
        await db.execute(
            "select * from collective.purchase_orders where id=%s", (released.order.id,)
        )
    ).fetchone()
    issued = IssuedInvoiceOrder(
        id=original["id"],
        business_id=ids["a"],
        amount=original["amount"],
        terms=original["snapshot"]["terms"],
        cost=original["snapshot"]["cost"],
    )
    await operator(db)
    purchase, evidence = await invoice(
        db, ids, ids["a"], "15.00", datetime(2026, 10, 1).date(), purpose="delivered"
    )
    delivered = ConfirmedInvoice(
        id=purchase,
        business_id=ids["a"],
        revision=1,
        supplier_id=ids["supplier"],
        confirmed=True,
        purpose="delivered",
        currency="MYR",
        goods_total="300.00",
        delivery_fee="15.00",
        tax_amount="0.00",
        tax_rate="0",
        discount="0.00",
        surcharge="0.00",
        total="315.00",
        lines=(
            InvoiceLine(
                line=0,
                quantity="1000",
                base_unit="piece",
                unit_price="0.30",
                line_total="300.00",
                spec_id=ids["spec"],
                spec_revision=1,
                mapping_confirmed=True,
            ),
        ),
    )
    checked = check_invoice(delivered, issued)
    assert checked.status == "flagged" and checked.total_variance == 15
    check_id = uuid4()
    await insert(
        db,
        "invoice_checks",
        {
            "id": check_id,
            "business_id": ids["a"],
            "purchase_id": purchase,
            "purchase_revision": 1,
            "po_id": original["id"],
            "status": checked.status,
            "differences": Jsonb([d.model_dump(mode="json") for d in checked.differences]),
            "result": Jsonb(checked.model_dump(mode="json")),
            "input_snapshot": Jsonb(
                {
                    "invoice": delivered.model_dump(mode="json"),
                    "order": issued.model_dump(mode="json"),
                    "source_id": str(evidence),
                    "tolerances": checked.tolerances.model_dump(mode="json"),
                    "version": checked.version,
                }
            ),
        },
    )
    before = await (
        await db.execute("select * from collective.invoice_checks where id=%s", (check_id,))
    ).fetchone()
    rule = await proposed(db, ids)
    accepted = await change(db, ids, rule, "accept")
    await change(db, ids, {"id": rule["id"], "revision": accepted.revision}, "revoke")
    assert (
        await (
            await db.execute(
                "select * from collective.purchase_orders where id=%s", (original["id"],)
            )
        ).fetchone()
        == original
    )
    assert (
        await (
            await db.execute("select * from collective.invoice_checks where id=%s", (check_id,))
        ).fetchone()
        == before
    )
    assert before["result"]["total_variance"] == "15.00" and before["recovery_amount"] is None
    accuracy = await (
        await db.execute(
            "select * from collective.learned_rules where business_id=%s and kind='quote_accuracy'",
            (ids["a"],),
        )
    ).fetchone()
    await change(db, ids, accuracy, "accept")
    need = await needs.load_need(db, ids["a"], ids["need_a"])
    comparison = await needs.comparisons(db, need, datetime.now(UTC))
    warning = comparison.offers[0].accuracy_warnings[0]
    assert warning.mismatch_count == warning.checked_count == 1
    assert comparison.offers[0].cost.decision_cost == 300
    facts, _ = await liaison_jobs.draft_context(
        db,
        ids["a"],
        RequestDraft(
            kind="invoice_query",
            reference_id=check_id,
            expected_revision=1,
            supplier_id=ids["supplier"],
        ),
    )
    assert facts["accuracy_warnings"][0]["mismatch_count"] == 1
    await change(db, ids, {"id": accuracy["id"], "revision": accuracy["revision"] + 1}, "revoke")
    assert not (await needs.comparisons(db, need, datetime.now(UTC))).offers[0].accuracy_warnings


async def test_two_fee_samples_cannot_activate_and_need_flags_cannot_self_accept_usage(db, history):
    ids = history
    await set_context(db, "tenant", ids["owner_a"])
    need = await needs.load_need(db, ids["a"], ids["need_a"])
    forged = need | {
        "usage_input": {
            "stated_daily_rate": "125",
            "rule_daily_rate": "999",
            "rule_accepted": True,
            "rule_revision": 99,
        }
    }
    assert (await needs.options(db, forged, datetime.now(UTC))).replenishment.daily_rate == 125
    await operator(db)
    fresh = uuid4()
    await db.execute(
        "insert into collective.businesses(id,name,zone,locale) values (%s,'Fresh','sepang','en')",
        (fresh,),
    )
    await db.execute(
        "insert into collective.business_memberships values (%s,%s,'owner')",
        (fresh, ids["owner_a"]),
    )
    for amount, day in (("10.00", 1), ("20.00", 2)):
        await invoice(db, ids, fresh, amount, datetime(2026, 9, day).date())
    target = ids | {"a": fresh}
    rule = await proposed(db, target)
    assert rule["sample_count"] == 2
    with pytest.raises(DomainError) as error:
        await change(db, target, rule, "accept")
    assert error.value.code == "INSUFFICIENT_EVIDENCE"
    assert await accepted_rules.fee_rules(db, fresh) == ()


async def test_invoice_copies_do_not_increase_the_comparable_sample_count(db, history):
    ids = history
    original = await (
        await db.execute(
            """select * from collective.purchases
            where business_id=%s order by document_date limit 1""",
            (ids["a"],),
        )
    ).fetchone()
    lines = await (
        await db.execute(
            "select * from collective.purchase_lines where business_id=%s and purchase_id=%s",
            (ids["a"], original["id"]),
        )
    ).fetchall()
    for same_bytes in (True, False):
        if same_bytes:
            original_source = await (
                await db.execute(
                    "select * from collective.sources where business_id=%s and id=%s",
                    (ids["a"], original["source_id"]),
                )
            ).fetchone()
            with pytest.raises(psycopg.errors.UniqueViolation):
                async with db.transaction():
                    await insert(db, "sources", original_source | {"id": uuid4()})
            evidence = original["source_id"]
        else:
            evidence = await source(db, ids["a"], "another photo of the same invoice")
        identity = uuid4()
        values = original | {"id": identity, "source_id": evidence, "sealed": False}
        await insert(
            db,
            "purchases",
            {k: Jsonb(v) if isinstance(v, dict | list) else v for k, v in values.items()},
        )
        for line in lines:
            await insert(db, "purchase_lines", line | {"purchase_id": identity})
        await db.execute(
            "update collective.purchases set sealed=true where business_id=%s and id=%s",
            (ids["a"], identity),
        )
    await set_context(db, "coordinator", None)
    calculated, _ = await learning.collect(db, "private", ids["a"], datetime.now(UTC))
    fee = next(c for c in calculated if c.value.kind == "observed_fee")
    assert fee.sample_count == 3 and fee.value.amount == 15


async def test_http_rule_jobs_scope_and_human_lifecycle(api, access_seed, auth_headers):
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    from tests.fixtures import scenario
    from tests.integration.conftest import local_test_url
    from tests.integration.scenarios import clone_business_data

    client, database = api
    cohort = await clone_business_data(access_seed, "a", pool_opt_in=False)
    data = scenario()
    business = cohort(data["businesses"][0]["id"])
    context = {"supplier": cohort(data["suppliers"][0]["id"]), "spec": cohort(data["spec"]["id"])}
    async with await AsyncConnection.connect(
        local_test_url("MIGRATION_DATABASE_URL"), row_factory=dict_row
    ) as conn:
        for amount, day in (("10.00", 1), ("15.00", 2), ("20.00", 3)):
            await invoice(conn, context, business, amount, datetime(2026, 9, day).date())
    root = f"/v1/businesses/{business}/rules"
    response = await client.post(
        root + "/evaluate", headers=auth_headers("owner-a") | {"Idempotency-Key": "rule-http"}
    )
    assert response.status_code == 200, response.text
    identity = response.json()["id"]
    repeated = await client.post(
        root + "/evaluate", headers=auth_headers("owner-a") | {"Idempotency-Key": "rule-http"}
    )
    assert repeated.json()["id"] == identity
    async with database.transaction("coordinator", None) as conn:
        await conn.execute(
            "update collective.jobs set next_attempt_at='1900-01-01' where id=%s", (identity,)
        )

    async def handle(db, job, clock):
        assert str(job["id"]) == identity
        await rules.handle(db, job, clock)

    assert await Runner(database, handlers={"rule_proposal": handle}).run_once()
    page = await client.get(root, headers=auth_headers("owner-a"))
    assert page.status_code == 200 and page.json()["latest_job"]["status"] == "succeeded"
    rule = next(r for r in page.json()["items"] if r["kind"] == "observed_fee")
    assert rule["state"] == "proposed" and len(rule["evidence"]) == 3
    for account, status in (("staff", 403), ("owner-b", 404), ("admin", 404)):
        denied = await client.post(
            root + f"/{rule['id']}/accept",
            headers=auth_headers(account),
            json={"expected_revision": rule["revision"]},
        )
        assert denied.status_code == status
    accepted = await client.post(
        root + f"/{rule['id']}/accept",
        headers=auth_headers("owner-a"),
        json={"expected_revision": rule["revision"]},
    )
    assert accepted.status_code == 200 and accepted.json()["state"] == "accepted"
    assert "rule_proposal" in Runner(database).handlers


async def test_http_network_job_and_acceptance_return_only_qualifying_aggregates(
    api, access_seed, access_id, auth_headers
):
    from uuid import UUID

    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    from tests.fixtures import scenario
    from tests.integration.conftest import local_test_url

    client, database = api
    fixture = json.loads((ROOT / "fixtures/scenarios/five-business-history.json").read_text())
    data = scenario()
    network, businesses, evidence_ids = uuid4(), [], []
    context = {
        "supplier": access_id(data["suppliers"][0]["id"]),
        "spec": access_id(data["spec"]["id"]),
    }
    async with await AsyncConnection.connect(
        local_test_url("MIGRATION_DATABASE_URL"), row_factory=dict_row
    ) as conn:
        await conn.execute(
            "insert into collective.networks(id,name) values (%s,'Historical fixture')", (network,)
        )
        await conn.execute(
            "insert into collective.network_admins values (%s,%s)",
            (network, UUID(access_seed["accounts"]["admin"]["id"])),
        )
        for key in fixture["business_keys"]:
            business = uuid4()
            businesses.append(business)
            await conn.execute(
                """insert into collective.businesses(id,network_id,name,zone,locale)
                values (%s,%s,%s,'sepang','en')""",
                (business, network, key),
            )
            for template in fixture["invoices_per_business"]:
                purchase, evidence = await invoice(
                    conn,
                    context,
                    business,
                    template["delivery_fee"],
                    datetime.fromisoformat(template["at"]).date(),
                )
                evidence_ids.extend((purchase, evidence))
    root = f"/v1/networks/{network}/rules"
    assert (await client.get(root, headers=auth_headers("owner-a"))).status_code == 404
    requested = await client.post(
        root + "/evaluate", headers=auth_headers("admin") | {"Idempotency-Key": "network-fixture"}
    )
    assert requested.status_code == 200, requested.text
    identity = requested.json()["id"]
    async with database.transaction("coordinator", None) as conn:
        await conn.execute(
            "update collective.jobs set next_attempt_at='1900-01-01' where id=%s", (identity,)
        )

    async def handle(db, job, clock):
        assert str(job["id"]) == identity and job["business_id"] is None
        await rules.handle(db, job, clock)

    assert await Runner(database, handlers={"rule_proposal": handle}).run_once()
    response = await client.get(root, headers=auth_headers("admin"))
    assert response.status_code == 200 and response.json()["latest_job"]["status"] == "succeeded"
    rule = response.json()["items"][0]
    assert rule["sample_count"] == 15 and rule["distinct_businesses"] == 5
    accepted = await client.post(
        root + f"/{rule['id']}/accept",
        headers=auth_headers("admin"),
        json={"expected_revision": rule["revision"]},
    )
    assert accepted.status_code == 200 and accepted.json()["state"] == "accepted"
    for private_id in businesses + evidence_ids:
        assert str(private_id) not in accepted.text and str(private_id) not in response.text
    assert '"evidence"' not in accepted.text and '"quantity"' not in accepted.text
