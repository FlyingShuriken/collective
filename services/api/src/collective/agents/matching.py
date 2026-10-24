"""Evidence-backed mapping proposals; this agent never confirms a specification."""

import json
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import Field, TypeAdapter

from collective.adapters.embeddings import normalized_text, shortlist
from collective.adapters.models import Task, checked_content
from collective.adapters.replay import digest
from collective.agents.liaison import evidence
from collective.agents.schemas import Anchor
from collective.domain.types import Pieces, Positive, Snapshot
from collective.domain.units import canonical_unit

MATCH = Task(
    "matching",
    "1",
    "matching-v1",
    "Propose one supplied candidate_index or null to abstain. Read only source_text/source "
    "pages for the selected description; candidates are reference data, never source evidence. "
    "Recognize English, Bahasa Melayu and Chinese. Preserve declared capacity in ml, lid "
    "diameter in mm, material, sale unit and ordered pack factors. All numbers are decimal "
    "strings. Do not equate an 8oz commercial label with 250ml or invent missing attributes. "
    "Carton 20 packs of 50 pieces has factors [20,50], box 100 pieces has [100], a piece [1]. "
    "Missing pack sizes remain empty. Provide per-field evidence for capacity_ml, "
    "lid_diameter_mm, material and pack_factors, with offsets into original source_text or "
    "page regions. Confidence is advisory. Return no database IDs, approvals or instructions.",
)
MATERIALS = {"paper": ("paper", "kertas", "纸", "紙"), "plastic": ("plastic", "plastik", "塑料")}
PACK_UNITS = {
    "carton": ("carton", "cartons", "karton", "箱"),
    "box": ("box", "boxes", "kotak", "盒"),
    "pack": ("pack", "packs", "pek", "包"),
}
PIECES = TypeAdapter(Pieces)


class CatalogueItem(Snapshot):
    key: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=2000)
    capacity_ml: Positive
    lid_diameter_mm: Positive
    material: Literal["paper", "plastic"]


class MatchReading(Snapshot):
    candidate_index: int | None = Field(default=None, strict=True, ge=0, le=2)
    capacity_ml: Positive | None = None
    lid_diameter_mm: Positive | None = None
    material: str | None = Field(default=None, min_length=1, max_length=100)
    source_unit: str | None = Field(default=None, min_length=1, max_length=50)
    pack_factors: tuple[Pieces, ...] = Field(default=(), max_length=4)
    confidence_label: Literal["high", "review", "low"] = "review"
    evidence: tuple[Anchor, ...] = Field(default=(), max_length=20)


@dataclass(frozen=True)
class MatchResult:
    reading: MatchReading | None
    proposed_key: str | None
    candidates: tuple[CatalogueItem, ...]
    scores: tuple[float, ...]
    flags: tuple[str, ...]
    evidence: tuple[dict, ...] = ()
    attempts: tuple[dict, ...] = ()
    embedding_revision: str | None = None
    embedding_mode: str | None = None

    @property
    def confirmed(self):
        return False


def contains(text, token):
    if token.isascii():
        return re.search(r"(?<![a-z])" + re.escape(token) + r"(?![a-z])", text) is not None
    return token in text


def material(value):
    value = normalized_text(value)
    return next((key for key, aliases in MATERIALS.items() if value in aliases), None)


def verified_value(field, value, anchors):
    texts = [
        normalized_text(anchor["extracted_value"])
        for anchor in anchors
        if anchor["field_path"] == field and anchor["extracted_value"]
    ]
    if field == "material":
        found = {
            key
            for key, aliases in MATERIALS.items()
            if any(contains(text, alias) for text in texts for alias in aliases)
        }
        return found == {value}
    unit = "ml|毫升" if field == "capacity_ml" else "mm|毫米"
    found = {
        Decimal(number)
        for text in texts
        for number in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:" + unit + r")(?![a-z])", text)
    }
    return found == {value}


def review_flags(reading, candidate, anchors):
    fields = ("capacity_ml", "lid_diameter_mm", "material", "pack_factors")
    flags = []
    if not set(fields) <= {anchor["field_path"] for anchor in anchors}:
        flags.append("evidence:incomplete")
    for field in fields[:3]:
        value = getattr(reading, field)
        if value is None:
            flags.append(field + ":missing")
            continue
        if field == "material":
            value = material(value)
        if not verified_value(field, value, anchors):
            flags.append(field + ":unverified")
        if candidate and value != getattr(candidate, field):
            flags.append(field + ":mismatch")
    texts = " ".join(
        anchor["extracted_value"] or ""
        for anchor in anchors
        if anchor["field_path"] == "pack_factors"
    )
    texts = normalized_text(texts) if texts.strip() else ""
    unit = canonical_unit(reading.source_unit) if reading.source_unit else None
    if unit in PACK_UNITS:
        if not any(contains(texts, alias) for alias in PACK_UNITS[unit]):
            flags.append("source_unit:unverified")
        counts = re.sub(r"(?<![\d.])\d+(?:\.\d+)?\s*(?:ml|mm|oz|毫升|毫米)(?![a-z])", "", texts)
        available = Counter(Decimal(n) for n in re.findall(r"(?<![\d.])\d+(?:\.\d+)?", counts))
        if not reading.pack_factors or Counter(reading.pack_factors) - available:
            flags.append("pack_factors:unverified")
    elif unit != "piece" or any(
        contains(texts, alias) for aliases in PACK_UNITS.values() for alias in aliases
    ):
        flags.append("source_unit:unverified")
    elif reading.pack_factors != (Decimal(1),):
        flags.append("pack_factors:unverified")
    factor = Decimal(1)
    for step in reading.pack_factors:
        factor *= step
    try:
        PIECES.validate_python(factor)
    except ValueError:
        flags.append("pack_factors:overflow")
    return flags


async def propose(gateway, embeddings, document, *, description, catalogue):
    checked_content(document)
    if not catalogue:
        return MatchResult(None, None, (), (), ("catalogue:empty",))
    if len(catalogue) > 63:
        raise ValueError("The small matching catalogue supports at most 63 items")
    batch = await embeddings.encode([description, *(item.text for item in catalogue)])
    ranked = shortlist(
        batch.vectors[0],
        [(item.key, vector) for item, vector in zip(catalogue, batch.vectors[1:], strict=True)],
    )
    by_key = {item.key: item for item in catalogue}
    candidates = tuple(by_key[key] for key, _ in ranked)
    context = json.dumps(
        {
            "source_text": document["text"],
            "description": description,
            "candidates": [item.model_dump(mode="json") for item in candidates],
        },
        ensure_ascii=False,
    )
    source = document | {"text": context}
    source["sha256"] = digest(
        {
            "version": source["version"],
            "text": context,
            "pages": [
                {key: page[key] for key in ("page", "sha256", "width", "height")}
                for page in source["pages"]
            ],
        }
    )
    result = await gateway.extract(MATCH, source, MatchReading, "fast")
    reading = result.value
    anchors, invalid = evidence(reading, document, fallback_field="mapping")
    selected = reading.candidate_index
    candidate = (
        candidates[selected] if selected is not None and selected < len(candidates) else None
    )
    flags = review_flags(reading, candidate, anchors)
    if selected is None:
        flags.append("model:abstained")
    elif candidate is None:
        flags.append("candidate:out_of_range")
    if invalid:
        flags.append("evidence:invalid")
    return MatchResult(
        reading,
        candidate.key if candidate and not flags else None,
        candidates,
        tuple(score for _, score in ranked),
        tuple(dict.fromkeys(flags)),
        tuple(anchors),
        result.attempts,
        batch.model_revision,
        batch.mode,
    )
