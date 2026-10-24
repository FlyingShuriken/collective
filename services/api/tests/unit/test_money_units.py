from decimal import Decimal as D

import pytest
from pydantic import TypeAdapter, ValidationError

from collective.domain.money import round_money
from collective.domain.types import DecimalValue, Money, SignedMoney
from collective.domain.units import PackMapping, SourceLine, normalize_line


def test_money_uses_half_up_at_exact_boundaries():
    assert round_money(D("1.005")) == D("1.01")
    assert round_money(D("1.004999")) == D("1.00")
    assert round_money(D("-1.005")) == D("-1.01")
    with pytest.raises(ValueError):
        round_money(D("10000000000000000"))


def test_money_precision_matches_numeric_18_2_without_narrowing_quantity_precision():
    assert TypeAdapter(Money).validate_python("9999999999999999.99") == D("9999999999999999.99")
    assert TypeAdapter(SignedMoney).validate_python("-9999999999999999.99") == D(
        "-9999999999999999.99"
    )
    for value in ("10000000000000000.00", "1.005", "NaN", "Infinity", 1.01):
        for schema in (Money, SignedMoney):
            with pytest.raises(ValidationError):
                TypeAdapter(schema).validate_python(value)
    with pytest.raises(ValidationError):
        TypeAdapter(Money).validate_python("-0.01")
    with pytest.raises(ValidationError):
        TypeAdapter(DecimalValue).validate_python("1000000000000")


def test_confirmed_pack_hierarchy_preserves_original_price():
    result = normalize_line(
        SourceLine(quantity="2", unit="carton", unit_price="300.00", currency="MYR"),
        PackMapping(base_unit="piece", factors=("10", "100"), confirmed=True),
    )
    assert result.quantity == D("2000")
    assert result.unit_price == D("0.30")
    assert result.original_unit_price == D("300.00")
    assert result.original_quantity == D("2")


@pytest.mark.parametrize(
    "line,mapping,code",
    [
        ({"unit": "carton"}, None, "UNCONFIRMED_PACK"),
        (
            {"unit": "carton"},
            {"base_unit": "piece", "factors": ["100"], "confirmed": False},
            "UNCONFIRMED_PACK",
        ),
        ({"currency": "USD"}, None, "UNSUPPORTED_CURRENCY"),
        ({"unit": "oz", "base_unit": "ml"}, None, "AMBIGUOUS_UNIT"),
        ({"unit": "g", "base_unit": "ml"}, None, "INCOMPATIBLE_UNIT"),
    ],
)
def test_unconfirmed_or_incompatible_units_stay_unavailable(line, mapping, code):
    source = SourceLine.model_validate(
        {
            "quantity": "1",
            "unit": "piece",
            "base_unit": "piece",
            "unit_price": "1.00",
            "currency": "MYR",
        }
        | line
    )
    result = normalize_line(source, PackMapping.model_validate(mapping) if mapping else None)
    assert result.failure_codes == (code,)
    assert result.quantity is None


def test_volume_and_mass_conversions_do_not_establish_equivalence():
    result = normalize_line(
        SourceLine(quantity="1.5", unit="l", base_unit="ml", unit_price="12.00", currency="MYR"),
        None,
    )
    assert result.quantity == D("1500")
    assert result.unit_price == D("0.012")
    assert not result.spec_confirmed
