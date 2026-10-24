"""Deterministic evidence summaries. These calculations never accept or activate a rule."""

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from collective.domain.clock import days_between
from collective.domain.invoices import InvoiceCheckResult
from collective.domain.money import round_money
from collective.domain.types import (
    BaseUnit,
    Instant,
    Money,
    NonNegative,
    Positive,
    Revision,
    Snapshot,
    whole_pieces,
)


class EvidenceReference(Snapshot):
    business_id: UUID
    source_id: UUID
    record_id: UUID
    revision: Revision = 1


class UsageContext(Snapshot):
    spec_id: UUID
    spec_revision: Revision
    base_unit: BaseUnit


class FeeContext(Snapshot):
    supplier_id: UUID
    zone: str = Field(min_length=1)
    delivery_mode: Literal["per_business", "collection_point"]
    delivery_location: str | None = None
    currency: Literal["MYR"] = "MYR"

    @model_validator(mode="after")
    def location_matches_mode(self):
        if (self.delivery_mode == "collection_point") != bool(self.delivery_location):
            raise ValueError("Collection fees require their agreed collection location")
        return self


class AccuracyContext(Snapshot):
    supplier_id: UUID


class StockEvidence(Snapshot):
    reference: EvidenceReference
    context: UsageContext
    quantity: NonNegative
    at: Instant
    kind: Literal["stock", "receipt", "inbound_adjustment", "waste"]
    confirmed: bool

    @field_validator("at")
    @classmethod
    def utc_time(cls, value):
        return aware(value)

    @model_validator(mode="after")
    def whole_piece_stock(self):
        if self.context.base_unit == "piece":
            whole_pieces(self.quantity)
        return self


class FeeEvidence(Snapshot):
    reference: EvidenceReference
    context: FeeContext
    at: Instant
    amount: Money | None
    confirmed: bool

    @field_validator("at")
    @classmethod
    def utc_time(cls, value):
        return aware(value)


class AccuracyEvidence(Snapshot):
    reference: EvidenceReference
    context: AccuracyContext
    purchase_id: UUID
    purchase_revision: Revision
    po_id: UUID | None
    at: Instant
    checked_at: Instant
    confirmed: bool
    result: InvoiceCheckResult

    @field_validator("at", "checked_at")
    @classmethod
    def utc_time(cls, value):
        return aware(value)


class UsageValue(Snapshot):
    kind: Literal["usage"] = "usage"
    daily_rate: Positive
    slowest_daily_rate: NonNegative
    basis: Literal["measured_stock_intervals"] = "measured_stock_intervals"


class FeeValue(Snapshot):
    kind: Literal["observed_fee"] = "observed_fee"
    amount: Money
    minimum: Money
    maximum: Money
    basis: Literal["mean_confirmed_invoice_delivery"] = "mean_confirmed_invoice_delivery"


class AccuracyValue(Snapshot):
    kind: Literal["quote_accuracy"] = "quote_accuracy"
    mismatch_count: int = Field(strict=True, ge=0)
    checked_count: int = Field(strict=True, ge=1)
    basis: Literal["complete_issued_order_checks"] = "complete_issued_order_checks"


class RuleCalculation(Snapshot):
    context: UsageContext | FeeContext | AccuracyContext
    value: UsageValue | FeeValue | AccuracyValue = Field(discriminator="kind")
    sample_count: int = Field(strict=True, ge=1)
    distinct_businesses: int = Field(strict=True, ge=1)
    period_start: Instant
    period_end: Instant
    excluded_count: int = Field(strict=True, ge=0)
    uncertainty: tuple[str, ...]
    evidence: tuple[EvidenceReference, ...]
    evidence_digest: str
    eligible_private: bool
    eligible_network: bool
    version: Literal["learning-v1"] = "learning-v1"


def unique_evidence(records):
    """Duplicate reads cannot increase the sample size; conflicting immutable IDs fail."""
    unique = {}
    for record in records:
        ref = record.reference
        key = (ref.business_id, ref.record_id, ref.revision)
        if key in unique and unique[key] != record:
            raise ValueError("Conflicting values for the same evidence revision")
        unique[key] = record
    return sorted(
        unique.values(),
        key=lambda r: (
            r.at,
            str(r.reference.business_id),
            str(r.reference.record_id),
            r.reference.revision,
        ),
    )


