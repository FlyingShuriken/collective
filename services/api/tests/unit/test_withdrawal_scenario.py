from collective.domain.approvals import ApprovalBinding, bind_approval, evaluate_release
from collective.domain.pools import evaluate_pool
from tests.fixtures import expectations, fixture_id, scenario_time
from tests.unit.test_pools import member


def test_canonical_withdrawal_reapproval_and_decline_branches():
    gold = expectations()
    cohort = [member(c) for c in "abc"]
    initial = evaluate_pool(cohort, scenario_time())
    caps = ("276.00", "285.00", "276.00")
    approvals = [
        bind_approval(
            m,
            approval_id=fixture_id("approval-" + str(m.need_id)),
            owner_id=fixture_id("owner-" + str(m.business_id)),
            cap=cap,
            expires_at=scenario_time(12),
            as_of=scenario_time(),
            pool_id=fixture_id("pool"),
        )
        for m, cap in zip(initial.members, caps, strict=True)
    ]
    assert evaluate_release(initial, approvals, scenario_time()).state == "ready"
    two = evaluate_pool(cohort[:2], scenario_time())
    assert all(str(m.decision_cost) == gold["after_c_withdraws"]["total_each"] for m in two.members)
    after_withdrawal = evaluate_release(two, approvals[:2], scenario_time())
    assert after_withdrawal.state == "awaiting_approvals"
    assert [a.state for a in after_withdrawal.approvals] == ["void", "valid"]
    void_a = ApprovalBinding.model_validate(approvals[0].model_dump() | {"state": "void"})
    assert (
        evaluate_release(two, [void_a, approvals[1]], scenario_time()).state == "awaiting_approvals"
    )
    fresh = bind_approval(
        two.members[0],
        approval_id=fixture_id("fresh-a"),
        owner_id=approvals[0].owner_id,
        cap="285.00",
        expires_at=scenario_time(12),
        as_of=scenario_time(),
        pool_id=fixture_id("pool"),
    )
    assert evaluate_release(two, [fresh, approvals[1]], scenario_time()).state == "ready"
    alone = evaluate_pool([cohort[1]], scenario_time())
    assert str(alone.members[0].decision_cost) == gold["after_a_declines"]["b_total"]
    decline = evaluate_release(alone, [approvals[1]], scenario_time())
    assert decline.approvals[0].state == "void" and decline.state == "ineligible"
