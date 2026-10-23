"""Generate small synthetic source assets and their independently authored gold fields."""

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "fixtures/sources"


def main() -> None:
    SOURCES.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=24)
    lines = [
        ("label", "SYNTHETIC DEMO - NOT A REAL INVOICE"),
        ("supplier", "Supplier B"),
        ("buyer", "Cafe B"),
        ("document_number", "DEMO-B-001"),
        ("invoice_date", "2026-10-23"),
        ("currency", "MYR"),
        ("lines.0.description", "250ml paper cups, 80mm lid, paper"),
        ("lines.0.quantity", "1000 pieces"),
        ("lines.0.unit_price", "0.275"),
        ("lines.0.total", "275.00"),
        ("delivery_fee", "15.00"),
        ("tax", "0.00"),
        ("total", "290.00"),
        ("credit_days", "30 days"),
    ]
    locations = {}
    for index, (field, text) in enumerate(lines):
        x, y = (60 if field == "label" else 400), 70 + index * 72
        if field != "label":
            label = field.split(".")[-1].replace("_", " ").title()
            draw.text((60, y), label, fill="black", font=font)
        draw.text((x, y), text, fill="black", font=font)
        bounds = draw.textbbox((x, y), text, font=font)
        locations[field] = [
            round(v * 1000 / image.size[i % 2]) for i, v in enumerate(bounds)
        ]
    image.save(SOURCES / "delivered-invoice.png")
    image.save(
        SOURCES / "delivered-invoice.pdf",
        resolution=144,
        creationDate="D:20261023010000Z",
        modDate="D:20261023010000Z",
    )
    messages = {
        "stock-ms.txt": "DEMO SINTETIK. Stok cawan kertas 250ml: 750 keping pada 23 Oktober 2026. Guna 125 keping sehari.\n",
        "quote-zh.txt": "合成演示报价。250ml纸杯，80mm杯口。1000个每个RM0.30；2000个RM0.27；3000个RM0.24。每家运费RM15，账期30天。允许独立买家拼单、分开发票和配送。报价于2026-10-25到期。\n",
        "quote-missing-delivery.txt": "SYNTHETIC. 250ml paper cups: RM0.30/piece, minimum 1000. Delivery and tax treatment to be confirmed.\n",
    }
    for name, content in messages.items():
        (SOURCES / name).write_text(content)
    entries = []
    for path in sorted(SOURCES.iterdir()):
        if path.name == "manifest.json":
            continue
        entries.append(
            {
                "id": str(uuid5(NAMESPACE_URL, "orbyte-fixture:" + path.name)),
                "path": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "provenance": "synthetic",
                "page_count": 1 if path.suffix in {".png", ".pdf"} else None,
            }
        )
    (SOURCES / "manifest.json").write_text(json.dumps(entries, indent=2) + "\n")
    gold = {
        "provenance": "authored synthetic source truth; not model output",
        "delivered-invoice": {
            "document_number": "DEMO-B-001",
            "invoice_date": "2026-10-23",
            "currency": "MYR",
            "quantity": "1000",
            "unit_price": "0.275",
            "line_total": "275.00",
            "delivery_fee": "15.00",
            "tax": "0.00",
            "total": "290.00",
            "page_dimensions": [1000, 1400],
            "bbox_normalized": locations,
        },
        "quote-missing-delivery": {
            "delivery_fee": None,
            "tax_rate": None,
            "reason": "missing",
        },
        "stock-ms": {
            "quantity": "750",
            "base_unit": "piece",
            "usage_per_day": "125",
            "observed_date": "2026-10-23",
            "source_language": "ms",
        },
        "quote-zh": {
            "tiers": [["1000", "0.30"], ["2000", "0.27"], ["3000", "0.24"]],
            "delivery_fee": "15.00",
            "credit_days": 30,
            "pooled_buyers_permitted": True,
            "valid_until_date": "2026-10-25",
            "source_language": "zh",
        },
    }
    (ROOT / "fixtures/gold/source-fields.json").write_text(
        json.dumps(gold, indent=2) + "\n"
    )
    print(f"Generated {len(entries)} synthetic sources")


if __name__ == "__main__":
    main()
