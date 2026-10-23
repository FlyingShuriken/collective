from datetime import datetime
from decimal import Decimal
from itertools import combinations
from uuid import UUID

from collective.domain.baselines import (
    HistoricalBasis,
    comparable_quote,
    historical_cost,
    solo_cost,
)
from collective.domain.costs import (
    CostBreakdown,
    DeliveryContext,
    FeeRule,
    Quote,
    applicable_fee_rule,
    calculate_cost,
)
from collective.domain.types import Money, NonNegative, SignedMoney, Snapshot, TimeWindow
from collective.domain.types import NeedSnapshot as NeedSnapshot


class MemberSnapshot(Snapshot):
    need: NeedSnapshot
    quote: Quote
    solo_quotes: tuple[Quote, ...]
    history: HistoricalBasis | None
    fee_rules: tuple[FeeRule, ...] = ()


class MemberEvaluation(Snapshot):
    need: NeedSnapshot
    quote: Quote
    cost: CostBreakdown
    historical_comparable_cost: Money | None
    current_solo_comparable_cost: Money | None
    historical_saving: SignedMoney | None
    pooling_saving: SignedMoney | None
    delivery_window: TimeWindow | None
    failure_codes: tuple[str, ...]

    @property
    def need_id(self):
        return self.need.id

    @property
    def business_id(self):
        return self.need.business_id

    @property
    def decision_cost(self):
        return self.cost.decision_cost

    @property
    def eligible(self):
        return not self.failure_codes


class PoolEvaluation(Snapshot):
    evaluated_at: datetime
    aggregate_quantity: NonNegative
    members: tuple[MemberEvaluation, ...]
    failure_codes: tuple[str, ...]

    @property
    def eligible(self):
        return (
            len(self.members) >= 2
            and not self.failure_codes
            and all(m.eligible for m in self.members)
        )


def shared_terms(quote: Quote):
    return (
        quote.supplier_id,
        quote.spec_id,
        quote.spec_revision,
        quote.base_unit,
        quote.offer_family,
        quote.tier_kind,
        quote.tiers,
        quote.aggregate_moq,
        quote.delivery_mode,
        quote.delivery_location,
        quote.free_delivery_threshold,
        quote.waive_split_drop_fees,
        quote.discount,
        quote.surcharge,
        quote.tax_rate,
        quote.conditions,
    )


def evaluate_pool(
    members: list[MemberSnapshot],
    as_of: datetime,
    pool_id: UUID | None = None,
) -> PoolEvaluation:
    if not members:
        return PoolEvaluation(
            evaluated_at=as_of,
            aggregate_quantity=Decimal(0),
            members=(),
            failure_codes=("NO_MEMBERS",),
        )
    aggregate = sum((m.need.quantity for m in members), Decimal(0))
    group_failures = []
    if len({m.need.business_id for m in members}) != len(members):
        group_failures.append("DUPLICATE_BUSINESS")
    if len({shared_terms(m.quote) for m in members}) != 1:
        group_failures.append("INCOMPATIBLE_OFFERS")
    if members[0].quote.delivery_mode == "per_business" and len({m.need.zone for m in members}) > 1:
        group_failures.append("NO_COMMON_ZONE")
    order_start = max(m.need.order_window.start for m in members)
    order_end = min(m.need.order_window.end for m in members)
    arrival_start = max(
        max(m.need.arrival_window.start, m.quote.delivery_window.start) for m in members
    )
    arrival_end = min(min(m.need.arrival_window.end, m.quote.delivery_window.end) for m in members)
    window = None
    if order_start >= order_end or arrival_start >= arrival_end:
        group_failures.append("NO_WINDOW_OVERLAP")
    else:
        window = TimeWindow(start=arrival_start, end=arrival_end)
    evaluations = []
    for member in members:
        need, quote = member.need, member.quote
        failures = list(group_failures)
        if not need.mapping_confirmed:
            failures.append("UNCONFIRMED_SPEC")
        if not need.pool_opt_in:
            failures.append("POOLING_NOT_OPTED_IN")
        if need.state != "selected" and not (
            need.state == "reserved" and pool_id is not None and need.reserved_pool_id == pool_id
        ):
            failures.append("NEED_COMMITTED")
        if (need.spec_id, need.spec_revision, need.base_unit) != (
            quote.spec_id,
            quote.spec_revision,
            quote.base_unit,
        ):
            failures.append("SPEC_MISMATCH")
        if not comparable_quote(need, quote):
            failures.append("INCOMPARABLE_QUOTE")
        if not quote.pooled_buyers_permitted:
            failures.append("POOLING_NOT_PERMITTED")
        if as_of >= order_end:
            failures.append("ORDER_WINDOW_EXPIRED")
        if as_of < order_start:
            failures.append("ORDER_WINDOW_NOT_OPEN")
        if need.quantity % need.pack_multiple:
            failures.append("INVALID_PACK_MULTIPLE")
        context = DeliveryContext(
            aggregate_quantity=aggregate,
            member_count=len(members),
            as_of=as_of,
            member_quantities=tuple(m.need.quantity for m in members),
            collection_location=need.delivery_location,
            collection_shares=tuple(
                m.need.collection_share for m in members if m.need.collection_share is not None
            ),
            own_collection_share=need.collection_share,
        )
        cost = calculate_cost(
            need.quantity, quote, context, applicable_fee_rule(quote, member.fee_rules)
        )
        failures.extend(cost.failure_codes)
        history = historical_cost(need, member.history)
        solo = solo_cost(need, member.solo_quotes, as_of, member.fee_rules)
        solo_total = solo.decision_cost if solo else None
        history_saving = (
            history - cost.decision_cost
            if history is not None and cost.decision_cost is not None
            else None
        )
        pooling_saving = (
            solo_total - cost.decision_cost
            if solo_total is not None and cost.decision_cost is not None
            else None
        )
        if history is None or solo_total is None:
            failures.append("NO_COMPARABLE_BASELINE")
        if history_saving is not None and history_saving < history * need.saving_threshold:
            failures.append("SAVING_BELOW_THRESHOLD")
        if pooling_saving is not None and pooling_saving <= 0:
            failures.append("NO_SOLO_IMPROVEMENT")
        evaluations.append(
            MemberEvaluation(
                need=need,
                quote=quote,
                cost=cost,
                historical_comparable_cost=history,
                current_solo_comparable_cost=solo_total,
                historical_saving=history_saving,
                pooling_saving=pooling_saving,
                delivery_window=window,
                failure_codes=tuple(dict.fromkeys(failures)),
            )
        )
    return PoolEvaluation(
        evaluated_at=as_of,
        aggregate_quantity=aggregate,
        members=tuple(evaluations),
        failure_codes=tuple(group_failures),
    )