def calculation(context, value, samples, records, dates, excluded, uncertainty):
    if not samples:
        return None
    records = unique_evidence(records)
    businesses = {r.reference.business_id for r in records}
    # Include immutable input values as well as IDs, without making each run a new proposal.
    payload = {
        "version": "learning-v1",
        "context": context.model_dump(mode="json"),
        "value": value.model_dump(mode="json"),
        "records": [r.model_dump(mode="json") for r in records],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    eligible = value.kind != "observed_fee" or samples >= 3
    return RuleCalculation(
        context=context,
        value=value,
        sample_count=samples,
        distinct_businesses=len(businesses),
        period_start=min(dates),
        period_end=max(dates),
        excluded_count=excluded,
        uncertainty=uncertainty,
        evidence=tuple(r.reference for r in records),
        evidence_digest=digest.hexdigest(),
        eligible_private=eligible,
        eligible_network=eligible and len(businesses) >= 5,
    )


def usage_proposal(records, context: UsageContext, as_of: datetime):
    as_of = aware(as_of)
    grouped = defaultdict(list)
    excluded = 0
    for record in unique_evidence(records):
        if record.context != context or not record.confirmed or record.at > as_of:
            excluded += 1
        else:
            grouped[record.reference.business_id].append(record)
    rates, evidence, dates = [], [], []
    for observations in grouped.values():
        counts = {}
        for row in observations:
            if row.kind == "stock":
                if row.at in counts and counts[row.at].quantity != row.quantity:
                    raise ValueError("Conflicting stock counts at the same time")
                counts.setdefault(row.at, row)
        counts = sorted(counts.values(), key=lambda r: r.at)
        for opening, closing in zip(counts, counts[1:], strict=False):
            movements = [
                r for r in observations if r.kind != "stock" and opening.at < r.at <= closing.at
            ]
            with localcontext() as decimal_context:
                decimal_context.prec = 50
                consumed = (
                    opening.quantity
                    - closing.quantity
                    + sum(
                        (-r.quantity if r.kind == "waste" else r.quantity for r in movements),
                        Decimal(0),
                    )
                )
                if consumed < 0:
                    excluded += 1
                    continue
                rate = consumed / days_between(opening.at, closing.at)
                # Round up so six-place storage never understates the fastest observed usage.
                rate = rate.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
            rates.append(rate)
            evidence.extend([opening, closing, *movements])
            dates.extend([opening.at, closing.at])
    if not rates or max(rates) <= 0:
        return None
    return calculation(
        context,
        UsageValue(daily_rate=max(rates), slowest_daily_rate=min(rates)),
        len(rates),
        evidence,
        dates,
        excluded,
        ("UNREPORTED_STOCK_MOVEMENTS_UNKNOWN", "PAST_USAGE_MAY_CHANGE"),
    )


def fee_proposal(records, context: FeeContext, as_of: datetime):
    as_of = aware(as_of)
    rows = unique_evidence(records)
    latest = {}
    for row in sorted(rows, key=lambda r: r.reference.revision):
        if row.confirmed and row.at <= as_of:
            latest[(row.reference.business_id, row.reference.record_id)] = row
    eligible = [r for r in latest.values() if r.context == context and r.amount is not None]
    if not eligible:
        return None
    amounts = [r.amount for r in eligible]
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        mean = round_money(sum(amounts) / len(amounts))
    return calculation(
        context,
        FeeValue(amount=mean, minimum=min(amounts), maximum=max(amounts)),
        len(eligible),
        eligible,
        [r.at for r in eligible],
        len(rows) - len(eligible),
        ("INVOICED_FEE_NOT_PAYMENT_OR_AGREEMENT", "FUTURE_DELIVERY_TERMS_MAY_DIFFER"),
    )


def accuracy_proposal(records, context: AccuracyContext, as_of: datetime):
    as_of = aware(as_of)
    rows = unique_evidence(records)
    # One invoice contributes at most once, including after repeat checks or corrections.
    latest = {}
    excluded = 0
    for row in sorted(
        rows, key=lambda r: (r.purchase_revision, r.checked_at, str(r.reference.record_id))
    ):
        if row.context != context or not row.confirmed or max(row.at, row.checked_at) > as_of:
            excluded += 1
            continue
        key = (row.reference.business_id, row.purchase_id)
        if key in latest:
            excluded += 1
        latest[key] = row
    eligible = [
        r
        for r in latest.values()
        if r.po_id is not None and r.result.complete and r.result.status in {"passed", "flagged"}
    ]
    excluded += len(latest) - len(eligible)
    if not eligible:
        return None
    return calculation(
        context,
        AccuracyValue(
            checked_count=len(eligible),
            mismatch_count=sum(any(d.material for d in r.result.differences) for r in eligible),
        ),
        len(eligible),
        eligible,
        [r.at for r in eligible],
        excluded,
        ("CHECKED_INVOICES_ONLY", "DISCREPANCIES_DO_NOT_ESTABLISH_SUPPLIER_FAULT"),
    )


def aware(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Learning time must be timezone-aware")
    return value.astimezone(UTC)
