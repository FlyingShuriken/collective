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
    pool_opt_in: bool
    reserved_pool_id: UUID | None
    order_window: TimeWindow
    arrival_window: TimeWindow
    approval_id: UUID | None = None
    approval_state: Literal["valid", "void", "expired", "withdrawn", "released"] | None = None


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


class PoolSummary(Snapshot):
    pool_id: UUID
    need_id: UUID
    need_revision: Revision
    evaluation_revision: Revision
    evaluated_at: Instant
    own_quantity: Positive
    base_unit: BaseUnit
    deadline: Instant
    member_count: int = Field(strict=True, ge=0)
    eligibility: Literal["eligible", "ineligible", "unavailable"]
    failure_codes: tuple[str, ...]
    approval_state: Literal["not_approved", "valid", "void", "expired", "withdrawn", "released"]
    release_state: Literal["waiting_for_approvals", "ready", "released", "ineligible"]
    approval_id: UUID | None = None
    approval_reason: str | None = None
    member_state: Literal[
        "offered", "approved", "awaiting_reapproval", "withdrawn", "expired", "released"
    ] = "offered"

    @model_validator(mode="after")
    def validate_pieces(self) -> Self:
        if self.base_unit == "piece":
            whole_pieces(self.own_quantity)
        return self


class HiddenPoolOffer(PoolSummary):
    price_visibility: Literal["hidden"] = "hidden"


class PoolOffer(PoolSummary):
    price_visibility: Literal["visible"] = "visible"
    supplier_id: UUID
    supplier_name: str
    quote_id: UUID
    quote_revision: Revision
    delivery_window: TimeWindow
    agreed_delivered_cost: Money
    expected_delivered_cost: Money
    decision_delivered_cost: Money
    historical_comparable_cost: Money
    current_solo_comparable_cost: Money
    historical_saving: SignedMoney
    pooling_saving: SignedMoney
    credit_days: int = Field(strict=True, ge=0)
    terms: BoundTerms | None = None
    conditions: str | None = None


class UnavailablePoolOffer(PoolSummary):
    price_visibility: Literal["unavailable"] = "unavailable"


class ApproveOrder(Snapshot):
    need_id: UUID
    pool_id: UUID | None
    cap: Money
    expires_at: Instant
    expected_evaluation_revision: Revision
    quote_id: UUID | None = None
    expected_quote_revision: Revision | None = None
    reviewed_decision_cost: Money | None = None

    @model_validator(mode="after")
    def solo_review(self) -> Self:
        solo_fields = (self.quote_id, self.expected_quote_revision, self.reviewed_decision_cost)
        if self.pool_id is None and any(value is None for value in solo_fields):
            raise ValueError("Solo approval requires the reviewed quote and decision cost")
        if self.pool_id is not None and any(value is not None for value in solo_fields):
            raise ValueError("A pool approval uses its authoritative member evaluation")
        return self


class SoloExecution(Mutation):
    approval_id: UUID


class ApprovalCommandResult(Snapshot):
    status: Literal["approved", "withdrawn", "refresh_required", "ineligible", "recalculated"]
    need: NeedView
    offer: PoolOffer | UnavailablePoolOffer | None = None
    approval_id: UUID | None = None
    failure_codes: tuple[str, ...] = ()


class HiddenPurchaseOrder(Snapshot):
    id: UUID
    business_id: UUID
    need_id: UUID
    approval_id: UUID
    quantity: Positive
    base_unit: BaseUnit
    issued_at: Instant
    implementation: Literal["simulated"] = "simulated"
    price_visibility: Literal["hidden"] = "hidden"


class VisiblePurchaseOrder(HiddenPurchaseOrder):
    price_visibility: Literal["visible"] = "visible"
    supplier_id: UUID
    supplier_name: str
    amount: Money
    decision_amount: Money
    terms: BoundTerms
    cost: CostBreakdown


class ReleaseResult(Snapshot):
    state: Literal["released", "ready", "awaiting_approvals", "ineligible"]
    need: NeedView
    offer: PoolOffer | UnavailablePoolOffer | None = None
    order: VisiblePurchaseOrder | None = None
    failure_codes: tuple[str, ...] = ()


class ActivityEvent(Snapshot):
    id: UUID
    event_type: str
    entity_id: UUID
    entity_revision: Revision | None
    actor_kind: Literal["owner", "staff", "system"]
    occurred_at: Instant
    summary: str


