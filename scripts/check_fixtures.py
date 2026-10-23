"""Validate fixture structure/provenance without calling a model or purchasing calculator."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def check() -> None:
    fixture = json.loads((ROOT / "fixtures/scenarios/three-cafes.json").read_text())
    assert fixture["schema_version"] == 1 and fixture["provenance"] == "synthetic"
    assert len(fixture["businesses"]) == 3 and len(fixture["suppliers"]) == 2
    business_ids = {UUID(b["id"]) for b in fixture["businesses"]}
    assert len(business_ids) == 3
    for business in fixture["businesses"]:
        letter = business["name"][-1].lower()
        assert business["id"] == str(
            uuid5(NAMESPACE_URL, "orbyte-fixture:cafe-" + letter)
        )
        assert Decimal(business["quantity"]) == 1000
    assert len(fixture["quotes"]) == 6
    for quote in fixture["quotes"]:
        assert UUID(quote["business_id"]) in business_ids
        assert quote["spec_id"] == fixture["spec"]["id"]
        bounds = [Decimal(tier["min_quantity"]) for tier in quote["tiers"]]
        assert bounds == sorted(set(bounds))
        assert quote["delivery_fee"] == "15.00"
    manifest = json.loads((ROOT / "fixtures/sources/manifest.json").read_text())
    assert len(manifest) >= 5
    for source in manifest:
        path = (ROOT / source["path"]).resolve()
        assert path.is_relative_to(ROOT / "fixtures/sources")
        assert source["provenance"] == "synthetic"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
        UUID(source["id"])
        if path.suffix == ".png":
            with Image.open(path) as image:
                assert image.size == (1000, 1400)
                image.verify()
        if path.suffix == ".pdf":
            assert path.read_bytes().startswith(b"%PDF-")
    gold = json.loads((ROOT / "fixtures/gold/expected-scenario.json").read_text())
    assert gold["three_member"]["total_each"] == "255.00"
    assert gold["after_c_withdraws"]["a_approval"] == "void"
    assert gold["delivered_invoice"]["variance"] == "5.00"
    fields = json.loads((ROOT / "fixtures/gold/source-fields.json").read_text())
    for bounds in fields["delivered-invoice"]["bbox_normalized"].values():
        left, top, right, bottom = bounds
        assert 0 <= left < right <= 1000 and 0 <= top < bottom <= 1000
    print(f"Fixtures valid: 3 cafés, 2 suppliers, {len(manifest)} synthetic sources")


if __name__ == "__main__":
    check()
