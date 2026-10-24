import hashlib
import importlib.util
import io
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from PIL import Image
from pydantic import ValidationError

from collective.adapters.models import ModelFailure, ReaderGateway, Reading, prepared_text
from collective.adapters.replay import ReplayStore, digest
from collective.agents.intake import INTAKE, extract
from collective.agents.schemas import IntakeReading
from collective.api.errors import DomainError
from collective.domain.intake_checks import check_reading
from collective.services.source_rendering import inspect_source
from collective.settings import Settings
from tests.fixtures import ROOT


def invoice(**changes):
    return IntakeReading.model_validate(
        {
            "kind": "purchase",
            "supplier_name": "Supplier B",
            "document_number": "DEMO-B-001",
            "date_text": "2026-10-23",
            "document_date": "2026-10-23",
            "currency": "MYR",
            "lines": [
                {
                    "description": "250ml paper cup",
                    "quantity": "1000",
                    "unit": "piece",
                    "unit_price": "0.275",
                    "line_total": "275.00",
                }
            ],
            "goods_total": "275.00",
            "delivery_fee": "15.00",
            "tax_amount": "0.00",
            "discount": "0.00",
            "surcharge": "0.00",
            "total": "290.00",
            "confidence_label": "high",
            **changes,
        }
    )


def test_code_checks_invoice_arithmetic_without_changing_the_reading():
    reading = invoice(total="291.00")
    result = check_reading(reading)
    assert result.calculated_goods == Decimal("275.00")
    assert result.calculated_total == Decimal("290.00")
    assert "total:mismatch" in result.flags and reading.total == Decimal("291.00")
    assert "total:mismatch" not in check_reading(invoice()).flags
    lines = [
        {
            "description": "cups",
            "quantity": "3",
            "unit": "piece",
            "unit_price": "0.335",
            "line_total": "1.01",
        }
    ]
    assert "lines.0.total:mismatch" not in check_reading(invoice(lines=lines)).flags


def test_missing_fees_and_unsupported_currency_are_not_completed_or_converted():
    reading = invoice(delivery_fee=None, currency="USD")
    checked = check_reading(reading)
    assert "delivery_fee:missing" in checked.flags and "currency:unsupported" in checked.flags
    assert checked.calculated_total is None and reading.delivery_fee is None
    assert reading.currency == "USD" and reading.total == Decimal("290.00")


@pytest.mark.parametrize(
    "literal,ambiguous",
    [
        ("03/04/2026", True),
        ("03/04", True),
        ("23/10/2026", False),
        ("2026-10-23", False),
        ("23 Oktober 2026", False),
    ],
)
def test_ambiguous_date_is_preserved_for_explicit_human_review(literal, ambiguous):
    reading = invoice(date_text=literal)
    assert ("document_date:ambiguous" in check_reading(reading).flags) is ambiguous
    assert reading.date_text == literal


def test_stock_date_without_time_is_not_fabricated_into_a_precise_observation():
    reading = IntakeReading(
        kind="stock",
        stock_description="cawan kertas 250ml",
        stock_quantity="750",
        stock_unit="keping",
        stock_date_text="23 Oktober 2026",
        reported_usage_per_day="125",
        confidence_label="high",
    )
    result = check_reading(reading)
    assert "stock_observed_at:missing" in result.flags
    assert reading.stock_observed_at is None and reading.reported_usage_per_day == Decimal("125")


async def test_one_stronger_reading_preserves_both_readings_and_unresolved_checks():
    calls, saved = [], []

    class Gateway:
        async def extract(self, task, source, schema, tier):
            calls.append((task, tier))
            return Reading(invoice(total="291.00"), ({"id": str(len(calls)), "tier": tier},))

    async def record(attempts, value):
        saved.append((attempts, value))

    result = await extract(Gateway(), prepared_text("SYNTHETIC invoice"), record=record)
    assert [tier for _, tier in calls] == ["fast", "strong"]
    assert len(saved) == 2 and "total:mismatch" in result.checks.flags
    assert result.reading.total == Decimal("291.00") and len(result.attempts) == 2
    assert result.selected_tier == "strong"


async def test_missing_information_alone_does_not_trigger_repeated_readings():
    calls = []

    class Gateway:
        async def extract(self, task, source, schema, tier):
            calls.append(tier)
            return Reading(invoice(delivery_fee=None), ())

    result = await extract(Gateway(), prepared_text("SYNTHETIC invoice"))
    assert calls == ["fast"] and "delivery_fee:missing" in result.checks.flags


async def test_schema_failure_can_escalate_once_but_transport_failure_does_not():
    calls, saved = [], []

    class Gateway:
        async def extract(self, task, source, schema, tier):
            calls.append(tier)
            if tier == "fast":
                raise ModelFailure("MODEL_SCHEMA_INVALID", "Invalid reading.", ({"tier": tier},))
            return Reading(invoice(), ({"tier": tier},))

    async def record(attempts, value):
        saved.append((attempts, value))

    result = await extract(Gateway(), prepared_text("SYNTHETIC invoice"), record=record)
    assert calls == ["fast", "strong"] and len(saved) == 2 and len(result.attempts) == 2

    class Unavailable:
        async def extract(self, task, source, schema, tier):
            raise ModelFailure("MODEL_UNAVAILABLE", "Unavailable.", ())

    with pytest.raises(ModelFailure, match="Unavailable"):
        await extract(Unavailable(), prepared_text("SYNTHETIC invoice"))


