"""Provision local runtime credentials after a Supabase start/reset; never targets a cloud DB."""

import json
import secrets
import subprocess
from pathlib import Path

import psycopg
from collective.repositories.db import require_local_database
from dotenv import set_key
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]


def configure(status: dict) -> None:
    admin_url = require_local_database(status["DB_URL"])
    password = secrets.token_urlsafe(32)
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            sql.SQL("alter role collective_backend login password {}").format(
                sql.Literal(password)
            )
        )
    backend_url = make_conninfo(admin_url, user="collective_backend", password=password)
    target = ROOT / ".env"
    target.touch(mode=0o600, exist_ok=True)
    target.chmod(0o600)
    for key, value in {
        "DATABASE_URL": backend_url,
        "TEST_DATABASE_URL": backend_url,
        "MIGRATION_DATABASE_URL": admin_url,
        "SUPABASE_URL": status["API_URL"],
        "SUPABASE_PUBLISHABLE_KEY": status["PUBLISHABLE_KEY"],
        "SUPABASE_SECRET_KEY": status["SECRET_KEY"],
    }.items():
        set_key(target, key, value)
    web_target = ROOT / "apps/web/.env.local"
    web_target.touch(mode=0o600, exist_ok=True)
    web_target.chmod(0o600)
    for key, value in {
        "COLLECTIVE_API_URL": "http://127.0.0.1:8000",
        "NEXT_PUBLIC_SUPABASE_URL": status["API_URL"],
        "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY": status["PUBLISHABLE_KEY"],
    }.items():
        set_key(web_target, key, value)
    with psycopg.connect(backend_url) as connection:
        assert (
            connection.execute("select current_user").fetchone()[0]
            == "collective_backend"
        )
    print(
        "Local restricted login and ignored environment files configured; restart running API processes"
    )


def main() -> None:
    result = subprocess.run(
        ["pnpm", "exec", "supabase", "status", "--output", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    configure(json.loads(result.stdout))


if __name__ == "__main__":
    main()
