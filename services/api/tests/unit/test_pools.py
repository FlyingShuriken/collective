from itertools import permutations

import pytest

from collective.domain.pools import MemberSnapshot, discover_pool, evaluate_pool
from tests.fixtures import fixture_id, scenario_time
from tests.unit.test_baselines import history, need
from tests.unit.test_costs import quote


def member(letter="a", **changes):
    n = need(letter)
    q = quote(business_id=n.business_id, id=fixture_id("quote-" + letter + "-b"))
    return MemberSnapshot.model_validate(
        {
            "need": n,
            "quote": q,
            "solo_quotes": [q],
            "history": history(letter),
        }
        | changes
    )


def test_three_members_individually_save_60_and_two_save_30():
    members = [member(letter) for letter in "abc"]
    evaluation = evaluate_pool(members, scenario_time())
    assert evaluation.eligible and evaluation.aggregate_quantity == 3000
    assert all(m.decision_cost == 255 and m.pooling_saving == 60 for m in evaluation.members)
    two = evaluate_pool(members[:2], scenario_time())
    assert all(m.decision_cost == 285 and m.pooling_saving == 30 for m in two.members)


def test_history_saving_does_not_mean_pool_is_cheaper_than_current_solo():
    a = member()
    cheaper_solo = quote(
        business_id=a.need.business_id,
        pooled_buyers_permitted=False,
        tiers=[{"min_quantity": "1000", "unit_price": "0.255"}],
    )
    tiers = [
        {"min_quantity": "1000", "unit_price": "0.30"},
        {"min_quantity": "2000", "unit_price": "0.265"},
    ]
    a = member(quote=quote(business_id=a.need.business_id, tiers=tiers), solo_quotes=[cheaper_solo])
    b = member("b", quote=quote(business_id=need("b").business_id, tiers=tiers))
    result = evaluate_pool([a, b], scenario_time())
    assert not result.eligible
    assert result.members[0].historical_comparable_cost == 315
    assert result.members[0].current_solo_comparable_cost == 270
    assert result.members[0].decision_cost == 280
    assert "NO_SOLO_IMPROVEMENT" in result.members[0].failure_codes


@pytest.mark.parametrize(
    "change,code",
    [
        ({"mapping_confirmed": False}, "UNCONFIRMED_SPEC"),
        ({"zone": "kl"}, "NO_COMMON_ZONE"),
        ({"pool_opt_in": False}, "POOLING_NOT_OPTED_IN"),
        ({"state": "released"}, "NEED_COMMITTED"),
        ({"spec_id": fixture_id("different-spec")}, "SPEC_MISMATCH"),
        ({"quantity": "1050"}, "INVALID_PACK_MULTIPLE"),
        (
            {"arrival_window": {"start": scenario_time(144), "end": scenario_time(168)}},
            "NO_WINDOW_OVERLAP",
        ),
    ],
)
def test_invalid_member_cannot_hide_in_group_savings(change, code):
    bad = member(need=need(**change))
    result = evaluate_pool([bad, member("b")], scenario_time())
    assert not result.eligible
    assert code in result.members[0].failure_codes or code in result.failure_codes


def test_missing_baseline_or_unsupported_supplier_terms_block_pool():
    no_history = member(history=None)
    assert (
        "NO_COMPARABLE_BASELINE"
        in evaluate_pool([no_history, member("b")], scenario_time()).members[0].failure_codes
    )
    no_permission = member(quote=quote(pooled_buyers_permitted=False))
    assert not evaluate_pool([no_permission, member("b")], scenario_time()).eligible


def test_subset_discovery_recomputes_actual_prices_and_is_stable():
    a, b, c = member(), member("b"), member("c", history=history("c", unit_price="0.20"))
    for ordering in permutations([a, b, c]):
        result = discover_pool(list(ordering), a.need.id, scenario_time())
        assert result is not None and result.eligible
        assert {m.need_id for m in result.members} == {a.need.id, b.need.id}
        assert all(m.decision_cost == 285 for m in result.members)


def test_duplicate_business_or_reserved_need_cannot_form_a_new_pool():
    assert not evaluate_pool([member(), member()], scenario_time()).eligible
    reserved = member(need=need(state="reserved", reserved_pool_id=fixture_id("existing-pool")))
    assert discover_pool([reserved, member("b")], reserved.need.id, scenario_time()) is None
    assert evaluate_pool(
        [reserved, member("b")], scenario_time(), pool_id=fixture_id("existing-pool")
    ).eligible
