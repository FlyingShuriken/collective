from decimal import ROUND_HALF_UP, Decimal, localcontext


def round_money(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Money requires a finite Decimal")
    with localcontext() as context:
        context.prec = 40
        rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(rounded) >= Decimal("10000000000000000"):
        raise ValueError("Money exceeds numeric(18,2)")
    return rounded
