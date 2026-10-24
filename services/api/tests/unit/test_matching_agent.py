import json
from uuid import UUID

import pytest

from collective.adapters.embeddings import ROOT, EmbeddingGateway
from collective.adapters.models import Reading, prepared_text
from collective.agents.matching import CatalogueItem, MatchReading, propose, review_flags
from collective.settings import Settings


def catalogue():
    rows = json.loads((ROOT / "cases.json").read_text())["catalogue"]
    return tuple(CatalogueItem.model_validate(row) for row in rows)


def document(text):
    return prepared_text(text) | {"source_id": UUID(int=1)}


class Reader:
    def __init__(self, **changes):
        self.changes = changes
        self.calls = []

    async def extract(self, task, source, schema, tier):
        self.calls.append(source)
        context = json.loads(source["text"])
        text = context["source_text"]
        values = {
            "candidate_index": 0,
            "capacity_ml": "250",
            "lid_diameter_mm": "80",
            "material": "paper",
            "source_unit": "piece",
            "pack_factors": ["1"],
            "confidence_label": "high",
            "evidence": [
                {
                    "field_path": field,
                    "region_quality": "text",
                    "text_span": [0, len(text)],
                    "extracted_value": text,
                }
                for field in ("capacity_ml", "lid_diameter_mm", "material", "pack_factors")
            ],
        } | self.changes
        return Reading(schema.model_validate(values), ({"mode": "synthetic_test"},))


async def match(text, *, items=None, **changes):
    reader = Reader(**changes)
    result = await propose(
        reader,
        EmbeddingGateway(Settings(_env_file=None)),
        document(text),
        description=text,
        catalogue=items if items is not None else catalogue(),
    )
    return result, reader


@pytest.mark.parametrize(
    "text,material",
    [
        ("250ml paper cup with 80mm lid", "paper"),
        ("Cawan kertas 250ml, diameter penutup 80mm", "kertas"),
        ("250毫升纸杯，80毫米杯盖", "纸"),
    ],
)
async def test_multilingual_matching_proposes_only_with_source_evidence(text, material):
    result, reader = await match(text, material=material)
    assert result.proposed_key == "paper-250-80"
    assert result.flags == () and result.confirmed is False
    assert len(result.candidates) == 3 and result.attempts
    assert len(reader.calls) == 1


@pytest.mark.parametrize(
    "text,changes,flag",
    [
        ("8oz paper cups with 80mm lids", {}, "capacity_ml:unverified"),
        ("250ml paper cup with 90mm lid", {"lid_diameter_mm": "90"}, "lid_diameter_mm:mismatch"),
        ("250ml plastic cup with 80mm lid", {"material": "plastic"}, "material:mismatch"),
        ("250ml paper cup with 80mm lid", {"capacity_ml": None}, "capacity_ml:missing"),
        ("250ml paper cup with 80mm lid", {"evidence": []}, "evidence:incomplete"),
        ("250ml paper cup with 80mm lid", {"candidate_index": 2}, "candidate:out_of_range"),
        ("250ml paper cup with 80mm lid", {"material": "plastic"}, "material:unverified"),
    ],
)
async def test_high_confidence_never_overrides_hard_attribute_or_evidence_failure(
    text, changes, flag
):
    result, _ = await match(text, items=catalogue()[:1], **changes)
    assert result.proposed_key is None and flag in result.flags
    assert result.confirmed is False


@pytest.mark.parametrize(
    "text,unit,factors",
    [
        ("250ml paper cups 80mm lid, carton of 20 packs of 50 pieces", "carton", ["20", "50"]),
        ("250ml paper cups 80mm lid, box of 100 pieces", "box", ["100"]),
    ],
)
async def test_pack_sizes_remain_explicit_and_separate_from_product_identity(text, unit, factors):
    result, _ = await match(text, source_unit=unit, pack_factors=factors)
    assert result.proposed_key == "paper-250-80"
    assert result.reading.model_dump(mode="json")["pack_factors"] == factors
    assert result.confirmed is False


async def test_pack_cannot_be_invented_or_inferred_from_similarity():
    text = "250ml paper cups 80mm lid, box of 100 pieces"
    result, _ = await match(text, source_unit="box", pack_factors=["1000"])
    assert result.proposed_key is None and "pack_factors:unverified" in result.flags
    result, _ = await match(text, source_unit="carton", pack_factors=["100"])
    assert result.proposed_key is None and "source_unit:unverified" in result.flags
    result, _ = await match(text, source_unit="box", pack_factors=["250"])
    assert result.proposed_key is None and "pack_factors:unverified" in result.flags


async def test_model_abstention_and_empty_catalogue_do_not_fabricate_a_match():
    result, _ = await match("250ml paper cup with 80mm lid", candidate_index=None)
    assert result.proposed_key is None and "model:abstained" in result.flags
    result, reader = await match("250ml paper cup with 80mm lid", items=())
    assert result.proposed_key is None and result.candidates == () and not reader.calls


async def test_one_item_catalogue_and_model_failure_preserve_real_candidate_count():
    result, reader = await match("250ml paper cup with 80mm lid", items=catalogue()[:1])
    assert len(result.candidates) == 1
    assert len(json.loads(reader.calls[0]["text"])["candidates"]) == 1
    assert result.proposed_key == "paper-250-80"
    assert result.confirmed is False


async def test_evidence_copied_from_a_candidate_is_not_source_evidence():
    result, _ = await match(
        "250ml paper cup with 80mm lid",
        evidence=[
            {
                "field_path": "capacity_ml",
                "region_quality": "text",
                "text_span": [0, 5],
                "extracted_value": "350ml",
            }
        ],
    )
    assert result.proposed_key is None
    assert "evidence:invalid" in result.flags


@pytest.mark.parametrize(
    "text,flag",
    [
        ("250ml or 350ml paper cup with 80mm lid", "capacity_ml:unverified"),
        ("250ml paper or plastic cup with 80mm lid", "material:unverified"),
        ("250ml paper cup with 80mm or 90mm lid", "lid_diameter_mm:unverified"),
    ],
)
def test_conflicting_source_attributes_need_narrower_evidence_or_abstention(text, flag):
    reading = MatchReading(
        capacity_ml="250",
        lid_diameter_mm="80",
        material="paper",
        source_unit="piece",
        pack_factors=["1"],
    )
    anchors = [
        {"field_path": field, "extracted_value": text}
        for field in ("capacity_ml", "lid_diameter_mm", "material", "pack_factors")
    ]
    assert flag in review_flags(reading, catalogue()[0], anchors)


def test_invalid_numeric_or_extra_authority_fields_cannot_enter_reading():
    for values in (
        {"capacity_ml": 250.0},
        {"pack_factors": ["0.5"]},
        {"capacity_ml": "999999999999999999999"},
        {"candidate_index": True},
        {"confirmed": True},
    ):
        with pytest.raises(ValueError):
            MatchReading.model_validate(values)
