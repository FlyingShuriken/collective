from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

from collective.domain.costs import DeliveryContext, Quote, calculate_cost
from collective.domain.types import (
    Money,
    NonNegative,
    Positive,
    SignedMoney,
    Snapshot,
)


class QuantityNeed(Snapshot):
    quantity: Positive
    pack_multiple: Positive
    selected: bool = False


class QuantityConstraints(Snapshot):
    max_stock: Positive
    stock_at_arrival: NonNegative
    usage_per_day: Positive | None
    perishable: bool
    shelf_life_days: Positive | None = None


class QuantityOption(Snapshot):
    quantity: Positive
    quote_id: UUID
    cost: Money
    credit_days: int
    coverage_days: Positive | None
    extra_cash_now: SignedMoney
    saving_vs_repeat_orders: SignedMoney
    repeat_order_count: int
    assumptions: tuple[str, ...]


def suggest_quantities(
    need: QuantityNeed,
    quotes: list[Quote],
    constraints: QuantityConstraints,
    as_of: datetime,
) -> list[QuantityOption]:
    def pack_round(quantity):
        return (quantity / need.pack_multiple).to_integral_value(
            rounding=ROUND_CEILING
        ) * need.pack_multiple

    base = pack_round(need.quantity)
    if need.selected and base != need.quantity:
        return []

    def best(quantity):
        candidates = []
        for quote in quotes:
            cost = calculate_cost(
                quantity,
                quote,
                DeliveryContext(
                    aggregate_quantity=quantity,
                    member_count=1,
                    as_of=as_of,
                ),
                None,
            )
            if cost.decision_cost is not None:
                candidates.append((cost.decision_cost, str(quote.id), quote))
        return min(candidates, key=lambda item: item[:2]) if candidates else None

    base_offer = best(base)
    if base_offer is None:
        return []
    base_cost = base_offer[0]
    candidates = {base}
    rate = constraints.usage_per_day
    if not need.selected and not constraints.perishable and rate is not None:
        for quote in quotes:
            for tier in quote.tiers:
                candidates.add(pack_round(tier.min_quantity))
                if quote.free_delivery_threshold is not None and tier.unit_price > 0:
                    candidates.add(
                        pack_round(
                            (quote.free_delivery_threshold + quote.discount) / tier.unit_price
                        )
                    )
    options = []
    for quantity in sorted(q for q in candidates if q >= base):
        stock = constraints.stock_at_arrival + quantity
        if stock > constraints.max_stock:
            continue
        if constraints.perishable and (rate is None or constraints.shelf_life_days is None):
            continue
        if constraints.shelf_life_days is not None:
            if rate is None or stock > rate * constraints.shelf_life_days:
                continue
        offer = best(quantity)
        if offer is None:
            continue
        cost, _, quote = offer
        repeat_count = int(quantity // base)
        repeated_cost = base_cost * repeat_count
        remainder = quantity % base
        if remainder:
            remainder_offer = best(remainder)
            if remainder_offer is None:
                continue
            repeated_cost += remainder_offer[0]
            repeat_count += 1
        saving = repeated_cost - cost
        if quantity != base and saving <= 0:
            continue
        options.append(
            QuantityOption(
                quantity=quantity,
                quote_id=quote.id,
                cost=cost,
                credit_days=quote.credit_days,
                coverage_days=(stock / rate).quantize(Decimal("0.000001")) if rate else None,
                extra_cash_now=cost - base_cost,
                saving_vs_repeat_orders=saving,
                repeat_order_count=repeat_count,
                assumptions=("same_confirmed_terms_for_repeat_orders",) if quantity != base else (),
            )
        )
        if len(options) == 3:
            break
    return options
