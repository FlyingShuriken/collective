from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import model_validator

from collective.domain.clock import days_between, days_delta
from collective.domain.types import Instant, NonNegative, Positive, Revision, Snapshot


class StockMovement(Snapshot):
    id: UUID
    quantity: Positive
    at: Instant
    kind: Literal["received", "incoming", "waste"]
    confirmed: bool


class StockSnapshot(Snapshot):
    quantity: NonNegative
    observed_at: Instant
    movements: tuple[StockMovement, ...] = ()

    @model_validator(mode="after")
    def unique_movements(self) -> Self:
        if len({m.id for m in self.movements}) != len(self.movements):
            raise ValueError("A receipt/order cannot be counted twice")
        return self


class UsageInput(Snapshot):
    stated_daily_rate: Positive | None = None
    rule_daily_rate: Positive | None = None
    rule_accepted: bool = False
    rule_revision: Revision | None = None

    @model_validator(mode="after")
    def accepted_rule_has_provenance(self) -> Self:
        if self.rule_accepted and (self.rule_daily_rate is None or self.rule_revision is None):
            raise ValueError("Accepted usage requires a value and rule revision")
        return self


class LeadTime(Snapshot):
    days: NonNegative
    buffer_days: NonNegative


class ReplenishmentResult(Snapshot):
    daily_rate: Positive | None = None
    usage_rule_revision: Revision | None = None
    stock_at_now: NonNegative | None = None
    stock_at_arrival: NonNegative | None = None
    run_out_at: Instant | None = None
    reorder_at: Instant | None = None
    reorder_due: bool | None = None
    failure_codes: tuple[str, ...] = ()


def estimate_replenishment(
    stock: StockSnapshot | None,
    usage: UsageInput,
    lead_time: LeadTime,
    as_of: datetime,
) -> ReplenishmentResult:
    if as_of.tzinfo is None:
        raise ValueError("Evaluation time must be timezone-aware")
    rate = usage.rule_daily_rate if usage.rule_accepted else usage.stated_daily_rate
    if stock is None:
        return ReplenishmentResult(failure_codes=("MISSING_STOCK",))
    if rate is None:
        return ReplenishmentResult(failure_codes=("MISSING_USAGE",))
    if stock.observed_at > as_of:
        return ReplenishmentResult(failure_codes=("FUTURE_STOCK_OBSERVATION",))
    movements = sorted((m for m in stock.movements if m.confirmed), key=lambda m: (m.at, m.id))
    if any(m.kind == "received" and m.at > as_of for m in movements):
        return ReplenishmentResult(failure_codes=("FUTURE_RECEIPT",))

    def project(quantity, start, end, receipts):
        previous = start
        for receipt in receipts:
            if start < receipt.at <= end:
                quantity = max(Decimal(0), quantity - rate * days_between(previous, receipt.at))
                quantity = max(
                    Decimal(0),
                    quantity + (-receipt.quantity if receipt.kind == "waste" else receipt.quantity),
                )
                previous = receipt.at
        return max(Decimal(0), quantity - rate * days_between(previous, end))

    current = project(
        stock.quantity,
        stock.observed_at,
        as_of,
        [m for m in movements if m.kind in {"received", "waste"}],
    )
    future = [m for m in movements if m.kind == "incoming" and m.at > as_of]
    arrival = as_of + days_delta(lead_time.days)
    at_arrival = project(current, as_of, arrival, future)
    run_out = as_of + days_delta(current / rate)
    for incoming in future:
        if incoming.at > run_out:
            break
        run_out += days_delta(incoming.quantity / rate)
    reorder = run_out - days_delta(lead_time.days + lead_time.buffer_days)
    return ReplenishmentResult(
        daily_rate=rate,
        usage_rule_revision=usage.rule_revision if usage.rule_accepted else None,
        stock_at_now=current.quantize(Decimal("0.000001")),
        stock_at_arrival=at_arrival.quantize(Decimal("0.000001")),
        run_out_at=run_out,
        reorder_at=reorder,
        reorder_due=as_of >= reorder,
    )
