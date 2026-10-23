from collective.domain.quantities import QuantityConstraints, QuantityNeed, suggest_quantities
from tests.fixtures import scenario_time
from tests.unit.test_costs import quote


def constraints(**changes):
    return QuantityConstraints.model_validate(
        {
            "max_stock": "4000",
            "stock_at_arrival": "250",
            "usage_per_day": "125",
            "perishable": False,
            "shelf_life_days": None,
        }
        | changes
    )


def test_at_most_three_options_with_cash_and_like_quantity_savings():
    options = suggest_quantities(
        QuantityNeed(quantity="1000", pack_multiple="100"),
        [quote()],
        constraints(),
        scenario_time(),
    )
    assert [o.quantity for o in options] == [1000, 2000, 3000]
    assert options[0].cost == 315
    assert options[1].cost == 555
    assert options[1].extra_cash_now == 240
    assert options[1].saving_vs_repeat_orders == 75
    assert options[1].coverage_days == 18
    assert "same_confirmed_terms_for_repeat_orders" in options[1].assumptions


def test_capacity_includes_stock_at_arrival_and_does_not_enlarge_selected_need():
    need = QuantityNeed(quantity="1000", pack_multiple="100", selected=True)
    options = suggest_quantities(need, [quote()], constraints(max_stock="2500"), scenario_time())
    assert [o.quantity for o in options] == [1000]
    suggested = QuantityNeed(quantity="1000", pack_multiple="100")
    options = suggest_quantities(
        suggested, [quote()], constraints(max_stock="2500"), scenario_time()
    )
    assert [o.quantity for o in options] == [1000, 2000]
    assert suggested.quantity == 1000


def test_perishables_have_no_extra_bulk_and_shelf_limit_is_hard():
    need = QuantityNeed(quantity="1000", pack_multiple="100")
    assert (
        len(
            suggest_quantities(
                need, [quote()], constraints(perishable=True, shelf_life_days="20"), scenario_time()
            )
        )
        == 1
    )
    assert (
        suggest_quantities(
            need, [quote()], constraints(perishable=True, shelf_life_days="5"), scenario_time()
        )
        == []
    )
    assert (
        suggest_quantities(
            need,
            [quote()],
            constraints(perishable=True, shelf_life_days="20", usage_per_day=None),
            scenario_time(),
        )
        == []
    )


def test_pack_rounding_and_unknown_usage_do_not_invent_bulk_benefit():
    options = suggest_quantities(
        QuantityNeed(quantity="1001", pack_multiple="100"),
        [quote()],
        constraints(usage_per_day=None),
        scenario_time(),
    )
    assert [o.quantity for o in options] == [1100]
    assert options[0].coverage_days is None


def test_solo_collection_uses_the_confirmed_full_collection_charge():
    options = suggest_quantities(
        QuantityNeed(quantity="1000", pack_multiple="100", selected=True),
        [quote(delivery_mode="collection_point", delivery_location="Confirmed hub")],
        constraints(),
        scenario_time(),
    )
    assert options[0].cost == 315
