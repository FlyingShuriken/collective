from decimal import Decimal as D

from collective.domain.baselines import HistoricalBasis, historical_cost, solo_cost
from collective.domain.pools import NeedSnapshot
from tests.fixtures import fixture_id, scenario, scenario_time
from tests.unit.test_costs import quote


def need(letter="a", **changes):
    data = scenario()
    return NeedSnapshot.model_validate(
        {
            "id": fixture_id("need-" + letter),
            "business_id": fixture_id("cafe-" + letter),
            "revision": 1,
            "spec_id": data["spec"]["id"],
            "spec_revision": 1,
            "quantity": "1000",
            "base_unit": "piece",
            "pack_multiple": "100",
            "mapping_confirmed": True,
            "zone": "sepang",
            "pool_opt_in": True,
            "state": "selected",
            "saving_threshold": "0.05",
            "min_credit_days": 30,
            "delivery_mode": "per_business",
            "order_window": {"start": scenario_time(), "end": scenario_time(24)},
            "arrival_window": {"start": scenario_time(96), "end": scenario_time(120)},
        }
        | changes
    )


def history(letter="a", **changes):
    return HistoricalBasis.model_validate(
        {
            "id": fixture_id("history-" + letter),
            "revision": 1,
            "confirmed": True,
            "business_id": fixture_id("cafe-" + letter),
            "spec_id": scenario()["spec"]["id"],
            "spec_revision": 1,
            "base_unit": "piece",
            "unit_price": "0.30",
            "delivery_fee": "15.00",
            "fee_basis": "per_order",
            "delivery_mode": "per_business",
            "zone": "sepang",
            "credit_days": 30,
            "tax_rate": "0",
            "discount": "0.00",
            "surcharge": "0.00",
        }
        | changes
    )


def test_history_uses_confirmed_applicable_price_fee_basis():
    assert historical_cost(need(), history()) == D("315.00")
    assert historical_cost(need(quantity="2000"), history()) == D("615.00")
    assert historical_cost(need(), history(fee_basis="unknown")) is None
    assert historical_cost(need(), history(spec_id=fixture_id("other-spec"))) is None
    assert historical_cost(need(), history(confirmed=False)) is None


def test_solo_baseline_excludes_incomparable_credit_or_delivery():
    q = quote(business_id=need().business_id)
    assert solo_cost(need(), (q,), scenario_time(), ()).decision_cost == 315
    cheap_wrong_credit = quote(
        business_id=need().business_id,
        credit_days=0,
        tiers=[{"min_quantity": "1000", "unit_price": "0.20"}],
    )
    assert solo_cost(need(), (q, cheap_wrong_credit), scenario_time(), ()).decision_cost == 315
