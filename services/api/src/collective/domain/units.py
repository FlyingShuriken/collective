from decimal import Decimal, localcontext

from pydantic import Field, ValidationError

from collective.domain.types import BaseUnit, NonNegative, Positive, Snapshot, whole_pieces


class SourceLine(Snapshot):
    quantity: Positive
    unit: str = Field(min_length=1)
    base_unit: BaseUnit = "piece"
    unit_price: NonNegative
    currency: str


class PackMapping(Snapshot):
    base_unit: BaseUnit
    factors: tuple[Positive, ...] = Field(min_length=1)
    confirmed: bool
    source_unit: str | None = None


class NormalizedLine(Snapshot):
    original_quantity: Positive
    original_unit: str
    original_unit_price: NonNegative
    quantity: Positive | None = None
    unit_price: NonNegative | None = None
    base_unit: BaseUnit
    spec_confirmed: bool = False
    failure_codes: tuple[str, ...] = ()


def canonical_unit(unit: str) -> str:
    value = unit.strip().lower()
    return {
        "pcs": "piece",
        "pc": "piece",
        "pieces": "piece",
        "keping": "piece",
        "unit": "piece",
        "units": "piece",
        "个": "piece",
        "件": "piece",
        "boxes": "box",
        "kotak": "box",
        "盒": "box",
        "箱": "carton",
        "cartons": "carton",
        "karton": "carton",
        "packs": "pack",
        "pek": "pack",
        "litre": "l",
        "liter": "l",
        "litres": "l",
        "liters": "l",
        "升": "l",
        "millilitre": "ml",
        "milliliter": "ml",
        "毫升": "ml",
        "克": "g",
        "公斤": "kg",
    }.get(value, value)


def normalize_line(source: SourceLine, mapping: PackMapping | None) -> NormalizedLine:
    original = dict(
        original_quantity=source.quantity,
        original_unit=source.unit,
        original_unit_price=source.unit_price,
        base_unit=source.base_unit,
    )

    def unavailable(code):
        return NormalizedLine(**original, failure_codes=(code,))

    if source.currency != "MYR":
        return unavailable("UNSUPPORTED_CURRENCY")
    if source.unit in {"oz", "ounce"}:
        return unavailable("AMBIGUOUS_UNIT")
    with localcontext() as context:
        context.prec = 40
        if source.unit in {"carton", "box", "pack"}:
            if mapping is None or not mapping.confirmed:
                return unavailable("UNCONFIRMED_PACK")
            if mapping.base_unit != source.base_unit:
                return unavailable("INCOMPATIBLE_UNIT")
            if mapping.source_unit is not None and mapping.source_unit != source.unit:
                return unavailable("INCOMPATIBLE_PACK_UNIT")
            factor = Decimal(1)
            for step in mapping.factors:
                factor *= step
        else:
            conversion = {
                "piece": ("piece", Decimal(1)),
                "ml": ("ml", Decimal(1)),
                "l": ("ml", Decimal(1000)),
                "g": ("g", Decimal(1)),
                "kg": ("g", Decimal(1000)),
            }.get(source.unit)
            if conversion is None or conversion[0] != source.base_unit:
                return unavailable("INCOMPATIBLE_UNIT")
            factor = conversion[1]
        quantity = source.quantity * factor
        if source.base_unit == "piece":
            try:
                whole_pieces(quantity)
            except ValueError:
                return unavailable("FRACTIONAL_PIECES")
        try:
            return NormalizedLine(
                **original,
                quantity=quantity,
                unit_price=source.unit_price / factor,
            )
        except ValidationError:
            return unavailable("UNSUPPORTED_PRECISION")
