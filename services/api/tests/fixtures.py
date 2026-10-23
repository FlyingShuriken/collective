import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[3]
ANCHOR = datetime(2026, 10, 23, 1, tzinfo=UTC)


def fixture_id(name):
    return uuid5(NAMESPACE_URL, "orbyte-fixture:" + name)


def scenario():
    return json.loads((ROOT / "fixtures/scenarios/three-cafes.json").read_text())


def scenario_time(hours=0):
    return ANCHOR + timedelta(hours=hours)


def expectations():
    return json.loads((ROOT / "fixtures/gold/expected-scenario.json").read_text())
