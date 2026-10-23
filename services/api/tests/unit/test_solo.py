from collective.domain.pools import evaluate_pool, evaluate_solo
from collective.domain.types import TimeWindow
from tests.fixtures import scenario_time
from tests.unit.test_pools import member


def test_solo_does_not_require_history_or_pool_permissions():
    original = member()
    solo = original.model_copy(update={
        "history": None,
        "need": original.need.model_copy(update={"pool_opt_in": False}),
        "quote": original.quote.model_copy(update={"pooled_buyers_permitted": False}),
    })
    result = evaluate_solo(solo, scenario_time())
    assert result.eligible and result.decision_cost == 315
    assert result.historical_comparable_cost is None
    assert not evaluate_pool([solo], scenario_time()).eligible


def test_solo_and_pool_cannot_execute_before_the_order_window_opens():
    original = member()
    future = original.model_copy(update={"need": original.need.model_copy(update={
        "order_window": TimeWindow(start=scenario_time(1), end=scenario_time(24)),
    })})
    assert "ORDER_WINDOW_NOT_OPEN" in evaluate_solo(future, scenario_time()).failure_codes
    pooled = evaluate_pool([future, member("b")], scenario_time())
    assert "ORDER_WINDOW_NOT_OPEN" in pooled.members[0].failure_codes
