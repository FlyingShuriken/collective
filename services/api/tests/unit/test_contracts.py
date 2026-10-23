from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from collective.api.dto import Evidence, PoolOffer
from collective.main import create_app


def offer_data():
    return {
        "pool_id": "2e6761e1-d712-5f72-a77b-bc2be09cd910",
        "need_id": "df450e66-ec35-5ba1-af8b-5416f9aac437",
        "need_revision": 2,
        "evaluation_revision": 3,
        "evaluated_at": "2026-10-23T01:00:00Z",
        "own_quantity": "1000",
        "base_unit": "piece",
        "deadline": "2026-10-24T01:00:00Z",
        "price_visibility": "visible",
        "supplier_id": "c9f28526-1831-5120-9172-1e86dadb2e5c",
        "supplier_name": "Supplier B",
        "quote_id": "892d73eb-3f54-583e-b0a8-dfd9305bacbb",
        "quote_revision": 1,
        "delivery_window": {"start": "2026-10-27T01:00:00Z", "end": "2026-10-28T01:00:00Z"},
        "agreed_delivered_cost": "255.00",
        "expected_delivered_cost": "255.00",
        "decision_delivered_cost": "255.00",
        "historical_comparable_cost": "315.00",
        "current_solo_comparable_cost": "315.00",
        "historical_saving": "60.00",
        "pooling_saving": "60.00",
        "credit_days": 30,
        "member_count": 3,
        "eligibility": "eligible",
        "failure_codes": [],
        "approval_state": "not_approved",
        "approval_id": None,
        "approval_reason": None,
        "member_state": "offered",
        "terms": None,
        "conditions": None,
        "release_state": "waiting_for_approvals",
    }


def test_offer_round_trip_is_decimal_string_and_utc():
    offer = PoolOffer.model_validate(offer_data())
    assert isinstance(offer.pool_id, UUID)
    assert offer.decision_delivered_cost == Decimal("255.00")
    assert offer.evaluated_at == datetime(2026, 10, 23, 1, tzinfo=UTC)
    assert offer.model_dump(mode="json") == offer_data()
    assert PoolOffer.model_validate_json(offer.model_dump_json()) == offer


@pytest.mark.parametrize(
    "patch",
    [
        {"decision_delivered_cost": 255.0},
        {"decision_delivered_cost": "NaN"},
        {"decision_delivered_cost": "-1"},
        {"decision_delivered_cost": "0.001"},
        {"decision_delivered_cost": "10000000000000000.00"},
        {"own_quantity": "0"},
        {"own_quantity": "1.5"},
        {"evaluation_revision": 0},
        {"evaluated_at": "2026-10-23T01:00:00"},
        {"another_members_cap": "285.00"},
    ],
)
def test_offer_rejects_untrusted_values_and_private_fields(patch):
    with pytest.raises(ValidationError):
        PoolOffer.model_validate(offer_data() | patch)


def test_evidence_requires_real_bounds_or_explicit_fallback():
    evidence = {
        "field_path": "lines.0.unit_price",
        "source_id": str(UUID(int=1)),
        "page": 1,
        "bbox_normalized": [610, 320, 770, 360],
        "region_quality": "approximate",
        "confidence_label": "review",
        "extracted_value": "0.30",
    }
    assert Evidence.model_validate(evidence).page == 1
    for bounds in ([700, 320, 610, 360], [-1, 0, 100, 100], [0, 0, 1001, 200]):
        with pytest.raises(ValidationError):
            Evidence.model_validate(evidence | {"bbox_normalized": bounds})
    with pytest.raises(ValidationError):
        Evidence.model_validate(evidence | {"bbox_normalized": None})
    assert Evidence.model_validate(
        evidence | {"bbox_normalized": None, "region_quality": "full_page"}
    )


def test_openapi_exposes_only_public_contracts_and_string_amounts():
    schemas = create_app().openapi()["components"]["schemas"]
    assert schemas["PoolOffer"]["properties"]["decision_delivered_cost"]["type"] == "string"
    assert "another_members_cap" not in schemas["PoolOffer"]["properties"]
    assert schemas["ErrorResponse"]["properties"]["code"]["type"] == "string"
