"""Author synthetic interpretation fixtures; this script never calls a model."""

import json
from pathlib import Path

from collective.adapters.models import ReaderGateway, prepared_text
from collective.adapters.replay import ReplayStore
from collective.agents.liaison import QUOTE
from collective.agents.schemas import QuoteReading
from collective.services.source_preparation import redact_text
from collective.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def main():
    cases = {
        "quote-zh.txt": {
            "description": "250ml纸杯，80mm杯口",
            "currency": "MYR",
            "base_unit": "piece",
            "tier_kind": "all_unit",
            "tiers": [
                {"min_quantity": "1000", "unit_price": "0.30"},
                {"min_quantity": "2000", "unit_price": "0.27"},
                {"min_quantity": "3000", "unit_price": "0.24"},
            ],
            "delivery_mode": "per_business",
            "delivery_fee": "15.00",
            "credit_days": 30,
            "pooled_buyers_permitted": True,
            "validity_text": "2026-10-25",
            "conditions": "允许独立买家拼单、分开发票和配送。",
            "conditions_complete": False,
            "issues": [{"field_path": "valid_until", "reason": "ambiguous"}],
            "confidence_label": "review",
        },
        "quote-missing-delivery.txt": {
            "description": "250ml paper cups",
            "currency": "MYR",
            "base_unit": "piece",
            "tier_kind": "all_unit",
            "per_business_moq": "1000",
            "tiers": [{"min_quantity": "1000", "unit_price": "0.30"}],
            "issues": [
                {"field_path": "delivery_fee", "reason": "missing"},
                {"field_path": "tax_rate", "reason": "missing"},
            ],
            "confidence_label": "review",
        },
    }
    documents = {}
    for filename, reading in cases.items():
        text, _ = redact_text((ROOT / "fixtures/sources" / filename).read_text(), [])
        documents[filename] = prepared_text(text)
        literal = "RM0.30"
        start = text.index(literal)
        reading["evidence"] = [
            {
                "field_path": "tiers.0.unit_price",
                "region_quality": "text",
                "text_span": [start, start + len(literal)],
                "extracted_value": literal,
            }
        ]
    store = ReplayStore(ROOT / "fixtures/replay")
    manifest = json.loads((store.root / "manifest.json").read_text())
    manifest["synthetic_sources"] = sorted(
        set(manifest["synthetic_sources"])
        | {doc["sha256"] for doc in documents.values()}
    )
    (store.root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    gateway = ReaderGateway(Settings(_env_file=ROOT / ".env"), replay=store)
    for filename, document in documents.items():
        value = QuoteReading.model_validate(cases[filename])
        store.record(
            gateway.key(QUOTE, document["sha256"], "fast"),
            value.model_dump_json(),
            {},
            provenance="authored_synthetic_replay",
        )
    print(
        f"Verified {len(cases)} authored synthetic quote replays; no live model calls."
    )


if __name__ == "__main__":
    main()
