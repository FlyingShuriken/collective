import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from collective.domain.invoices import InvoiceCheckResult, InvoiceTolerances, check_invoice
from collective.domain.learning import (
    AccuracyContext,
    AccuracyEvidence,
    EvidenceReference,
    FeeContext,
    FeeEvidence,
    StockEvidence,
    UsageContext,
    accuracy_proposal,
    fee_proposal,
    usage_proposal,
)
from tests.fixtures import ROOT, fixture_id, scenario, scenario_time
from tests.unit.test_invoice_variances import fixtures

USAGE = UsageContext(spec_id=scenario()["spec"]["id"], spec_revision=1, base_unit="piece")
FEE = FeeContext(supplier_id=fixture_id("supplier-a"), zone="sepang", delivery_mode="per_business")
ACCURACY = AccuracyContext(supplier_id=FEE.supplier_id)


def reference(key, business="a"):
    return EvidenceReference(
        business_id=fixture_id("cafe-" + business),
        source_id=fixture_id("source-" + key),
        record_id=fixture_id(key),
    )


def stock(key, quantity, hours, kind="stock", **changes):
    return StockEvidence.model_validate(
        {
            "reference": reference(key),
            "context": USAGE,
            "quantity": quantity,
            "at": scenario_time(hours),
            "kind": kind,
            "confirmed": True,
        }
        | changes
    )


def fee(key, amount="15.00", **changes):
    return FeeEvidence.model_validate(
        {
            "reference": reference(key),
            "context": FEE,
            "amount": amount,
            "at": scenario_time(-24),
            "confirmed": True,
        }
        | changes
    )


def checked(key, result=None, **changes):
    if result is None:
        result = check_invoice(*fixtures())
    return AccuracyEvidence.model_validate(
        {
            "reference": reference(key),
            "context": ACCURACY,
            "purchase_id": fixture_id("purchase-" + key),
            "purchase_revision": 1,
            "po_id": fixture_id("po-" + key),
            "at": scenario_time(-48),
            "checked_at": scenario_time(-24),
            "confirmed": True,
            "result": result,
        }
        | changes
    )


def passed():
    return InvoiceCheckResult(
        status="passed",
        differences=(),
        missing_fields=(),
        failure_codes=(),
        total_variance="0.00",
        complete=True,
        tolerances=InvoiceTolerances(),
    )


def test_measured_usage_counts_receipts_adjustments_and_waste_not_invoice_dates():
    rows = [
        stock("open", "600", 0),
        stock("receipt", "400", 24, "receipt"),
        stock("inbound", "100", 24, "inbound_adjustment"),
        stock("waste", "100", 48, "waste"),
        stock("close", "500", 96),
        stock("next", "100", 192),
    ]
    result = usage_proposal(rows, USAGE, scenario_time(192))
    assert result.value.daily_rate == D("125")
    assert result.value.slowest_daily_rate == D("100")
    assert result.sample_count == 2 and len(result.evidence) == 6
    assert result.period_start == scenario_time() and result.period_end == scenario_time(192)
    assert result.eligible_private and not result.eligible_network
    assert "UNREPORTED_STOCK_MOVEMENTS_UNKNOWN" in result.uncertainty
    assert usage_proposal(rows[:1], USAGE, scenario_time(192)) is None
    assert usage_proposal([], USAGE, scenario_time(192)) is None


def test_usage_does_not_mix_businesses_specs_revisions_unconfirmed_or_future_receipts():
    rows = [
        stock("open", "1000", 0),
        stock("close", "0", 192),
        stock("other-business", "400", 24, "receipt", reference=reference("x", "b")),
        stock("unconfirmed", "100", 24, "receipt", confirmed=False),
        stock("future", "100", 216, "receipt"),
        stock(
            "revision", "100", 24, "receipt", context=USAGE.model_copy(update={"spec_revision": 2})
        ),
    ]
    result = usage_proposal(rows, USAGE, scenario_time(192))
    assert result.value.daily_rate == 125 and result.sample_count == 1
    assert result.excluded_count == 3 and len(result.evidence) == 2


def test_usage_rejects_impossible_balance_and_conflicting_counts_without_precise_prediction():
    assert (
        usage_proposal([stock("o", "10", 0), stock("c", "11", 24)], USAGE, scenario_time(24))
        is None
    )
    with pytest.raises(ValueError, match="Conflicting stock"):
        usage_proposal([stock("o", "10", 0), stock("c", "11", 0)], USAGE, scenario_time())
    assert (
        usage_proposal([stock("o", "10", 0), stock("c", "10", 24)], USAGE, scenario_time(24))
        is None
    )
    # Repeated identical counts at one time are not a second elapsed interval.
    assert (
        usage_proposal([stock("o", "10", 0), stock("same", "10", 0)], USAGE, scenario_time())
        is None
    )


