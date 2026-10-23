from decimal import Decimal, localcontext
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from collective.domain.money import round_money
from collective.domain.types import (
    BaseUnit,
    Instant,
    Money,
    NonNegative,
    Positive,
    Rate,
    Revision,
    Snapshot,
    TimeWindow,
    whole_pieces,
)


class Tier(Snapshot):
    min_quantity: Positive
    unit_price: NonNegative


class Quote(Snapshot):
    id: UUID
    business_id: UUID
    supplier_id: UUID
    spec_id: UUID
    spec_revision: Revision
    revision: Revision
    currency: str
    base_unit: BaseUnit
    pack_multiple: Positive
    zone: str
    confirmed: bool
    offer_family: UUID
    pooled_buyers_permitted: bool | None
    tiers: tuple[Tier, ...] = Field(min_length=1)
    tier_kind: Literal["all_unit", "incremental"]
    per_business_moq: Positive
    aggregate_moq: Positive
    delivery_mode: Literal["per_business", "collection_point"]
    delivery_location: str | None = None
    delivery_fee: Money | None
    free_delivery_threshold: Money | None = None
    waive_split_drop_fees: bool | None = None
    credit_days: int = Field(strict=True, ge=0)
    tax_rate: Rate | None
    discount: Money
    surcharge: Money
    conditions: str = Field(min_length=1)
    conditions_complete: bool = True
    valid_from: Instant
    valid_until: Instant
    delivery_window: TimeWindow

    @model_validator(mode="after")
    def valid_schedule(self) -> Self:
        bounds = [tier.min_quantity for tier in self.tiers]
        if bounds != sorted(set(bounds)):
            raise ValueError("Tier lower bounds must be unique and increasing")
        if self.valid_from >= self.valid_until:
            raise ValueError("Quote validity interval is empty")
        if self.base_unit == "piece":
            whole_pieces(self.pack_multiple)
        return self


class DeliveryContext(Snapshot):
    aggregate_quantity: Positive
    member_count: int = Field(strict=True, ge=1)
    as_of: Instant
    member_quantities: tuple[Positive, ...] = ()
    collection_location: str | None = None
    collection_shares: tuple[Money, ...] = ()
    own_collection_share: Money | None = None


class FeeRule(Snapshot):
    id: UUID
    revision: Revision
    accepted: bool
    sample_count: int = Field(strict=True, ge=0)
    business_id: UUID
    supplier_id: UUID
    zone: str
    delivery_mode: Literal["per_business", "collection_point"]
    amount: Money


class CostBreakdown(Snapshot):
    quote_id: UUID
    quote_revision: Revision
    rule_id: UUID | None = None
    rule_revision: Revision | None = None
    unit_price: NonNegative | None = None
    tier_min_quantity: Positive | None = None
    goods: Money | None = None
    threshold_goods: Money | None = None
    free_delivery_qualified: bool | None = None
    delivery_fee: Money | None = None
    tax: Money | None = None
    agreed_cost: Money | None = None
    expected_cost: Money | None = None
    decision_cost: Money | None = None
    fee_unverified: bool = True
    failure_codes: tuple[str, ...] = ()


def calculate_payable(goods, delivery_fee, discount, surcharge, tax_rate):
    subtotal = goods - discount + delivery_fee + surcharge
    tax = round_money(subtotal * tax_rate)
    return round_money(subtotal + tax), tax


def applicable_fee_rule(quote: Quote, rules: tuple[FeeRule, ...]) -> FeeRule | None:
    matches = [
        r
        for r in rules
        if r.accepted
        and r.sample_count >= 3
        and (r.business_id, r.supplier_id, r.zone, r.delivery_mode)
        == (quote.business_id, quote.supplier_id, quote.zone, quote.delivery_mode)
    ]
    return max(matches, key=lambda rule: (rule.revision, str(rule.id)), default=None)


