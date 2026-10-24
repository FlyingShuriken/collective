"""Run the declared synthetic replay against owned production web/API processes."""

import json
import os
import socket
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

import httpx
from collective.adapters.models import ReaderGateway, prepared_text
from collective.agents.intake import INTAKE
from collective.api.errors import DomainError
from collective.repositories.db import require_local_database
from collective.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def wait_ready(url, process):
    with httpx.Client(timeout=2, trust_env=False) as client:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError(
                    "Rehearsal process exited; inspect .cache/demo-*.log"
                )
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
    raise RuntimeError("Rehearsal service did not become ready")


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, 15)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 9)
            process.wait()


def main():
    settings = Settings()
    require_local_database(settings.database_url.get_secret_value())
    if (
        settings.model_mode != "replay"
        or settings.embedding_mode != "replay"
        or not settings.demo_mode
    ):
        raise SystemExit(
            "Configure local demo mode with model and embedding replay first"
        )
    if not (ROOT / "apps/web/.next/BUILD_ID").exists():
        raise SystemExit("Build the production web app before the rehearsal")
    for port in (8000, 3000, 8101):
        with socket.socket() as connection:
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(
                    f"Stop the development service on port {port} before the owned rehearsal"
                )
    env = os.environ | {
        "MODEL_MODE": "replay",
        "EMBEDDING_MODE": "replay",
        "DEMO_MODE": "true",
        "GEMINI_API_KEY": "",
        "EMBEDDING_API_KEY": "",
        "ENABLE_WORKER": "true",
        "NEXT_TELEMETRY_DISABLED": "1",
        "ORBYTE_REHEARSAL": "1",
    }
    # Prove exact misses fail before starting, without provider credentials or live fallback.
    gateway = ReaderGateway(settings.model_copy(update={"gemini_api_key": None}))
    try:
        gateway.replay.read(
            gateway.key(
                INTAKE, prepared_text("unlisted offline probe")["sha256"], "fast"
            )
        )
    except DomainError as error:
        assert error.code == "REPLAY_MISS"
    else:
        raise AssertionError("An unlisted replay unexpectedly matched")
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    with ExitStack() as stack:
        for name, command, url in (
            (
                "api",
                [
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
                    "8000",
                    "--no-access-log",
                ],
                "http://127.0.0.1:8000/v1/ready",
            ),
            (
                "web",
                [
                    "pnpm",
                    "--dir",
                    "apps/web",
                    "start",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    "3000",
                ],
                "http://127.0.0.1:3000",
            ),
        ):
            log = stack.enter_context((cache / f"demo-{name}.log").open("w"))
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            stack.callback(stop, process)
            wait_ready(url, process)
        subprocess.run(
            [
                "pnpm",
                "exec",
                "playwright",
                "test",
                "tests/e2e/local-replay.spec.ts",
                "tests/e2e/recovery.spec.ts",
                "tests/e2e/preliminary-flow.spec.ts",
                "tests/e2e/single-business.spec.ts",
                "tests/e2e/liaison.spec.ts",
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
    result = {
        "status": "passed",
        "offline_isolation": os.environ.get("ORBYTE_OFFLINE") == "1",
        "provider_keys": "absent",
        "models": "synthetic replay",
        "orders_and_messages": "simulated",
    }
    (cache / "demo-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
