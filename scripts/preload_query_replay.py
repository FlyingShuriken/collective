"""Author an exact local query replay for a declared synthetic demo invoice; no model calls."""

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from collective.adapters.models import ReaderGateway, prepared_text
from collective.adapters.replay import ReplayStore, digest
from collective.agents.liaison import DRAFT
from collective.agents.schemas import DraftReading
from collective.api.dto import RequestDraft
from collective.repositories.db import Database, require_local_database
from collective.services import invoice_checks, liaison_jobs
from collective.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def manifest_write(store, manifest):
    with tempfile.NamedTemporaryFile(
        mode="w", dir=store.root, delete=False
    ) as temporary:
        temporary.write(json.dumps(manifest, indent=2) + "\n")
        path = Path(temporary.name)
    path.replace(store.root / "manifest.json")


async def preload(run, identity, *, clean=False, source_hash=None):
    if source_hash is not None and (
        not clean or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
    ):
        raise ValueError("An exact replay hash is accepted only for cleanup")
    settings = Settings(_env_file=ROOT / ".env")
    if (
        not settings.demo_mode
        or settings.model_mode != "replay"
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", run)
    ):
        raise ValueError("Use a declared local synthetic run in demo/replay mode")
    database = Database(
        require_local_database(settings.database_url.get_secret_value())
    )
    scenario = json.loads((ROOT / "fixtures/scenarios/three-cafes.json").read_text())
    businesses = [
        UUID(b["id"])
        if run == "demo"
        else uuid5(NAMESPACE_URL, f"orbyte-run:{run}:{UUID(b['id'])}")
        for b in scenario["businesses"]
    ]
    declared = {
        row["sha256"]
        for row in json.loads((ROOT / "fixtures/sources/manifest.json").read_text())
        if row["provenance"] == "synthetic"
    }
    await database.open()
    try:
        async with database.transaction("coordinator", None) as connection:
            row = await (
                await connection.execute(
                    """select c.business_id from collective.invoice_checks c
                where c.id=%s and c.business_id=any(%s)""",
                    (identity, businesses),
                )
            ).fetchone()
            if row is None:
                raise ValueError(
                    "The check does not belong to this declared synthetic run"
                )
            checked = await invoice_checks.load(
                connection, row["business_id"], identity
            )
            source = await (
                await connection.execute(
                    "select sha256 from collective.sources where business_id=%s and id=%s",
                    (row["business_id"], checked["source_id"]),
                )
            ).fetchone()
            if source is None or source["sha256"] not in declared:
                raise ValueError(
                    "Only declared synthetic source assets may be preloaded"
                )
            request = RequestDraft(
                kind="invoice_query",
                reference_id=identity,
                expected_revision=checked["revision"],
                supplier_id=checked["supplier_id"],
            )
            facts, _ = await liaison_jobs.draft_context(
                connection, row["business_id"], request
            )
    finally:
        await database.close()
    total = next((d for d in facts["differences"] if d["field_path"] == "total"), None)
    if total is None:
        raise ValueError("This declared query needs a known total difference")
    document = prepared_text(
        json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    # Learning may change current drafting facts after this exact replay was authored.
    replay_hash = source_hash or document["sha256"]
    store = ReplayStore()
    key = ReaderGateway(settings, replay=store).key(DRAFT, replay_hash, "fast")
    target = store.root / (digest(key) + ".json")
    manifest = json.loads((store.root / "manifest.json").read_text())
    if clean:
        if target.exists():
            existing = store.read(key)
            if existing["provenance"] != "authored_synthetic_replay":
                raise ValueError("Refusing to remove a recorded model observation")
            target.unlink()
        used = any(
            json.loads(path.read_text()).get("key", {}).get("source_hash")
            == replay_hash
            for path in store.root.glob("*.json")
            if path.name != "manifest.json"
        )
        if not used:
            manifest["synthetic_sources"] = [
                value for value in manifest["synthetic_sources"] if value != replay_hash
            ]
            manifest_write(store, manifest)
    else:
        manifest["synthetic_sources"] = sorted(
            set(manifest["synthetic_sources"]) | {document["sha256"]}
        )
        manifest_write(store, manifest)
        wording = {
            "en": (
                f"Please review invoice {facts['document_number']}. "
                f"The recorded invoice total is RM {total['invoiced']}; our issued PO total is "
                f"RM {total['agreed']}; the difference is RM {total['delta']}. "
                "Please confirm whether a correction or credit is due."
            ),
            "ms": (
                f"Sila semak invois {facts['document_number']}. "
                f"Jumlah invois direkodkan RM {total['invoiced']}; jumlah PO dikeluarkan "
                f"RM {total['agreed']}; perbezaan RM {total['delta']}. "
                "Sila sahkan sama ada pembetulan atau kredit diperlukan."
            ),
            "zh": (
                f"请核对发票 {facts['document_number']}。记录的发票总额为 RM {total['invoiced']}，"
                f"已签发采购单总额为 RM {total['agreed']}，差额为 RM {total['delta']}。"
                "请确认是否需要更正或贷项。"
            ),
        }
        value = DraftReading(text_content=wording[facts["language"]])
        store.record(
            key, value.model_dump_json(), {}, provenance="authored_synthetic_replay"
        )
    return {"source_hash": replay_hash, "cleaned": clean}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--check-id", required=True, type=UUID)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument(
        "--source-hash", help="Original preload hash when current facts have changed"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                preload(
                    args.run,
                    args.check_id,
                    clean=args.clean,
                    source_hash=args.source_hash,
                )
            )
        )
    )


if __name__ == "__main__":
    main()
