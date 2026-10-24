from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    model_validator,
)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def decimal_input(value: object) -> object:
    if not isinstance(value, str | Decimal):
        raise ValueError("Decimal values must be strings; binary floats are not accepted")
    return value


DecimalValue = Annotated[
    Decimal,
    BeforeValidator(decimal_input),
    Field(max_digits=18, decimal_places=6, allow_inf_nan=False),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+(\.\d{1,6})?$"}),
]
NonNegative = Annotated[DecimalValue, Field(ge=0)]
Positive = Annotated[DecimalValue, Field(gt=0)]
# Money has its own precision; nesting the six-place type retains its whole-digit limit.
SignedMoney = Annotated[
    Decimal,
    BeforeValidator(decimal_input),
    Field(max_digits=18, decimal_places=2, allow_inf_nan=False),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+(\.\d{1,2})?$"}),
]
Money = Annotated[
    SignedMoney,
    Field(ge=0),
    WithJsonSchema({"type": "string", "pattern": r"^\d+(\.\d{1,2})?$"}),
]
Rate = Annotated[NonNegative, Field(le=1)]
Revision = Annotated[int, Field(strict=True, ge=1)]
Instant = AwareDatetime
BaseUnit = Literal["piece", "ml", "g"]
MissingReason = Literal["missing", "ambiguous", "unreadable", "unsupported"]


def whole_pieces(value: Decimal) -> Decimal:
    if value != value.to_integral_value():
        raise ValueError("Pieces must be whole")
    return value


Pieces = Annotated[Positive, AfterValidator(whole_pieces)]


class TimeWindow(Snapshot):
    start: Instant
    end: Instant

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("Window end must follow its start")
        return self


class NeedSnapshot(Snapshot):
    id: UUID
    business_id: UUID
    revision: Revision
    spec_id: UUID
    spec_revision: Revision
    quantity: Positive
    base_unit: BaseUnit
    pack_multiple: Positive
    mapping_confirmed: bool
    zone: str
    pool_opt_in: bool
    state: Literal["suggested", "selected", "reserved", "released", "stale"]
    reserved_pool_id: UUID | None = None
    saving_threshold: Rate
    min_credit_days: int = Field(strict=True, ge=0)
    delivery_mode: Literal["per_business", "collection_point"]
    delivery_location: str | None = None
    collection_share: Money | None = None
    order_window: TimeWindow
    arrival_window: TimeWindow