def evaluate_solo(member: MemberSnapshot, as_of: datetime) -> MemberEvaluation:
    need, quote = member.need, member.quote
    failures = []
    if not need.mapping_confirmed:
        failures.append("UNCONFIRMED_SPEC")
    if need.state != "selected" or need.reserved_pool_id is not None:
        failures.append("NEED_COMMITTED")
    if not comparable_quote(need, quote):
        failures.append("INCOMPARABLE_QUOTE")
    if not need.order_window.start <= as_of < need.order_window.end:
        failures.append(
            "ORDER_WINDOW_EXPIRED" if as_of >= need.order_window.end else "ORDER_WINDOW_NOT_OPEN"
        )
    if need.quantity % need.pack_multiple:
        failures.append("INVALID_PACK_MULTIPLE")
    start = max(need.arrival_window.start, quote.delivery_window.start)
    end = min(need.arrival_window.end, quote.delivery_window.end)
    window = TimeWindow(start=start, end=end) if start < end else None
    if window is None:
        failures.append("NO_WINDOW_OVERLAP")
    cost = calculate_cost(
        need.quantity,
        quote,
        DeliveryContext(
            aggregate_quantity=need.quantity,
            member_count=1,
            as_of=as_of,
            collection_location=need.delivery_location,
            collection_shares=(quote.delivery_fee,) if quote.delivery_fee is not None else (),
            own_collection_share=quote.delivery_fee,
        ),
        applicable_fee_rule(quote, member.fee_rules),
    )
    history = historical_cost(need, member.history)
    solo = solo_cost(need, member.solo_quotes, as_of, member.fee_rules)
    total = solo.decision_cost if solo else None
    return MemberEvaluation(
        need=need,
        quote=quote,
        cost=cost,
        historical_comparable_cost=history,
        current_solo_comparable_cost=total,
        historical_saving=history - cost.decision_cost
        if history is not None and cost.decision_cost is not None
        else None,
        pooling_saving=total - cost.decision_cost
        if total is not None and cost.decision_cost is not None
        else None,
        delivery_window=window,
        failure_codes=tuple(dict.fromkeys((*failures, *cost.failure_codes))),
    )


def discover_pool(
    candidates: list[MemberSnapshot],
    initiating_need_id: UUID,
    as_of: datetime,
) -> PoolEvaluation | None:
    ordered = sorted(candidates, key=lambda member: str(member.need.id))
    feasible = []
    # shortcut: exhaustive subsets suit the three-café demo; replace before a larger network pilot.
    for size in range(2, len(ordered) + 1):
        for subset in combinations(ordered, size):
            if not any(m.need.id == initiating_need_id for m in subset):
                continue
            result = evaluate_pool(list(subset), as_of)
            if result.eligible:
                feasible.append(result)
    return min(
        feasible,
        key=lambda result: (
            -len(result.members),
            -sum(m.pooling_saving for m in result.members),
            tuple(str(m.need_id) for m in result.members),
        ),
        default=None,
    )
