from datetime import date
from typing import Literal, Self

from pydantic import Field, model_validator

from collective.domain.types import (
    BaseUnit,
    Instant,
    MissingReason,
    Money,
    NonNegative,
    Positive,
    Rate,
    Snapshot,
)


class FieldIssue(Snapshot):
    field_path: str = Field(min_length=1, max_length=200)
    reason: MissingReason


class Anchor(Snapshot):
    field_path: str = Field(min_length=1, max_length=200)
    page: int | None = Field(default=None, ge=1, le=10)
    text_span: list[int] | None = Field(default=None, min_length=2, max_length=2)
    bbox_normalized: list[int] | None = Field(default=None, min_length=4, max_length=4)
    region_quality: Literal["approximate", "full_page", "text"]
    extracted_value: str | None = Field(default=None, max_length=1000)


class QuotedTier(Snapshot):
    min_quantity: Positive
    unit_price: NonNegative


class QuoteReading(Snapshot):
    description: str | None = Field(default=None, max_length=2000)
    currency: str | None = Field(default=None, max_length=10)
    base_unit: BaseUnit | None = None
    pack_multiple: Positive | None = None
    tier_kind: Literal["all_unit", "incremental"] | None = None
    tiers: list[QuotedTier] = Field(default_factory=list, max_length=20)
    per_business_moq: Positive | None = None
    aggregate_moq: Positive | None = None
    delivery_mode: Literal["per_business", "collection_point"] | None = None
    delivery_location: str | None = Field(default=None, max_length=500)
    delivery_fee: Money | None = None
    free_delivery_threshold: Money | None = None
    waive_split_drop_fees: bool | None = None
    pooled_buyers_permitted: bool | None = None
    credit_days: int | None = Field(default=None, strict=True, ge=0, le=3650)
    tax_rate: Rate | None = None
    discount: Money | None = None
    surcharge: Money | None = None
    conditions: str | None = Field(default=None, max_length=4000)
    conditions_complete: bool = False
    valid_from: Instant | None = None
    valid_until: Instant | None = None
    arrival_start: Instant | None = None
    arrival_end: Instant | None = None
    validity_text: str | None = Field(default=None, max_length=500)
    issues: list[FieldIssue] = Field(default_factory=list, max_length=100)
    evidence: list[Anchor] = Field(default_factory=list, max_length=100)
    confidence_label: Literal["high", "review", "low"] = "review"

    @model_validator(mode="after")
    def ordered(self) -> Self:
        bounds = [tier.min_quantity for tier in self.tiers]
        if bounds != sorted(set(bounds)):
            raise ValueError("Quote tiers must have unique increasing lower bounds")
        for start, end in (
            (self.valid_from, self.valid_until),
            (self.arrival_start, self.arrival_end),
        ):
            if start and end and start >= end:
                raise ValueError("Quote windows must be ordered")
        return self


class DraftReading(Snapshot):
    text_content: str = Field(min_length=1, max_length=16000)

    @model_validator(mode="after")
    def readable(self) -> Self:
        if not self.text_content.strip() or "\x00" in self.text_content:
            raise ValueError("Use readable draft text")
        return self


class IntakeAnchor(Anchor):
    confidence_label: Literal["high", "review", "low"] = "review"


class InvoiceLineReading(Snapshot):
    description: str | None = Field(default=None, max_length=2000)
    supplier_sku: str | None = Field(default=None, max_length=200)
    quantity: Positive | None = None
    unit: str | None = Field(default=None, max_length=100)
    unit_price: NonNegative | None = None
    line_total: Money | None = None


class AdditionalFee(Snapshot):
    label: str | None = Field(default=None, max_length=300)
    amount: Money | None = None


class IntakeReading(Snapshot):
    kind: Literal["purchase", "stock", "supplier_reply", "unreadable"]
    supplier_name: str | None = Field(default=None, max_length=300)
    document_number: str | None = Field(default=None, max_length=200)
    date_text: str | None = Field(default=None, max_length=200)
    document_date: date | None = None
    currency: str | None = Field(default=None, max_length=10)
    lines: list[InvoiceLineReading] = Field(default_factory=list, max_length=100)
    goods_total: Money | None = None
    delivery_fee: Money | None = None
    tax_amount: Money | None = None
    tax_rate: Rate | None = None
    discount: Money | None = None
    surcharge: Money | None = None
    additional_fees: list[AdditionalFee] = Field(default_factory=list, max_length=30)
    total: Money | None = None
    credit_days: int | None = Field(default=None, strict=True, ge=0, le=3650)
    delivery_mode: Literal["per_business", "collection_point"] | None = None
    stock_description: str | None = Field(default=None, max_length=2000)
    stock_quantity: NonNegative | None = None
    stock_unit: str | None = Field(default=None, max_length=100)
    stock_date_text: str | None = Field(default=None, max_length=200)
    stock_observed_at: Instant | None = None
    reported_usage_per_day: NonNegative | None = None
    issues: list[FieldIssue] = Field(default_factory=list, max_length=100)
    evidence: list[IntakeAnchor] = Field(default_factory=list, max_length=100)
    confidence_label: Literal["high", "review", "low"] = "review"
