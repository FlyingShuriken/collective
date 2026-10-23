from uuid import uuid4

import pytest

from collective.repositories.db import set_context
from tests.integration.conftest import local_test_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://test:password@db.example.org:54322/postgres",
        "postgresql://test:password@127.0.0.1:5432/postgres",
        "postgresql://test:password@localhost:54322/real_records",
        "host=127.0.0.1 hostaddr=203.0.113.10 port=54322 dbname=postgres user=test",
    ],
)
def test_destructive_fixture_guard_rejects_remote_or_wrong_target(monkeypatch, url):
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    with pytest.raises(ValueError, match="refuse"):
        local_test_url("TEST_DATABASE_URL")


async def test_context_rejects_unverified_subject_and_arbitrary_role_before_sql():
    with pytest.raises(ValueError, match="verified"):
        await set_context(None, "tenant", None)
    with pytest.raises(ValueError, match="capability"):
        await set_context(None, "postgres", uuid4())
