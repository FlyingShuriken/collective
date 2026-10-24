from dataclasses import dataclass
from typing import Literal

from collective.adapters.models import ModelFailure, Task
from collective.agents.schemas import IntakeReading
from collective.api.errors import DomainError
from collective.domain.intake_checks import IntakeChecks, check_reading

INTAKE = Task(
    "intake",
    "1",
    "intake-v1",
    "Read the supplied prepared invoice, receipt or stock update into a proposed record. "
    "Recognize English, Bahasa Melayu and Chinese. Values are decimal strings. RM denotes MYR. "
    "Preserve product descriptions, supplier codes, original quantities, units, unit prices "
    "and printed line totals. Do not calculate or repair any amount. "
    "Missing delivery, tax, discount or surcharge values are null, never invented zeroes. "
    "Additional fees contain only charges not already represented by the named header fields. "
    "Keep the literal date text; an ambiguous day/month or missing year needs an issue. "
    "Do not invent a precise stock observation time from a date alone. A reported daily usage "
    "is a claim to review, never an accepted replenishment rule. Supplier replies remain "
    "supplier_reply and unreadable inputs remain unreadable. Do not invent a purchase. "
    "Give per-field confidence labels and literal text spans or approximate page regions; "
    "use a full-page fallback when a field cannot be localized. Return no database IDs.",
)


@dataclass(frozen=True)
class IntakeResult:
    reading: IntakeReading
    checks: IntakeChecks
    attempts: tuple[dict, ...]
    selected_tier: Literal["fast", "strong"]


async def extract(gateway, document, *, record=None):
    attempts = []
    first = None
    for tier in ("fast", "strong"):
        try:
            result = await gateway.extract(INTAKE, document, IntakeReading, tier)
        except DomainError as exc:
            failed = getattr(exc, "attempts", ())
            attempts.extend(failed)
            if record is not None:
                await record(failed, None)
            if tier == "fast" and exc.code in {"MODEL_SCHEMA_INVALID", "MODEL_INCOMPLETE"}:
                continue
            if tier == "strong" and first is not None and isinstance(exc, ModelFailure):
                checks = check_reading(first)
                return IntakeResult(
                    first,
                    checks.model_copy(
                        update={"flags": checks.flags + ("stronger_reading:failed",)}
                    ),
                    tuple(attempts),
                    "fast",
                )
            exc.attempts = tuple(attempts)
            raise
        attempts.extend(result.attempts)
        if record is not None:
            await record(result.attempts, result.value.model_dump(mode="json"))
        checked = check_reading(result.value)
        if result.value.kind == "supplier_reply":
            return IntakeResult(result.value, checked, tuple(attempts), tier)
        uncertain = (
            result.value.kind == "unreadable"
            or result.value.confidence_label != "high"
            or any(issue.reason == "unreadable" for issue in result.value.issues)
        ) or any(anchor.confidence_label != "high" for anchor in result.value.evidence)
        mismatched = any(
            flag.endswith(":mismatch") or flag.endswith(":overflow") for flag in checked.flags
        )
        if tier == "fast" and (uncertain or mismatched):
            first = result.value
            continue
        return IntakeResult(result.value, checked, tuple(attempts), tier)
