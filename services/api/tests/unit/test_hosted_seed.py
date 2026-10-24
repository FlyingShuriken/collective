import runpy
from pathlib import Path

import pytest

from tests.fixtures import ROOT

module = runpy.run_path(str(ROOT / "scripts/seed_demo.py"))
seed, seed_target = module["seed"], module["seed_target"]

REF = "abcdefghijklmnopqrst"


def hosted_env():
    return {
        "DEMO_MODE": "true",
        "SUPABASE_URL": f"https://{REF}.supabase.co",
        "MIGRATION_DATABASE_URL": (
            f"postgresql://postgres:example@db.{REF}.supabase.co:5432/postgres?sslmode=require"
        ),
    }


def test_hosted_seed_accepts_explicit_direct_and_session_connections():
    env = hosted_env()
    assert seed_target(env, REF) == (env["MIGRATION_DATABASE_URL"], env["SUPABASE_URL"])
    env["MIGRATION_DATABASE_URL"] = (
        f"postgresql://postgres.{REF}:example@aws-0-ap-southeast-1.pooler.supabase.com"
        ":5432/postgres?sslmode=require"
    )
    assert seed_target(env, REF)[0] == env["MIGRATION_DATABASE_URL"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("DEMO_MODE", "false"),
        ("SUPABASE_URL", "https://another-project.supabase.co"),
        ("SUPABASE_URL", f"https://{REF}.supabase.co.evil.example"),
        ("MIGRATION_DATABASE_URL", "host=localhost port=5432 dbname=postgres user=postgres"),
        ("MIGRATION_DATABASE_URL", f"host=db.{REF}.supabase.co port=5432 dbname=postgres "
         "user=postgres sslmode=disable"),
        ("MIGRATION_DATABASE_URL", f"host=db.{REF}.supabase.co port=6543 dbname=postgres "
         "user=postgres sslmode=require"),
        ("MIGRATION_DATABASE_URL", "host=aws-0-ap-southeast-1.pooler.supabase.com port=5432 "
         "dbname=postgres user=postgres.otherproject sslmode=require"),
        ("MIGRATION_DATABASE_URL", f"host=db.{REF}.supabase.co hostaddr=127.0.0.1 port=5432 "
         "dbname=postgres user=postgres sslmode=require"),
    ],
)
def test_hosted_seed_rejects_wrong_or_unencrypted_targets(key, value):
    env = hosted_env() | {key: value}
    with pytest.raises(ValueError):
        seed_target(env, REF)


def test_remote_seed_remains_refused_without_explicit_hosted_arguments():
    with pytest.raises(ValueError, match="refuse"):
        seed_target(hosted_env())
    with pytest.raises(ValueError, match="both"):
        seed(project_ref=REF)
    with pytest.raises(ValueError, match="both"):
        seed(env_file=Path("not-read.env"))
    with pytest.raises(ValueError, match="reference"):
        seed_target(hosted_env(), "not-a-project")
