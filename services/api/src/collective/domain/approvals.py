import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import TypeAdapter, model_validator

from collective.domain.pools import MemberEvaluation, PoolEvaluation
from collective.domain.types import (
    BaseUnit,
    Instant,
    Money,
    Positive,
    Revision,
    Snapshot,
    TimeWindow,
)

ApprovalState = Literal["valid", "void", "expired", "withdrawn", "released"]


class ApprovalError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class BoundTerms(Snapshot):
    need_id: UUID
    need_revision: Revision
    supplier_id: UUID
    spec_id: UUID
    spec_revision: Revision
    quantity: Positive
    base_unit: BaseUnit
    delivery_mode: Literal["per_business", "collection_point"]
    delivery_location: str | None
    delivery_window: TimeWindow
    credit_days: int
    conditions_fingerprint: str


class ApprovalBinding(Snapshot):
    id: UUID
    business_id: UUID
    owner_id: UUID
    pool_id: UUID | None
    terms: BoundTerms
    cap: Money
    expires_at: Instant
    approved_at: Instant
    evaluation_revision: Revision
    quote_id: UUID
    quote_revision: Revision
    state: ApprovalState = "valid"

    @model_validator(mode="after")
    def future_expiry(self) -> Self:
        if self.expires_at <= self.approved_at:
            raise ValueError("Approval expiry must follow its creation")
        return self


class ApprovalEvaluation(Snapshot):
    approval_id: UUID
    state: ApprovalState
    reason: str


class ReleaseEvaluation(Snapshot):
    state: Literal["ready", "awaiting_approvals", "ineligible", "released"]
    approvals: tuple[ApprovalEvaluation, ...]


def bound_terms(evaluation: MemberEvaluation) -> BoundTerms | None:
    if evaluation.delivery_window is None:
        return None
    need, quote = evaluation.need, evaluation.quote
    conditions = quote.model_dump(
        mode="json",
        exclude={
            "id",
            "business_id",
            "revision",
            "supplier_id",
            "spec_id",
            "spec_revision",
            "base_unit",
            "credit_days",
            "delivery_window",
            "valid_from",
            "valid_until",
        },
    )
    return BoundTerms(
        need_id=need.id,
        need_revision=need.revision,
        supplier_id=quote.supplier_id,
        spec_id=need.spec_id,
        spec_revision=need.spec_revision,
        quantity=need.quantity,
        base_unit=need.base_unit,
        delivery_mode=quote.delivery_mode,
        delivery_location=quote.delivery_location,
        delivery_window=evaluation.delivery_window,
        credit_days=quote.credit_days,
        conditions_fingerprint=hashlib.sha256(
            json.dumps(conditions, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def bind_approval(
    evaluation: MemberEvaluation,
    *,
    approval_id: UUID,
    owner_id: UUID,
    cap: str | Decimal,
    expires_at: datetime,
    as_of: datetime,
    pool_id: UUID | None,
    evaluation_revision: int = 1,
) -> ApprovalBinding:
    terms = bound_terms(evaluation)
    if not evaluation.eligible or evaluation.decision_cost is None or terms is None:
        raise ApprovalError("INELIGIBLE_OFFER")
    cap_value = TypeAdapter(Money).validate_python(cap)
    if cap_value < evaluation.decision_cost:
        raise ApprovalError("CAP_EXCEEDED")
    if (
        not as_of
        < expires_at
        <= min(evaluation.quote.valid_until, evaluation.need.order_window.end)
    ):
        raise ApprovalError("INVALID_EXPIRY")
    return ApprovalBinding(
        id=approval_id,
        business_id=evaluation.business_id,
        owner_id=owner_id,
        pool_id=pool_id,
        terms=terms,
        cap=cap_value,
        expires_at=expires_at,
        approved_at=as_of,
        evaluation_revision=evaluation_revision,
        quote_id=evaluation.quote.id,
        quote_revision=evaluation.quote.revision,
    )


def evaluate_approval(
    binding: ApprovalBinding,
    evaluation: MemberEvaluation,
    as_of: datetime,
) -> ApprovalEvaluation:
    def result(state, reason):
        return ApprovalEvaluation(approval_id=binding.id, state=state, reason=reason)

    if binding.state != "valid":
        return result(binding.state, "APPROVAL_NOT_VALID")
    if as_of >= binding.expires_at:
        return result("expired", "APPROVAL_EXPIRED")
    if binding.business_id != evaluation.business_id or binding.terms != bound_terms(evaluation):
        return result("void", "BOUND_TERMS_CHANGED")
    if evaluation.decision_cost is not None and evaluation.decision_cost > binding.cap:
        return result("void", "CAP_EXCEEDED")
    if not evaluation.eligible:
        return result("void", "INELIGIBLE_OFFER")
    return result("valid", "STANDS")


def evaluate_release(
    pool: PoolEvaluation,
    approvals: list[ApprovalBinding],
    as_of: datetime,
    *,
    released: bool = False,
) -> ReleaseEvaluation:
    if released:
        return ReleaseEvaluation(state="released", approvals=())
    by_need = {approval.terms.need_id: approval for approval in approvals}
    results = tuple(
        evaluate_approval(by_need[member.need_id], member, as_of)
        for member in pool.members
        if member.need_id in by_need
    )
    if not pool.eligible:
        state = "ineligible"
    elif len(results) == len(pool.members) and all(result.state == "valid" for result in results):
        state = "ready"
    else:
        state = "awaiting_approvals"
    return ReleaseEvaluation(state=state, approvals=results)


def assert_pre_release(state: str) -> None:
    if state == "released":
        raise ApprovalError("ALREADY_RELEASED")