def test_usage_uses_elapsed_seconds_and_rounds_conservative_rate_up():
    result = usage_proposal([stock("o", "1", 0), stock("c", "0", 72)], USAGE, scenario_time(72))
    assert result.value.daily_rate == D("0.333334")
    result = usage_proposal([stock("o", "2", 0), stock("c", "1", 12)], USAGE, scenario_time(12))
    assert result.value.daily_rate == 2
    with pytest.raises(ValidationError, match="whole"):
        stock("fractional-piece", "0.5", 0)


def test_fee_needs_three_comparable_confirmed_invoices_not_three_reads():
    one, two, three = fee("one", "10.00"), fee("two", "15.00"), fee("three", "20.00")
    assert not fee_proposal([one, one, one], FEE, scenario_time()).eligible_private
    assert not fee_proposal([one, two], FEE, scenario_time()).eligible_private
    result = fee_proposal([one, two, three], FEE, scenario_time())
    assert result.eligible_private and not result.eligible_network
    assert result.sample_count == 3 and result.value.amount == 15
    assert (result.value.minimum, result.value.maximum) == (10, 20)
    assert result.value.basis == "mean_confirmed_invoice_delivery"
    assert "INVOICED_FEE_NOT_PAYMENT_OR_AGREEMENT" in result.uncertainty


@pytest.mark.parametrize(
    "change",
    [
        {"context": FEE.model_copy(update={"zone": "other"})},
        {"context": FEE.model_copy(update={"supplier_id": fixture_id("other-supplier")})},
        {
            "context": FeeContext(
                supplier_id=FEE.supplier_id,
                zone="sepang",
                delivery_mode="collection_point",
                delivery_location="hub",
            )
        },
        {"confirmed": False},
        {"at": scenario_time(1)},
        {"amount": None},
    ],
)
def test_fee_excludes_incomparable_missing_unconfirmed_and_future_samples(change):
    rows = [fee("one"), fee("two"), fee("excluded", **change)]
    result = fee_proposal(rows, FEE, scenario_time())
    assert result.sample_count == 2 and not result.eligible_private
    assert result.excluded_count == 1


def test_fee_zero_is_real_evidence_and_decimal_rounding_is_explicit():
    result = fee_proposal(
        [fee("a", "0.00"), fee("b", "0.00"), fee("c", "0.01")], FEE, scenario_time()
    )
    assert result.eligible_private and result.value.amount == D("0.00")
    assert result.value.maximum == D("0.01")
    with pytest.raises(ValidationError):
        fee("float", 0.1)
    with pytest.raises(ValidationError):
        FeeContext(supplier_id=FEE.supplier_id, zone="sepang", delivery_mode="collection_point")


def test_fee_corrections_replace_the_old_sample_instead_of_meeting_the_threshold():
    original = fee("original")
    corrected = fee(
        "corrected",
        "20.00",
        reference=original.reference.model_copy(
            update={"revision": 2, "source_id": fixture_id("corrected-source")},
        ),
    )
    result = fee_proposal([original, corrected, fee("other", "10.00")], FEE, scenario_time())
    assert result.sample_count == 2 and result.value.amount == 15
    assert result.excluded_count == 1 and not result.eligible_private
    missing = fee("missing", None, reference=corrected.reference.model_copy(update={"revision": 3}))
    result = fee_proposal([original, corrected, missing, fee("other")], FEE, scenario_time())
    assert result.sample_count == 1


def test_accuracy_denominator_only_complete_unique_matched_confirmed_checks():
    rows = [
        checked("flag"),
        checked("pass", passed()),
        checked("unmatched", check_invoice(fixtures()[0], None)),
        checked("incomplete", check_invoice(*fixtures()).model_copy(update={"complete": False})),
        checked("unconfirmed", confirmed=False),
        checked("future", checked_at=scenario_time(1)),
        checked("wrong-supplier", context=AccuracyContext(supplier_id=fixture_id("other"))),
    ]
    result = accuracy_proposal(rows + [rows[0]], ACCURACY, scenario_time())
    assert result.value.mismatch_count == 1 and result.value.checked_count == 2
    assert result.sample_count == 2 and result.excluded_count == 5
    assert len(result.evidence) == 2
    assert "DISCREPANCIES_DO_NOT_ESTABLISH_SUPPLIER_FAULT" in result.uncertainty


