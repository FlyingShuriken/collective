import importlib.util
import json
import shutil

from tests.fixtures import ROOT


def test_replays_bootstrap_from_sources_and_preserve_existing_records(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "generate_replay", ROOT / "scripts/generate_replay_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    for name in ("sources", "gold"):
        shutil.copytree(ROOT / "fixtures" / name, tmp_path / "fixtures" / name)
    module.main()
    folder = tmp_path / "fixtures/replay"
    original = {path.name: path.read_bytes() for path in folder.glob("*.json")}
    assert len(original) == 13
    assert len(json.loads(original["manifest.json"])["synthetic_sources"]) == 6
    module.main()
    assert {path.name: path.read_bytes() for path in folder.glob("*.json")} == original
