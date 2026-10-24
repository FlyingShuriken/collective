import runpy
from uuid import UUID

import pytest

from tests.fixtures import ROOT


@pytest.mark.parametrize(
    "clean,source_hash",
    [
        (False, "a" * 64),
        (True, "short"),
        (True, "a" * 65),
        (True, "g" * 64),
        (True, "../" + "a" * 61),
        (True, "A" * 64),
    ],
)
async def test_cleanup_hash_is_bounded_and_cannot_change_preload_inputs(
    monkeypatch, clean, source_hash
):
    preload = runpy.run_path(str(ROOT / "scripts/preload_query_replay.py"))["preload"]

    def forbidden_settings(**_):
        pytest.fail("Invalid cleanup hashes must fail before configuration or database access")

    monkeypatch.setitem(preload.__globals__, "Settings", forbidden_settings)
    with pytest.raises(ValueError, match="exact replay hash"):
        await preload("e2e-guard", UUID(int=1), clean=clean, source_hash=source_hash)
