from datetime import timedelta

import pytest

from collective.domain.approvals import (
    ApprovalBinding,
    ApprovalError,
    assert_pre_release,
    bind_approval,
    evaluate_approval,
    evaluate_release,
)
from collective.domain.pools import evaluate_pool
from tests.fixtures import fixture_id, scenario_time
from tests.unit.test_baselines import need
from tests.unit.test_costs import quote
from tests.unit.test_pools import member


def binding(evaluation=None, cap="276.00", **changes):
    evaluation = evaluation or evaluate_pool([member(c) for c in "abc"], scenario_time()).members[0]
    return bind_approval(
        evaluation,
        approval_id=fixture_id("approval-a"),
        owner_id=fixture_id("owner-a"),
        cap=cap,
        expires_at=scenario_time(12),
        as_of=scenario_time(),
        pool_id=fixture_id("pool"),
        **changes,
    )


def test_exact_cap_stands_with_tier_repricing():
    approved = binding(cap="285.00")
    current = evaluate_pool([member(), member("b")], scenario_time()).members[0]
    result = evaluate_approval(approved, current, scenario_time())
    assert result.state == "valid" and result.reason == "STANDS"
    assert approved.cap == 285


def test_exact_expiry_and_immutable_binding():
    approved = binding()
    current = evaluate_pool([member(c) for c in "abc"], scenario_time()).members[0]
    assert evaluate_approval(approved, current, scenario_time(12)).state == "expired"
    assert (
        evaluate_approval(approved, current, scenario_time(12) - timedelta(microseconds=1)).state
        == "valid"
    )
    with pytest.raises(ValueError):
        approved.cap = "999.00"


@pytest.mark.parametrize(
    "quote_change,need_change",
    [
        ({"supplier_id": fixture_id("other-supplier")}, {}),
        ({"spec_revision": 2}, {"spec_revision": 2}),
        ({}, {"quantity": "2000"}),
        ({"credit_days": 45}, {}),
        ({"delivery_fee": "10.00"}, {}),
        ({"tax_rate": "0.01"}, {}),
        ({"conditions": "Pay before dispatch"}, {}),
        ({}, {"arrival_window": {"start": scenario_time(100), "end": scenario_time(120)}}),
    ],
)
def test_changed_bound_terms_void_even_if_cheaper(quote_change, need_change):
    approved = binding(cap="999.00")
    changed = member(
        need=need(**need_change),
        quote=quote(business_id=need().business_id, **quote_change),
    )
    current = evaluate_pool([changed, member("b"), member("c")], scenario_time()).members[0]
    result = evaluate_approval(approved, current, scenario_time())
    assert result.state == "void" and result.reason == "BOUND_TERMS_CHANGED"


def test_void_permission_never_revives_and_fresh_approval_is_new():
    approved = binding()
    void = ApprovalBinding.model_validate(approved.model_dump() | {"state": "void"})
    current = evaluate_pool([member(c) for c in "abc"], scenario_time()).members[0]
    assert evaluate_approval(void, current, scenario_time()).state == "void"
    fresh = bind_approval(
        current,
        approval_id=fixture_id("fresh-a"),
        owner_id=approved.owner_id,
        cap="276.00",
        expires_at=scenario_time(12),
        as_of=scenario_time(),
        pool_id=approved.pool_id,
    )
    assert fresh.id != approved.id and fresh.state == "valid"


def test_release_ready_is_distinct_from_issued_and_missing_approval_blocks():
    pool = evaluate_pool([member(c) for c in "abc"], scenario_time())
    approvals = [
        bind_approval(
            m,
            approval_id=fixture_id("approval-" + str(m.need_id)),
            owner_id=fixture_id("owner-" + str(m.business_id)),
            cap="285.00",
            expires_at=scenario_time(12),
            as_of=scenario_time(),
            pool_id=fixture_id("pool"),
        )
        for m in pool.members
    ]
    assert evaluate_release(pool, approvals, scenario_time()).state == "ready"
    assert evaluate_release(pool, approvals[:2], scenario_time()).state == "awaiting_approvals"
    assert evaluate_release(pool, approvals, scenario_time(), released=True).state == "released"
    with pytest.raises(ApprovalError, match="ALREADY_RELEASED"):
        assert_pre_release("released")


def test_approval_cannot_start_under_current_cost_or_after_deadline():
    with pytest.raises(ApprovalError, match="CAP_EXCEEDED"):
        binding(cap="250.00")
    current = evaluate_pool([member(c) for c in "abc"], scenario_time()).members[0]
    with pytest.raises(ApprovalError, match="INVALID_EXPIRY"):
        bind_approval(
            current,
            approval_id=fixture_id("late"),
            owner_id=fixture_id("owner-a"),
            cap="276.00",
            expires_at=scenario_time(),
            as_of=scenario_time(),
            pool_id=fixture_id("pool"),
        )
