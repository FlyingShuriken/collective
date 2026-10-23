import importlib.util

import pytest

from tests.fixtures import ROOT


def fixture_checker():
    spec = importlib.util.spec_from_file_location(
        "fixture_check", ROOT / "scripts/check_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_check_and_tampered_source_rejection(tmp_path):
    module = fixture_checker()
    module.check()
    # Use an isolated copy so the negative check never edits committed source truth.
    import shutil

    shutil.copytree(ROOT / "fixtures", tmp_path / "fixtures")
    module.ROOT = tmp_path
    (tmp_path / "fixtures/sources/stock-ms.txt").write_text("tampered")
    with pytest.raises(AssertionError):
        module.check()
