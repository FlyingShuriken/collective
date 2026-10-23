from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from collective.domain.costs import (
    CostBreakdown,
    DeliveryContext,
    FeeRule,
    Quote,
    applicable_fee_rule,
    calculate_cost,
    calculate_payable,
)
from collective.domain.money import round_money
from collective.domain.types import (
    BaseUnit,
    Money,
    NeedSnapshot,
    NonNegative,
    Rate,
    Revision,
    Snapshot,
)


class HistoricalBasis(Snapshot):
    id: UUID
    business_id: UUID
    revision: Revision
    confirmed: bool
    spec_id: UUID
    spec_revision: Revision
    base_unit: BaseUnit
    currency: str = "MYR"
    unit_price: NonNegative | None
    delivery_fee: Money | None
    fee_basis: Literal["per_order", "unknown"]
    delivery_mode: Literal["per_business", "collection_point"]
    zone: str
    credit_days: int = Field(strict=True, ge=0)
    tax_rate: Rate | None
    discount: Money
    surcharge: Money


def historical_cost(need: NeedSnapshot, basis: HistoricalBasis | None) -> Decimal | None:
    if basis is None or not basis.confirmed or basis.currency != "MYR":
        return None
    if (
        (
            basis.business_id,
            basis.spec_id,
            basis.spec_revision,
            basis.base_unit,
            basis.delivery_mode,
            basis.zone,
        )
        != (
            need.business_id,
            need.spec_id,
            need.spec_revision,
            need.base_unit,
            need.delivery_mode,
            need.zone,
        )
        or basis.credit_days < need.min_credit_days
        or basis.fee_basis != "per_order"
        or basis.unit_price is None
        or basis.delivery_fee is None
        or basis.tax_rate is None
    ):
        return None
    goods = round_money(need.quantity * basis.unit_price)
    if basis.discount > goods:
        return None
    total, _ = calculate_payable(
        goods, basis.delivery_fee, basis.discount, basis.surcharge, basis.tax_rate
    )
    return total if total > 0 else None


def comparable_quote(need: NeedSnapshot, quote: Quote) -> bool:
    return (
        (need.business_id, need.spec_id, need.spec_revision, need.base_unit, need.delivery_mode)
        == (
            quote.business_id,
            quote.spec_id,
            quote.spec_revision,
            quote.base_unit,
            quote.delivery_mode,
        )
        and quote.credit_days >= need.min_credit_days
        and (
            need.zone == quote.zone
            if need.delivery_mode == "per_business"
            else need.delivery_location is not None
            and need.delivery_location == quote.delivery_location
        )
        and max(need.arrival_window.start, quote.delivery_window.start)
        < min(need.arrival_window.end, quote.delivery_window.end)
    )


def solo_cost(
    need: NeedSnapshot,
    quotes: tuple[Quote, ...],
    as_of: datetime,
    fee_rules: tuple[FeeRule, ...],
) -> CostBreakdown | None:
    costs = []
    for quote in quotes:
        if not comparable_quote(need, quote):
            continue
        context = DeliveryContext(
            aggregate_quantity=need.quantity,
            member_count=1,
            as_of=as_of,
            collection_location=need.delivery_location,
            collection_shares=(quote.delivery_fee,) if quote.delivery_fee is not None else (),
            own_collection_share=quote.delivery_fee,
        )
        cost = calculate_cost(need.quantity, quote, context, applicable_fee_rule(quote, fee_rules))
        if cost.decision_cost is not None:
            costs.append(cost)
    return min(costs, key=lambda c: (c.decision_cost, str(c.quote_id)), default=None)
