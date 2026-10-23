from decimal import Decimal as D

import pytest

from collective.domain.costs import DeliveryContext, FeeRule, Quote, calculate_cost
from tests.fixtures import fixture_id, scenario, scenario_time


def quote(**changes):
    data = scenario()["quotes"][1] | {
        "spec_revision": 1,
        "pack_multiple": "100",
        "zone": "sepang",
        "valid_from": scenario_time(-1),
        "valid_until": scenario_time(48),
        "delivery_window": {"start": scenario_time(96), "end": scenario_time(120)},
    }
    return Quote.model_validate(data | changes)


def context(quantity="1000", members=1):
    return DeliveryContext(
        aggregate_quantity=quantity,
        member_count=members,
        as_of=scenario_time(),
        member_quantities=tuple(D(quantity) / members for _ in range(members)),
    )


@pytest.mark.parametrize(
    "quantity,price",
    [
        ("1999", "0.30"),
        ("2000", "0.27"),
        ("2999", "0.27"),
        ("3000", "0.24"),
    ],
)
def test_inclusive_all_unit_tier_boundaries(quantity, price):
    result = calculate_cost(D(quantity), quote(pack_multiple="1"), context(quantity), None)
    assert result.unit_price == D(price)


@pytest.mark.parametrize("members,expected", [(1, "315.00"), (2, "285.00"), (3, "255.00")])
def test_canonical_costs_are_not_hardcoded(members, expected):
    result = calculate_cost(D("1000"), quote(), context(str(members * 1000), members), None)
    assert result.decision_cost == D(expected)
    assert result.agreed_cost == result.expected_cost
    assert result.fee_unverified
    assert result.quote_revision == 1
    assert result.goods == D(expected) - 15


def test_discounted_goods_do_not_cross_delivery_threshold():
    q = quote(waive_split_drop_fees=True)
    result = calculate_cost(D("1000"), q, context("3000", 3), None)
    assert result.threshold_goods == D("720.00")
    assert result.delivery_fee == 15
    assert not result.free_delivery_qualified
    qualified = calculate_cost(D("2000"), q, context("6000", 3), None)
    assert qualified.threshold_goods == D("1440.00")
    assert qualified.delivery_fee == 0
    split = calculate_cost(D("2000"), quote(), context("6000", 3), None)
    assert split.delivery_fee == 15


def test_discount_tax_surcharge_and_rounding():
    result = calculate_cost(
        D("1000"),
        quote(
            tiers=[{"min_quantity": "1000", "unit_price": "0.300005"}],
            discount="10.00",
            tax_rate="0.06",
            surcharge="2.00",
        ),
        context(),
        None,
    )
    assert result.goods == D("300.01")
    assert result.tax == D("18.42")  # Confirmed rate on goods minus discount plus fees.
    assert result.agreed_cost == D("325.43")


def test_accepted_fee_warning_never_rewrites_the_agreement():
    fee = FeeRule(
        id=fixture_id("fee-rule"),
        revision=1,
        accepted=True,
        sample_count=3,
        business_id=quote().business_id,
        supplier_id=quote().supplier_id,
        zone="sepang",
        delivery_mode="per_business",
        amount="15.00",
    )
    result = calculate_cost(D("1000"), quote(delivery_fee="0.00"), context(), fee)
    assert (result.agreed_cost, result.expected_cost, result.decision_cost) == (
        D("300.00"),
        D("315.00"),
        D("315.00"),
    )
    lower = FeeRule.model_validate(fee.model_dump() | {"amount": "0.00"})
    result = calculate_cost(D("1000"), quote(), context(), lower)
    assert result.expected_cost == 300 and result.decision_cost == 315
    unaccepted = FeeRule.model_validate(fee.model_dump() | {"accepted": False})
    assert calculate_cost(D("1000"), quote(), context(), unaccepted).fee_unverified


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"delivery_fee": None}, "UNKNOWN_DELIVERY"),
        ({"tax_rate": None}, "UNKNOWN_TAX"),
        ({"confirmed": False}, "UNCONFIRMED_QUOTE"),
        ({"currency": "USD"}, "UNSUPPORTED_CURRENCY"),
        ({"tier_kind": "incremental"}, "UNSUPPORTED_TIER_KIND"),
        ({"valid_until": scenario_time()}, "QUOTE_EXPIRED"),
        ({"valid_from": scenario_time(1)}, "QUOTE_NOT_ACTIVE"),
        ({"per_business_moq": "1100"}, "MOQ_NOT_MET"),
        ({"aggregate_moq": "1100"}, "MOQ_NOT_MET"),
    ],
)
def test_missing_or_invalid_terms_are_unavailable(patch, code):
    result = calculate_cost(D("1000"), quote(**patch), context(), None)
    assert code in result.failure_codes
    assert result.decision_cost is None


def test_collection_needs_confirmed_shares_and_location():
    q = quote(delivery_mode="collection_point", delivery_location="Hub A", delivery_fee="30.00")
    assert (
        "UNCONFIRMED_COLLECTION_SHARES"
        in calculate_cost(D("1000"), q, context("3000", 3), None).failure_codes
    )
    ctx = DeliveryContext(
        aggregate_quantity="3000",
        member_count=3,
        as_of=scenario_time(),
        collection_location="Hub A",
        collection_shares=("10.00", "10.00", "10.00"),
        member_quantities=("1000", "1000", "1000"),
        own_collection_share="10.00",
    )
    assert calculate_cost(D("1000"), q, ctx, None).agreed_cost == D("250.00")


def test_invalid_tiers_and_pack_quantity_are_rejected():
    with pytest.raises(ValueError):
        quote(tiers=[{"min_quantity": "1000", "unit_price": "0.30"}] * 2)
    result = calculate_cost(D("1050"), quote(), context("1050"), None)
    assert result.failure_codes == ("INVALID_PACK_MULTIPLE",)