def calculate_cost(
    quantity: Decimal,
    quote: Quote,
    delivery: DeliveryContext,
    accepted_fee_rule: FeeRule | None,
) -> CostBreakdown:
    reference = {"quote_id": quote.id, "quote_revision": quote.revision}

    def unavailable(code):
        return CostBreakdown(**reference, failure_codes=(code,))

    if not quantity.is_finite() or quantity <= 0 or quantity >= Decimal("1000000000000"):
        return unavailable("INVALID_QUANTITY")
    if not quote.confirmed:
        return unavailable("UNCONFIRMED_QUOTE")
    if quote.currency != "MYR":
        return unavailable("UNSUPPORTED_CURRENCY")
    if delivery.as_of >= quote.valid_until:
        return unavailable("QUOTE_EXPIRED")
    if delivery.as_of < quote.valid_from:
        return unavailable("QUOTE_NOT_ACTIVE")
    if not quote.conditions_complete:
        return unavailable("UNKNOWN_CONDITIONS")
    if quote.delivery_fee is None:
        return unavailable("UNKNOWN_DELIVERY")
    if quote.tax_rate is None:
        return unavailable("UNKNOWN_TAX")
    if quote.tier_kind != "all_unit":
        return unavailable("UNSUPPORTED_TIER_KIND")
    if quantity % quote.pack_multiple or (quote.base_unit == "piece" and quantity % 1):
        return unavailable("INVALID_PACK_MULTIPLE")
    if delivery.aggregate_quantity < quantity:
        return unavailable("INVALID_AGGREGATE_QUANTITY")
    quantities = delivery.member_quantities or (quantity,)
    if len(quantities) != delivery.member_count or sum(quantities) != delivery.aggregate_quantity:
        return unavailable("INVALID_AGGREGATE_QUANTITY")
    if quantity < quote.per_business_moq or delivery.aggregate_quantity < quote.aggregate_moq:
        return unavailable("MOQ_NOT_MET")
    if delivery.member_count > 1 and not quote.pooled_buyers_permitted:
        return unavailable("POOLING_NOT_PERMITTED")
    eligible_tiers = [t for t in quote.tiers if t.min_quantity <= delivery.aggregate_quantity]
    if not eligible_tiers:
        return unavailable("MOQ_NOT_MET")
    tier = eligible_tiers[-1]
    with localcontext() as context:
        context.prec = 40
        goods = round_money(quantity * tier.unit_price)
        threshold_goods = sum(
            (round_money(q * tier.unit_price) - quote.discount for q in quantities), Decimal("0.00")
        )
        if goods < quote.discount or threshold_goods < 0:
            return unavailable("DISCOUNT_EXCEEDS_GOODS")
        qualifies = (
            quote.free_delivery_threshold is not None
            and threshold_goods >= quote.free_delivery_threshold
        )
        fee = quote.delivery_fee
        if quote.delivery_mode == "collection_point":
            shares = delivery.collection_shares
            if (
                not quote.delivery_location
                or delivery.collection_location != quote.delivery_location
                or len(shares) != delivery.member_count
                or sum(shares) != quote.delivery_fee
                or delivery.own_collection_share not in shares
            ):
                return unavailable("UNCONFIRMED_COLLECTION_SHARES")
            fee = delivery.own_collection_share
        if qualifies:
            if quote.waive_split_drop_fees is None and delivery.member_count > 1:
                return unavailable("UNKNOWN_DELIVERY")
            if (
                delivery.member_count == 1
                or quote.delivery_mode == "collection_point"
                or quote.waive_split_drop_fees
            ):
                fee = Decimal("0.00")
        rule = accepted_fee_rule
        comparable_rule = (
            rule is not None
            and rule.accepted
            and rule.sample_count >= 3
            and rule.business_id == quote.business_id
            and rule.supplier_id == quote.supplier_id
            and rule.zone == quote.zone
            and rule.delivery_mode == quote.delivery_mode
        )
        expected_fee = rule.amount if comparable_rule else fee

        def payable(delivery_fee):
            return calculate_payable(
                goods, delivery_fee, quote.discount, quote.surcharge, quote.tax_rate
            )

        agreed, tax = payable(fee)
        expected, _ = payable(expected_fee)
        return CostBreakdown(
            **reference,
            unit_price=tier.unit_price,
            tier_min_quantity=tier.min_quantity,
            goods=goods,
            threshold_goods=threshold_goods,
            free_delivery_qualified=qualifies,
            delivery_fee=fee,
            tax=tax,
            agreed_cost=agreed,
            expected_cost=expected,
            decision_cost=max(agreed, expected),
            fee_unverified=not comparable_rule,
            rule_id=rule.id if comparable_rule else None,
            rule_revision=rule.revision if comparable_rule else None,
        )
