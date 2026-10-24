from uuid import uuid4

import pytest
from pydantic import ValidationError

from collective.agents.liaison import evidence, review_flags
from collective.agents.schemas import QuoteReading


def test_confidence_cannot_hide_unknown_delivery_or_tax():
    reading = QuoteReading(currency="MYR", confidence_label="high", conditions_complete=True)
    flags = review_flags(reading)
    assert "delivery_fee:missing" in flags and "tax_rate:missing" in flags
    assert reading.delivery_fee is None and reading.tax_rate is None
    with pytest.raises(ValidationError):
        QuoteReading(delivery_fee=0.0)


def test_evidence_must_match_saved_text_and_invalid_regions_fall_back():
    document = {"source_id": str(uuid4()), "text": "RM0.30", "pages": []}
    reading = QuoteReading(
        evidence=[
            {
                "field_path": "tiers.0.unit_price",
                "region_quality": "text",
                "text_span": [0, 6],
                "extracted_value": "RM0.30",
            }
        ]
    )
    anchors, invalid = evidence(reading, document)
    assert not invalid and anchors[0]["text_span"] == [0, 6]
    assert anchors[0]["source_id"] == document["source_id"]
    anchors, invalid = evidence(reading, document | {"text": "changed"})
    assert invalid and anchors[0]["field_path"] == "quote"
    page = {"source_id": str(uuid4()), "text": None, "pages": [{"page": 1}]}
    anchors, invalid = evidence(
        QuoteReading(
            evidence=[
                {
                    "field_path": "delivery_fee",
                    "region_quality": "approximate",
                    "page": 2,
                    "bbox_normalized": [10, 10, 100, 100],
                }
            ]
        ),
        page,
    )
    assert invalid and anchors[0]["region_quality"] == "full_page" and anchors[0]["page"] == 1