def test_accuracy_does_not_count_repeat_checks_or_old_purchase_revisions():
    original = checked("original")
    repeated = checked(
        "repeat", passed(), purchase_id=original.purchase_id, checked_at=scenario_time(-23)
    )
    result = accuracy_proposal([original, repeated], ACCURACY, scenario_time())
    assert result.value.checked_count == 1 and result.value.mismatch_count == 0
    corrected = checked(
        "correction",
        passed(),
        purchase_id=original.purchase_id,
        purchase_revision=2,
        checked_at=scenario_time(-24),
    )
    result = accuracy_proposal([repeated, original, corrected], ACCURACY, scenario_time())
    assert result.value.checked_count == 1 and result.excluded_count == 2
    unmatched = checked(
        "unmatched",
        check_invoice(fixtures()[0], None),
        purchase_id=original.purchase_id,
        purchase_revision=3,
        po_id=None,
    )
    assert accuracy_proposal([original, unmatched], ACCURACY, scenario_time()) is None


def test_accuracy_preserves_subthreshold_differences_without_counting_them_as_mismatch():
    invoice, order = fixtures()
    result = check_invoice(invoice, order)
    small = next(d for d in result.differences if d.kind == "unit_price")
    reading = result.model_copy(update={"differences": (small,), "status": "passed"})
    proposal = accuracy_proposal([checked("subthreshold", reading)], ACCURACY, scenario_time())
    assert not small.material and proposal.value.mismatch_count == 0
    assert proposal.value.checked_count == 1


def test_proposal_digest_is_order_and_duplicate_stable_but_binds_actual_evidence():
    rows = [fee("one"), fee("two"), fee("three")]
    result = fee_proposal(rows, FEE, scenario_time())
    assert result == fee_proposal(list(reversed(rows)) + [rows[0]], FEE, scenario_time(1))
    altered = fee("one", "16.00")
    assert (
        fee_proposal([altered, *rows[1:]], FEE, scenario_time()).evidence_digest
        != result.evidence_digest
    )
    with pytest.raises(ValueError, match="Conflicting values"):
        fee_proposal([*rows, altered], FEE, scenario_time())
    with pytest.raises(ValueError, match="timezone-aware"):
        fee_proposal(rows, FEE, datetime(2026, 10, 23))


def test_equivalent_timezones_do_not_change_evidence_identity_or_proposal_digest():
    original = fee("one")
    local = fee("one", at=original.at.astimezone(timezone(timedelta(hours=8))))
    assert fee_proposal([original], FEE, scenario_time()) == fee_proposal(
        [local, original],
        FEE,
        scenario_time(),
    )


def test_five_business_history_has_independent_counts_and_network_threshold():
    fixture = json.loads((ROOT / "fixtures/scenarios/five-business-history.json").read_text())
    as_of = datetime.fromisoformat(fixture["as_of"])
    context = FeeContext(supplier_id=fixture["supplier_id"], **fixture["context"])
    rows, checks = [], []
    for business in fixture["business_keys"]:
        for invoice in fixture["invoices_per_business"]:
            key = business + ":" + invoice["key"]
            ref = EvidenceReference(
                business_id=fixture_id(business),
                source_id=fixture_id(key + ":source"),
                record_id=fixture_id(key + ":purchase"),
            )
            at = datetime.fromisoformat(invoice["at"])
            rows.append(fee(key, invoice["delivery_fee"], reference=ref, context=context, at=at))
            checks.append(
                checked(
                    key,
                    None if invoice["mismatch"] else passed(),
                    reference=ref.model_copy(update={"record_id": fixture_id(key + ":check")}),
                    purchase_id=ref.record_id,
                    context=AccuracyContext(supplier_id=context.supplier_id),
                    at=at,
                    checked_at=at + timedelta(hours=1),
                )
            )
    expected = fixture["expected"]
    result = fee_proposal(rows, context, as_of)
    assert result.sample_count == expected["network_sample_count"] == 15
    assert result.distinct_businesses == expected["distinct_businesses"] == 5
    assert str(result.value.amount) == expected["observed_mean_fee"]
    assert result.eligible_network is expected["five_business_network_eligible"]
    assert (
        fee_proposal(rows[:9], context, as_of).eligible_network
        is expected["three_business_network_eligible"]
    )
    assert fee_proposal(rows[:3], context, as_of).sample_count == expected["private_sample_count"]
    accuracy = accuracy_proposal(checks, AccuracyContext(supplier_id=context.supplier_id), as_of)
    assert accuracy.value.mismatch_count == expected["mismatch_count"]
    assert accuracy.value.checked_count == expected["checked_count"]
    assert accuracy.period_start == datetime.fromisoformat("2026-07-01T01:00:00Z")
    assert accuracy.period_end == datetime.fromisoformat("2026-09-01T01:00:00Z")