class ActivityPage(Snapshot):
    items: tuple[ActivityEvent, ...]
    next_cursor: UUID | None


class DiscoverPool(Mutation):
    need_id: UUID


class JobStatus(Snapshot):
    id: UUID
    kind: str
    status: Literal["waiting_review", "queued", "running", "succeeded", "failed"]
    input_revision: Revision | None
    attempts: int = Field(strict=True, ge=0)
    created_at: Instant
    next_attempt_at: Instant
    completed_at: Instant | None
    error_code: str | None
    lease_expired: bool


class JobPage(Snapshot):
    items: tuple[JobStatus, ...]
    next_cursor: UUID | None


class SourcePage(Snapshot):
    page: int = Field(strict=True, ge=1, le=10)
    width: int = Field(strict=True, ge=64)
    height: int = Field(strict=True, ge=64)
    transformations: dict


class SourceSummary(Snapshot):
    id: UUID
    kind: Literal["image", "pdf", "text", "supplier_reply"]
    status: Literal["uploading", "available", "failed"]
    mime_type: str | None
    size_bytes: int | None
    page_count: int | None
    created_at: Instant
    preparation_status: Literal["pending_review", "ready"] | None


class HiddenSource(SourceSummary):
    price_visibility: Literal["hidden"] = "hidden"


class VisibleSource(SourceSummary):
    price_visibility: Literal["visible"] = "visible"
    sha256: str | None
    pages: tuple[SourcePage, ...]


class SourcePageList(Snapshot):
    items: tuple[VisibleSource | HiddenSource, ...]
    next_cursor: UUID | None


class SourceResult(Snapshot):
    source: VisibleSource | HiddenSource
    job: JobStatus | None


class MaskRegion(Snapshot):
    page: int = Field(strict=True, ge=1, le=10)
    bbox: tuple[int, int, int, int]

    @model_validator(mode="after")
    def valid_box(self) -> Self:
        left, top, right, bottom = self.bbox
        if not 0 <= left < right <= 1000 or not 0 <= top < bottom <= 1000:
            raise ValueError("Invalid normalized mask bounds")
        return self


class PrepareSource(Snapshot):
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    regions: tuple[MaskRegion, ...] = Field(default=(), max_length=100)
    sensitive_text: tuple[str, ...] = Field(default=(), max_length=20)
    reviewed: Literal[True]

    @model_validator(mode="after")
    def sensitive_values(self) -> Self:
        if any(not value.strip() or len(value) > 200 for value in self.sensitive_text):
            raise ValueError("Sensitive values must be nonempty and at most 200 characters")
        return self


class DiscoveryResult(Snapshot):
    state: Literal["found", "unavailable"]
    need: NeedView
    offer: PoolOffer | UnavailablePoolOffer | None = None
    failure_codes: tuple[str, ...] = ()


class DraftSummary(Snapshot):
    id: UUID
    kind: Literal["rfq", "invoice_query"]
    status: Literal["draft", "sent_simulated"]
    revision: Revision
    created_at: Instant


class HiddenDraft(DraftSummary):
    price_visibility: Literal["hidden"] = "hidden"


class DraftView(DraftSummary):
    price_visibility: Literal["visible"] = "visible"
    text_content: str
    recipient: str
    approved_hash: str
    sent_at: Instant | None


class DraftPage(Snapshot):
    items: tuple[DraftView | HiddenDraft, ...]
    next_cursor: UUID | None = None


class EditDraft(Snapshot):
    expected_revision: Revision
    text_content: str = Field(min_length=1, max_length=16000)
    recipient: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def readable(self) -> Self:
        if any(
            not value.strip() or "\x00" in value for value in (self.text_content, self.recipient)
        ):
            raise ValueError("Use readable message text and recipient")
        return self


class SendDraft(Snapshot):
    expected_revision: Revision
    approved_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    recipient: str = Field(min_length=1, max_length=300)


class MessageReceipt(Snapshot):
    id: UUID
    receipt_id: UUID
    draft_id: UUID
    draft_revision: Revision
    created_at: Instant
    simulated: Literal[True] = True


PUBLIC_MODELS = (HealthResponse, ErrorResponse, Evidence, Identity, Mutation, PoolOffer)
