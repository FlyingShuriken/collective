from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from collective.domain.approvals import BoundTerms
from collective.domain.costs import CostBreakdown
from collective.domain.quantities import QuantityOption
from collective.domain.replenishment import ReplenishmentResult
from collective.domain.types import (
    BaseUnit,
    Instant,
    Money,
    NonNegative,
    Positive,
    Rate,
    Revision,
    SignedMoney,
    Snapshot,
    TimeWindow,
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


class NetworkAccess(Snapshot):
    network_id: UUID
    name: str
    role: Literal["network_admin"] = "network_admin"


class Identity(Snapshot):
    subject: UUID
    memberships: tuple[Membership, ...]
    networks: tuple[NetworkAccess, ...] = ()


class HubIdentity(Identity):
    implementation: Literal["mock"] = "mock"


class HubService(Snapshot):
    name: str
    description: str
    implementation: Literal["mock"] = "mock"


class ApprovalSummary(Snapshot):
    id: UUID
    business_id: UUID
    need_id: UUID
    pool_id: UUID | None
    state: Literal["valid", "void", "expired", "withdrawn", "released"]
    expires_at: Instant
    revision: Revision


class HiddenApproval(ApprovalSummary):
    price_visibility: Literal["hidden"] = "hidden"


class VisibleApproval(ApprovalSummary):
    price_visibility: Literal["visible"] = "visible"
    cap: Money
    terms: BoundTerms


class Mutation(Snapshot):
    expected_revision: Revision


class EvaluateNeed(Snapshot):
    mapping_id: UUID
    mapping_revision: Revision
    quantity: Positive
    usage_per_day: Positive | None = None
    lead_days: NonNegative
    buffer_days: NonNegative
    max_stock: Positive | None = None
    shelf_life_days: Positive | None = None
    saving_threshold: Rate
    min_credit_days: int = Field(strict=True, ge=0)
    delivery_mode: Literal["per_business", "collection_point"]
    delivery_location: str | None = None
    collection_share: Money | None = None
    order_window: TimeWindow
    arrival_window: TimeWindow


class SelectNeed(Mutation):
    quantity: Positive


class NeedView(Snapshot):
    id: UUID
    business_id: UUID
    revision: Revision
    spec_id: UUID
    spec_revision: Revision
    name: str
    quantity: Positive
    base_unit: BaseUnit
    state: Literal["suggested", "selected", "reserved", "released", "stale"]
    mapping_confirmed: bool
    order_window: TimeWindow
    arrival_window: TimeWindow


class HiddenNeedData(Snapshot):
    need: NeedView
    price_visibility: Literal["hidden"] = "hidden"


class NeedPage(Snapshot):
    items: tuple[NeedView, ...]
    next_cursor: UUID | None


class NeedOptions(Snapshot):
    need: NeedView
    price_visibility: Literal["visible"] = "visible"
    evaluated_at: Instant
    replenishment: ReplenishmentResult
    options: tuple[QuantityOption, ...]
    stated_quantity_valid: bool
    failure_codes: tuple[str, ...]


class OfferComparison(Snapshot):
    quote_id: UUID
    quote_revision: Revision
    supplier_id: UUID
    supplier_name: str
    credit_days: int | None
    conditions: str | None
    valid_until: Instant | None
    cost: CostBreakdown
    missing_fields: tuple[str, ...]
    failure_codes: tuple[str, ...]


class NeedComparisons(Snapshot):
    need: NeedView
    price_visibility: Literal["visible"] = "visible"
    evaluated_at: Instant
    historical_reference: UUID | None
    historical_revision: Revision | None
    historical_comparable_cost: Money | None
    best_solo_cost: Money | None
    offers: tuple[OfferComparison, ...]
    failure_codes: tuple[str, ...]


class HiddenLedgerEntry(Snapshot):
    id: UUID
    kind: Literal["purchase", "stock", "quote", "po"]
    revision: Revision
    recorded_at: Instant
    quantity: NonNegative | None
    price_visibility: Literal["hidden"] = "hidden"


class VisibleLedgerEntry(HiddenLedgerEntry):
    price_visibility: Literal["visible"] = "visible"
    total: Money | None
    source_id: UUID | None
    supplier_id: UUID | None


class LedgerPage(Snapshot):
    items: tuple[VisibleLedgerEntry | HiddenLedgerEntry, ...]
    next_cursor: UUID | None


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
