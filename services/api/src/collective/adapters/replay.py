"""Content-addressed interpretation fixtures, restricted to reviewed synthetic inputs."""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from collective.api.errors import DomainError

ROOT = Path(__file__).resolve().parents[5] / "fixtures/replay"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


class ReplayStore:
    def __init__(self, root=ROOT):
        self.root = root

    def permitted(self, source_hash):
        try:
            manifest = json.loads((self.root / "manifest.json").read_text())
        except (OSError, ValueError) as exc:
            raise DomainError(
                "REPLAY_UNAVAILABLE", "The replay manifest is unavailable.", 503
            ) from exc
        return manifest.get("version") == "1" and source_hash in manifest.get(
            "synthetic_sources", []
        )

    def require_permitted(self, source_hash):
        if not self.permitted(source_hash):
            raise DomainError(
                "RECORD_NOT_PERMITTED", "Only predeclared synthetic inputs may be recorded.", 409
            )

    def read(self, key):
        try:
            value = json.loads((self.root / (digest(key) + ".json")).read_text())
        except FileNotFoundError as exc:
            raise DomainError(
                "REPLAY_MISS", "No replay matches this input and version.", 409
            ) from exc
        except (OSError, ValueError) as exc:
            raise DomainError("REPLAY_INVALID", "The replay fixture is invalid.", 503) from exc
        if (
            value.get("key") != key
            or value.get("version") != "1"
            or value.get("provenance") not in {"recorded_synthetic", "authored_synthetic_replay"}
            or not isinstance(value.get("raw_output"), str)
            or not isinstance(value.get("usage"), dict)
            or value.get("sha256") != hashlib.sha256(value["raw_output"].encode()).hexdigest()
        ):
            raise DomainError("REPLAY_INVALID", "The replay fixture failed verification.", 503)
        self.require_permitted(key["source_hash"])
        return value

    def record(self, key, raw, usage, *, provenance="recorded_synthetic"):
        self.require_permitted(key["source_hash"])
        value = {
            "version": "1",
            "key": key,
            "raw_output": raw,
            "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "usage": usage,
            "provenance": provenance,
            "created_at": datetime.now(UTC).isoformat(),
        }
        target = self.root / (digest(key) + ".json")
        # Link a complete temporary file exclusively: readers never see a half-written fixture.
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as temporary:
            temporary.write(encoded(value))
            temporary.flush()
            path = Path(temporary.name)
        try:
            try:
                os.link(path, target)
            except FileExistsError:
                existing = self.read(key)
                if existing["raw_output"] != raw:
                    raise DomainError(
                        "REPLAY_CONFLICT", "An immutable replay already exists.", 409
                    ) from None
        finally:
            path.unlink(missing_ok=True)
