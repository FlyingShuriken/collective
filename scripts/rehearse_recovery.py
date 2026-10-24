"""Local fault injection: kill a claimed draft worker and API, then reclaim exactly once."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from functools import partial
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from collective.agents.liaison import DRAFT
from collective.api.dto import RequestDraft
from collective.domain.clock import SystemClock
from collective.jobs.runner import Runner
from collective.repositories import jobs
from collective.repositories.db import Database, require_local_database
from collective.services.liaison_jobs import draft_context, handle_draft
from collective.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
KIND = "rehearsal-draft-recovery"


def settings():
    value = Settings(enable_worker=False, gemini_api_key=None, embedding_api_key=None)
    require_local_database(value.database_url.get_secret_value())
    if not value.demo_mode or value.model_mode != "replay":
        raise ValueError("Recovery injection is restricted to local synthetic replay")
    return value


async def worker(identity, marker, crash):
    value = settings()
    database = Database(value.database_url.get_secret_value())
    await database.open()
    try:
        async with database.transaction("coordinator", None) as conn:
            current = await (
                await conn.execute(
                    "select kind from collective.jobs where id=%s", (identity,)
                )
            ).fetchone()
            kind = current["kind"]
            assert kind.startswith(KIND + ":")
        if crash:
            async with database.transaction("coordinator", None) as conn:
                # Exercise real expiry in seconds; the production lease remains five minutes.
                claim = await jobs.claim(
                    conn,
                    uuid4(),
                    (kind,),
                    SystemClock().now() - jobs.LEASE + timedelta(seconds=2),
                )
                assert claim and claim["id"] == identity
            marker.write_text(str(identity))
            await asyncio.sleep(300)
        else:
            handler = partial(handle_draft, settings=value)
            assert await Runner(database, handlers={kind: handler}).run_once()
    finally:
        await database.close()


async def prepare(run, supplier):
    from preload_query_replay import preload

    scenario = json.loads((ROOT / "fixtures/scenarios/three-cafes.json").read_text())
    if run == "demo":
        raise ValueError("Use a fresh rehearsal run, not the shared demo")
    seed = json.loads((ROOT / f".cache/demo-{run}.json").read_text())
    business = uuid5(
        NAMESPACE_URL, f"orbyte-run:{run}:{UUID(scenario['businesses'][0]['id'])}"
    )
    need = uuid5(
        NAMESPACE_URL, f"orbyte-run:{run}:{UUID(scenario['businesses'][0]['need_id'])}"
    )
    authored = await preload(run, need, kind="rfq", supplier_id=supplier)
    value = settings()
    database = Database(value.database_url.get_secret_value())
    await database.open()
    try:
        async with database.transaction("coordinator", None) as conn:
            body = RequestDraft(
                kind="rfq", reference_id=need, expected_revision=1, supplier_id=supplier
            )
            facts, recipient = await draft_context(conn, business, body)
            payload = {
                "request": body.model_dump(mode="json"),
                "facts": facts,
                "recipient": recipient,
                "requester": seed["accounts"]["owner-a"]["id"],
                "schema_version": DRAFT.schema_version,
                "prompt_version": DRAFT.prompt_version,
            }
            row = await jobs.enqueue(
                conn,
                business=business,
                kind=KIND + ":" + run,
                revision=1,
                key="recovery:" + run,
                payload=payload,
                due=SystemClock().now() - jobs.LEASE,
            )
        return row, authored, need, business
    finally:
        await database.close()


async def verify(row):
    database = Database(settings().database_url.get_secret_value())
    await database.open()
    try:
        async with database.transaction("coordinator", None) as conn:
            again = await jobs.enqueue(
                conn,
                business=row["business_id"],
                kind=row["kind"],
                revision=1,
                key=row["dedup_key"],
                payload=row["payload"],
                due=row["next_attempt_at"],
            )
            assert (
                again["id"] == row["id"]
                and again["status"] == "succeeded"
                and again["attempts"] == 2
            )
            draft = UUID(again["result"]["draft_id"])
            events = await (
                await conn.execute(
                    "select id from collective.events where correlation_id=%s and event_type='draft_proposed'",
                    (row["id"],),
                )
            ).fetchall()
            assert len(events) == 1
            assert (
                await conn.execute(
                    "select id from collective.message_drafts where id=%s", (draft,)
                )
            ).rowcount == 1
            assert (
                await jobs.claim(conn, uuid4(), (row["kind"],), SystemClock().now())
                is None
            )
            return {
                "job_id": str(row["id"]),
                "draft_id": str(draft),
                "attempts": 2,
                "results": 1,
            }
    finally:
        await database.close()


def main():
    from check_demo import stop, wait_ready
    from preload_query_replay import preload

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run")
    parser.add_argument("--supplier-id", type=UUID)
    parser.add_argument("--worker", type=UUID)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--crash", action="store_true")
    args = parser.parse_args()
    if args.worker:
        asyncio.run(worker(args.worker, args.marker, args.crash))
        return
    row, authored, need, business = asyncio.run(prepare(args.run, args.supplier_id))
    marker = ROOT / f".cache/recovery-{row['id']}.claimed"
    processes = []
    env = os.environ | {
        "ENABLE_WORKER": "false",
        "GEMINI_API_KEY": "",
        "EMBEDDING_API_KEY": "",
    }
    api_command = [
        "uv",
        "run",
        "--project",
        "services/api",
        "uvicorn",
        "collective.main:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        "8101",
        "--no-access-log",
    ]
    worker_command = [
        sys.executable,
        __file__,
        "--worker",
        str(row["id"]),
        "--marker",
        str(marker),
    ]
    try:
        with (ROOT / ".cache/recovery-processes.log").open("w") as log:

            def start(command):
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                processes.append(process)
                return process

            api = start(api_command)
            wait_ready("http://127.0.0.1:8101/v1/ready", api)
            crashed = start(worker_command + ["--crash"])
            for _ in range(100):
                if marker.exists():
                    break
                assert crashed.poll() is None, (
                    "Fault-injection worker exited before its claim"
                )
                time.sleep(0.1)
            assert marker.exists(), "Worker never claimed the job"
            # SIGKILL deliberately prevents either process from running shutdown handlers.
            for process in (crashed, api):
                os.killpg(process.pid, 9)
                process.wait()
            api = start(api_command)
            wait_ready("http://127.0.0.1:8101/v1/ready", api)
            time.sleep(2)
            restarted = start(worker_command)
            assert restarted.wait(timeout=25) == 0, (
                "Restarted worker failed; inspect recovery-processes.log"
            )
            result = asyncio.run(verify(row))
            # Query the restarted real HTTP API using a newly verified Supabase JWT.
            value = settings()
            account = json.loads((ROOT / f".cache/demo-{args.run}.json").read_text())[
                "accounts"
            ]["owner-a"]
            with httpx.Client(timeout=10, trust_env=False) as client:
                signed = client.post(
                    str(value.supabase_url).rstrip("/")
                    + "/auth/v1/token?grant_type=password",
                    headers={"apikey": value.supabase_publishable_key},
                    json={"email": account["email"], "password": account["password"]},
                )
                signed.raise_for_status()
                response = client.get(
                    f"http://127.0.0.1:8101/v1/businesses/{business}/jobs/{row['id']}",
                    headers={
                        "Authorization": "Bearer " + signed.json()["access_token"]
                    },
                )
                response.raise_for_status()
                assert response.json()["status"] == "succeeded"
            print(json.dumps(result))
    finally:
        for process in reversed(processes):
            stop(process)
        marker.unlink(missing_ok=True)
        asyncio.run(
            preload(
                args.run,
                need,
                kind="rfq",
                supplier_id=args.supplier_id,
                clean=True,
                source_hash=authored["source_hash"],
            )
        )


if __name__ == "__main__":
    main()
