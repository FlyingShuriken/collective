from decimal import Decimal as D

from collective.domain.replenishment import (
    LeadTime,
    StockMovement,
    StockSnapshot,
    UsageInput,
    estimate_replenishment,
)
from tests.fixtures import fixture_id, scenario_time


def stock(**changes):
    return StockSnapshot.model_validate(
        {"quantity": "750", "observed_at": scenario_time(), "movements": []} | changes
    )


def test_125_daily_750_stock_four_day_lead_and_two_day_buffer():
    result = estimate_replenishment(
        stock(),
        UsageInput(stated_daily_rate="125"),
        LeadTime(days="4", buffer_days="2"),
        scenario_time(),
    )
    assert result.run_out_at == scenario_time(144)
    assert result.reorder_at == scenario_time()
    assert result.stock_at_arrival == D("250")
    assert result.reorder_due


def test_accepted_usage_replaces_stated_but_unaccepted_does_not():
    usage = UsageInput(stated_daily_rate="125", rule_daily_rate="150", rule_accepted=False)
    args = (stock(), usage, LeadTime(days="4", buffer_days="0"), scenario_time())
    assert estimate_replenishment(*args).daily_rate == 125
    accepted = UsageInput.model_validate(
        usage.model_dump()
        | {
            "rule_accepted": True,
            "rule_revision": 2,
        }
    )
    result = estimate_replenishment(stock(), accepted, args[2], args[3])
    assert result.daily_rate == 150 and result.usage_rule_revision == 2


def test_future_order_can_extend_runout_but_invoice_alone_cannot():
    movement = StockMovement(
        id=fixture_id("incoming-1"),
        quantity="1000",
        at=scenario_time(48),
        kind="incoming",
        confirmed=True,
    )
    incoming = stock(movements=[movement])
    result = estimate_replenishment(
        incoming,
        UsageInput(stated_daily_rate="125"),
        LeadTime(days="4", buffer_days="2"),
        scenario_time(),
    )
    assert result.stock_at_arrival == 1250
    assert result.run_out_at == scenario_time(336)
    unconfirmed = StockMovement.model_validate(movement.model_dump() | {"confirmed": False})
    result = estimate_replenishment(
        stock(movements=[unconfirmed]),
        UsageInput(stated_daily_rate="125"),
        LeadTime(days="4", buffer_days="2"),
        scenario_time(),
    )
    assert result.stock_at_arrival == 250


def test_stock_observation_age_and_late_delivery_are_not_hidden():
    result = estimate_replenishment(
        stock(),
        UsageInput(stated_daily_rate="125"),
        LeadTime(days="1", buffer_days="0"),
        scenario_time(48),
    )
    assert result.stock_at_now == 500 and result.stock_at_arrival == 375
    late = StockMovement(
        id=fixture_id("late"),
        quantity="1000",
        at=scenario_time(168),
        kind="incoming",
        confirmed=True,
    )
    result = estimate_replenishment(
        stock(movements=[late]),
        UsageInput(stated_daily_rate="125"),
        LeadTime(days="4", buffer_days="0"),
        scenario_time(),
    )
    assert result.run_out_at == scenario_time(144)


def test_missing_evidence_has_no_fabricated_runout():
    result = estimate_replenishment(
        stock(), UsageInput(), LeadTime(days="4", buffer_days="2"), scenario_time()
    )
    assert result.failure_codes == ("MISSING_USAGE",) and result.run_out_at is None
    result = estimate_replenishment(
        None,
        UsageInput(stated_daily_rate="125"),
        LeadTime(days="4", buffer_days="2"),
        scenario_time(),
    )
    assert result.failure_codes == ("MISSING_STOCK",)
