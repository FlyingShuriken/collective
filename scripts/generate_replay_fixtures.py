"""Author synthetic interpretation fixtures; this script never calls a model."""

import json
from pathlib import Path

from collective.adapters.models import ReaderGateway, prepared_text
from collective.adapters.replay import ReplayStore, digest
from collective.agents.intake import INTAKE
from collective.agents.liaison import QUOTE
from collective.agents.schemas import IntakeReading, QuoteReading
from collective.services.source_preparation import redact_text
from collective.services.source_rendering import inspect_source
from collective.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def prepared_fixture(filename):
    mime = "application/pdf" if filename.endswith(".pdf") else "image/png"
    content = (ROOT / "fixtures/sources" / filename).read_bytes()
    prepared = inspect_source(content, mime)
    normalized = {
        "version": "1",
        "text": prepared["text"],
        "pages": [
            {key: page[key] for key in ("page", "sha256", "width", "height")}
            for page in prepared["pages"]
        ],
    }
    return normalized | {"sha256": digest(normalized), "pages": prepared["pages"]}


def intake_cases():
    fields = json.loads((ROOT / "fixtures/gold/source-fields.json").read_text())[
        "delivered-invoice"
    ]
    invoice = IntakeReading.model_validate(
        {
            "kind": "purchase",
            "supplier_name": "Supplier B",
            "document_number": fields["document_number"],
            "date_text": fields["invoice_date"],
            "document_date": fields["invoice_date"],
            "currency": fields["currency"],
            "goods_total": fields["line_total"],
            "delivery_fee": fields["delivery_fee"],
            "tax_amount": fields["tax"],
            "total": fields["total"],
            "credit_days": 30,
            "confidence_label": "high",
            "lines": [
                {
                    "description": "250ml paper cups, 80mm lid, paper",
                    "quantity": fields["quantity"],
                    "unit": "piece",
                    "unit_price": fields["unit_price"],
                    "line_total": fields["line_total"],
                }
            ],
            "evidence": [
                {
                    "field_path": "lines.0.unit_price",
                    "page": 1,
                    "bbox_normalized": fields["bbox_normalized"]["lines.0.unit_price"],
                    "region_quality": "approximate",
                    "extracted_value": fields["unit_price"],
                    "confidence_label": "high",
                }
            ],
        }
    )
    # Authored fixtures exercise rereading; they are not measured model observations.
    uncertain = invoice.model_dump(mode="json")
    uncertain["lines"][0]["unit_price"] = "0.270"
    uncertain["confidence_label"] = "review"
    text = (ROOT / "fixtures/sources/stock-ms.txt").read_text()
    start = text.index("750")
    stock = IntakeReading(
        kind="stock",
        stock_description="cawan kertas 250ml",
        stock_quantity="750",
        stock_unit="keping",
        stock_date_text="23 Oktober 2026",
        reported_usage_per_day="125",
        confidence_label="high",
        evidence=[
            {
                "field_path": "stock_quantity",
                "region_quality": "text",
                "text_span": [start, start + 3],
                "extracted_value": "750",
                "confidence_label": "high",
            }
        ],
    )
    single_text = (ROOT / "fixtures/sources/single-business-invoice.txt").read_text()
    price_start = single_text.index("0.305")
    single = IntakeReading.model_validate(
        {
            "kind": "purchase",
            "supplier_name": "Supplier B",
            "document_number": "SINGLE-B-002",
            "date_text": "2026-10-24",
            "document_date": "2026-10-24",
            "currency": "MYR",
            "lines": [
                {
                    "description": "250ml paper cups, 80mm lid, paper",
                    "quantity": "1000",
                    "unit": "piece",
                    "unit_price": "0.305",
                    "line_total": "305.00",
                }
            ],
            "goods_total": "305.00",
            "delivery_fee": "15.00",
            "tax_amount": "0.00",
            "tax_rate": "0",
            "discount": "0.00",
            "surcharge": "0.00",
            "total": "320.00",
            "credit_days": 30,
            "delivery_mode": "per_business",
            "confidence_label": "high",
            "evidence": [
                {
                    "field_path": "lines.0.unit_price",
                    "region_quality": "text",
                    "text_span": [price_start, price_start + 5],
                    "extracted_value": "0.305",
                    "confidence_label": "high",
                }
            ],
        }
    )
    return [
        (
            prepared_fixture("delivered-invoice.png"),
            IntakeReading.model_validate(uncertain),
            invoice,
        ),
        (prepared_fixture("delivered-invoice.pdf"), invoice, invoice),
        (prepared_text(redact_text(text, [])[0]), stock, stock),
        (prepared_text(redact_text(single_text, [])[0]), single, single),
    ]


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
    readings = intake_cases()
    store = ReplayStore(ROOT / "fixtures/replay")
    store.root.mkdir(parents=True, exist_ok=True)
    manifest_path = store.root / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"version": "1", "synthetic_sources": []}
    )
    manifest["synthetic_sources"] = sorted(
        set(manifest["synthetic_sources"])
        | {doc["sha256"] for doc in documents.values()}
        | {doc["sha256"] for doc, _, _ in readings}
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    gateway = ReaderGateway(Settings(_env_file=ROOT / ".env"), replay=store)
    for filename, document in documents.items():
        value = QuoteReading.model_validate(cases[filename])
        store.record(
            gateway.key(QUOTE, document["sha256"], "fast"),
            value.model_dump_json(),
            {},
            provenance="authored_synthetic_replay",
        )
        store.record(
            gateway.key(INTAKE, document["sha256"], "fast"),
            IntakeReading(
                kind="supplier_reply", confidence_label="high"
            ).model_dump_json(),
            {},
            provenance="authored_synthetic_replay",
        )
    for document, fast, strong in readings:
        for tier, value in (("fast", fast), ("strong", strong)):
            store.record(
                gateway.key(INTAKE, document["sha256"], tier),
                value.model_dump_json(),
                {},
                provenance="authored_synthetic_replay",
            )
    print(
        f"Verified {len(cases)} quote and {2 * len(readings) + len(cases)} intake replays; no live calls."
    )


if __name__ == "__main__":
    main()
