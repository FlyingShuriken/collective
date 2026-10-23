from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from collective.domain.types import (
    BaseUnit,
    Instant,
    Money,
    Positive,
    Revision,
    SignedMoney,
    Snapshot,
    whole_pieces,
)


class HealthResponse(Snapshot):
    status: Literal["ok"] = "ok"


class ErrorResponse(Snapshot):
    code: str
    message: str
    refresh_reference: UUID | None = None


class Evidence(Snapshot):
    field_path: str = Field(min_length=1, max_length=200)
    source_id: UUID
    page: int | None = Field(default=None, ge=1, le=10)
    text_span: tuple[int, int] | None = None
    bbox_normalized: tuple[int, int, int, int] | None = None
    region_quality: Literal["exact", "approximate", "full_page", "text"]
    confidence_label: Literal["high", "review", "low"]
    extracted_value: str | None

    @model_validator(mode="after")
    def evidence_location(self) -> Self:
        if self.region_quality == "text":
            if self.text_span is None or not 0 <= self.text_span[0] < self.text_span[1]:
                raise ValueError("Text evidence requires an ordered character span")
            if self.page is not None or self.bbox_normalized is not None:
                raise ValueError("Text evidence cannot claim page coordinates")
        else:
            if self.page is None or self.text_span is not None:
                raise ValueError("Page evidence requires a page and no text span")
            if self.region_quality in {"exact", "approximate"} and self.bbox_normalized is None:
                raise ValueError("Located evidence requires bounds")
        if self.bbox_normalized is not None:
            left, top, right, bottom = self.bbox_normalized
            if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
                raise ValueError("Invalid normalised evidence bounds")
        return self


class Membership(Snapshot):
    business_id: UUID
    name: str
    role: Literal["owner", "staff"]
    locale: Literal["en", "ms", "zh"]
    can_view_prices: bool


class Identity(Snapshot):
    subject: UUID
    memberships: tuple[Membership, ...]
    networks: tuple["NetworkAccess", ...] = ()


class NetworkAccess(Snapshot):
    network_id: UUID
    name: str
    role: Literal["network_admin"] = "network_admin"


class Mutation(Snapshot):
    expected_revision: Revision


class PoolOffer(Snapshot):
    pool_id: UUID
    evaluation_revision: Revision
    evaluated_at: Instant
    own_quantity: Positive
    base_unit: BaseUnit
    agreed_delivered_cost: Money
    expected_delivered_cost: Money
    decision_delivered_cost: Money
    historical_comparable_cost: Money
    current_solo_comparable_cost: Money
    historical_saving: SignedMoney
    pooling_saving: SignedMoney
    credit_days: int = Field(strict=True, ge=0)
    member_count: int = Field(strict=True, ge=1)
    eligibility: Literal["eligible", "ineligible", "unavailable"]
    failure_codes: tuple[str, ...]
    approval_state: Literal["not_approved", "valid", "void", "expired", "withdrawn", "released"]
    release_state: Literal["waiting_for_approvals", "ready", "released", "ineligible"]

    @model_validator(mode="after")
    def validate_pieces(self) -> Self:
        if self.base_unit == "piece":
            whole_pieces(self.own_quantity)
        return self


PUBLIC_MODELS = (HealthResponse, ErrorResponse, Evidence, Identity, Mutation, PoolOffer)