async def test_failed_stronger_reading_keeps_fast_fields_and_reports_the_failed_escalation():
    class Gateway:
        async def extract(self, task, source, schema, tier):
            if tier == "strong":
                raise ModelFailure("MODEL_SCHEMA_INVALID", "Invalid reading.", ({"tier": tier},))
            return Reading(invoice(confidence_label="low"), ({"tier": tier},))

    result = await extract(Gateway(), prepared_text("SYNTHETIC invoice"))
    assert (
        result.reading.confidence_label == "low"
        and "stronger_reading:failed" in result.checks.flags
    )
    assert len(result.attempts) == 2
    assert result.selected_tier == "fast"


def test_intake_prompt_has_stable_schema_and_prompt_versions():
    assert INTAKE.name == "intake" and INTAKE.schema_version == "1"
    assert replace(INTAKE, prompt_version="intake-v2") != INTAKE


def test_model_output_cannot_supply_confirmation_authority():
    payload = invoice().model_dump(mode="json") | {"confirmed": True}
    with pytest.raises(ValidationError):
        IntakeReading.model_validate(payload)


async def test_replay_miss_is_not_hidden_by_a_lower_confidence_fast_reading():
    saved = []

    class Gateway:
        async def extract(self, task, source, schema, tier):
            if tier == "strong":
                failure = DomainError("REPLAY_MISS", "No exact replay.", 409)
                failure.attempts = ({"tier": "strong"},)
                raise failure
            return Reading(invoice(confidence_label="low"), ({"tier": "fast"},))

    async def record(attempts, value):
        saved.append((attempts, value))

    with pytest.raises(DomainError) as failed:
        await extract(Gateway(), prepared_text("SYNTHETIC invoice"), record=record)
    assert failed.value.code == "REPLAY_MISS" and len(saved) == 2
    assert saved[0][1]["total"] == "290.00" and saved[1][1] is None


def test_overflow_and_unknown_additional_fees_are_review_conditions():
    huge = invoice(
        lines=[
            {
                "description": "cups",
                "quantity": "999999999999",
                "unit_price": "999999999999",
                "unit": "piece",
            }
        ]
    )
    assert "lines.0.amount:overflow" in check_reading(huge).flags
    reading = invoice(additional_fees=[{"label": "unreadable charge", "amount": None}])
    checked = check_reading(reading)
    assert "additional_fees.0.amount:missing" in checked.flags and checked.calculated_total is None


@pytest.mark.parametrize(
    "kind,expected", [("supplier_reply", ["fast"]), ("unreadable", ["fast", "strong"])]
)
async def test_non_record_inputs_never_become_an_invented_purchase(kind, expected):
    calls = []

    class Gateway:
        async def extract(self, task, source, schema, tier):
            calls.append(tier)
            return Reading(IntakeReading(kind=kind, confidence_label="review"), ())

    result = await extract(Gateway(), prepared_text("SYNTHETIC non-record source"))
    assert calls == expected and result.reading.kind == kind
    assert result.reading.lines == [] and result.reading.total is None


def fixture_cases():
    spec = importlib.util.spec_from_file_location(
        "intake_fixtures", ROOT / "scripts/generate_replay_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.intake_cases()


@pytest.mark.parametrize(
    "index,kind,tiers",
    [(0, "purchase", ["fast", "strong"]), (1, "purchase", ["fast"]), (2, "stock", ["fast"])],
)
async def test_real_prepared_image_pdf_and_text_follow_exact_authored_replays(index, kind, tiers):
    document, _, _ = fixture_cases()[index]
    result = await extract(ReaderGateway(Settings(_env_file=None, model_mode="replay")), document)
    assert result.reading.kind == kind
    assert [attempt["tier"] for attempt in result.attempts] == tiers
    assert all(attempt["provenance"] == "authored_synthetic_replay" for attempt in result.attempts)
    if kind == "purchase":
        assert result.reading.lines[0].unit_price == Decimal("0.275")
        assert result.reading.total == Decimal("290.00")
        assert result.reading.discount is None and "discount:missing" in result.checks.flags
    else:
        assert result.reading.stock_quantity == Decimal("750")
        assert result.reading.reported_usage_per_day == Decimal("125")
        assert result.reading.stock_observed_at is None


async def test_rotated_photo_like_jpeg_uses_its_own_prepared_content_and_replay(tmp_path):
    # A synthetic JPEG tests the camera-format/orientation path, not live OCR accuracy.
    with Image.open(ROOT / "fixtures/sources/delivered-invoice.png") as original:
        photo = original.transpose(Image.Transpose.ROTATE_90)
        exif = Image.Exif()
        exif[274] = 6
        output = io.BytesIO()
        photo.save(output, format="JPEG", exif=exif)
    prepared = inspect_source(output.getvalue(), "image/jpeg")
    page = prepared["pages"][0]
    assert (page["width"], page["height"]) == (1000, 1400)
    normalized = {
        "version": "1",
        "text": None,
        "pages": [{key: page[key] for key in ("page", "sha256", "width", "height")}],
    }
    document = normalized | {"sha256": digest(normalized), "pages": prepared["pages"]}
    assert hashlib.sha256(page["content"]).hexdigest() == page["sha256"]
    store = ReplayStore(tmp_path)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"version": "1", "synthetic_sources": [document["sha256"]]})
    )
    gateway = ReaderGateway(Settings(_env_file=None, model_mode="replay"), replay=store)
    correct = fixture_cases()[0][2]
    store.record(
        gateway.key(INTAKE, document["sha256"], "fast"),
        correct.model_dump_json(),
        {},
        provenance="authored_synthetic_replay",
    )
    result = await extract(gateway, document)
    assert result.reading.total == Decimal("290.00") and len(result.attempts) == 1
