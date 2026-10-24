"""Rehearse with loopback-only host processes and no Supabase container default routes."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".cache/offline-network.json"
PROFILE = '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'


def docker(*arguments):
    return subprocess.check_output(["docker", *arguments], text=True).strip()


def route(state, container, *arguments):
    # The cached helper shares only this container's network namespace, never the host's.
    output = docker(
        "run",
        "--rm",
        "--network",
        "container:" + container,
        "--cap-add",
        "NET_ADMIN",
        "--entrypoint",
        "/sbin/ip",
        state["image"],
        *arguments,
    )
    if arguments[-3:] == ("route", "show", "default"):
        return "\n".join(
            line.strip() for line in output.splitlines() if line.startswith("default ")
        )
    return output


def restore(state):
    failures = []
    for container in state["containers"]:
        try:
            if not route(state, container["name"], "route", "show", "default"):
                route(
                    state,
                    container["name"],
                    "route",
                    "add",
                    "default",
                    "via",
                    container["gateway"],
                    "dev",
                    container["dev"],
                )
        except subprocess.CalledProcessError:
            failures.append(container["name"])
    if failures:
        raise RuntimeError(
            f"Route restore incomplete for {failures}; rerun with --restore"
        )
    STATE.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if args.restore:
        restore(json.loads(STATE.read_text()))
        return
    if sys.platform != "darwin":
        # shortcut: macOS host isolation; use a Linux network namespace for an offline CI run.
        raise SystemExit(
            "This rehearsal requires macOS sandbox-exec; no offline result was claimed."
        )
    if STATE.exists():
        raise SystemExit(
            "An interrupted rehearsal needs --restore before starting another."
        )
    project = tomllib.loads((ROOT / "supabase/config.toml").read_text())["project_id"]
    original = "supabase_network_" + project
    network = json.loads(docker("network", "inspect", original))[0]
    assert not network["EnableIPv6"], (
        "This rehearsal expects the declared IPv4-only local stack"
    )
    state = {
        "image": docker(
            "inspect", "--format", "{{.Config.Image}}", "supabase_db_" + project
        ),
        "containers": [],
    }
    for identity, endpoint in network["Containers"].items():
        name = endpoint["Name"]
        if not name.startswith("supabase_") or not name.endswith("_" + project):
            raise SystemExit(
                "Refusing to isolate a container outside this Supabase project"
            )
        networks = json.loads(
            docker(
                "inspect", "--format", "{{json .NetworkSettings.Networks}}", identity
            )
        )
        if set(networks) != {original}:
            raise SystemExit(
                "Offline rehearsal requires project containers on one network only"
            )
        routes = route(state, name, "route", "show", "default")
        match = re.fullmatch(r"default via (\S+) dev (\S+)(?: +[^\n]*)?", routes)
        assert match, "Expected one default gateway"
        assert not route(state, name, "-f", "inet6", "route", "show", "default")
        state["containers"].append({"name": name, "gateway": match[1], "dev": match[2]})
    if len(state["containers"]) < 5:
        raise SystemExit("Start local Supabase Auth/Storage/DB first")
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        for container in state["containers"]:
            route(state, container["name"], "route", "del", "default")
            assert not route(state, container["name"], "route", "show", "default")
        # Direct IP avoids confusing DNS failure with an enforced outbound restriction.
        probe = subprocess.run(
            [
                "sandbox-exec",
                "-p",
                PROFILE,
                "/usr/bin/curl",
                "--noproxy",
                "*",
                "--max-time",
                "2",
                "--silent",
                "http://1.1.1.1",
            ],
            capture_output=True,
            check=False,
        )
        assert probe.returncode != 0, "Host egress was not blocked"
        print(
            f"Offline isolation: loopback-only host descendants; {len(state['containers'])} containers without external routes",
            flush=True,
        )
        subprocess.run(
            [
                "sandbox-exec",
                "-p",
                PROFILE,
                sys.executable,
                str(ROOT / "scripts/check_demo.py"),
            ],
            cwd=ROOT,
            env=os.environ
            | {
                "ORBYTE_OFFLINE": "1",
                "UV_OFFLINE": "1",
                "DEMO_MODE": "true",
                "MODEL_MODE": "replay",
                "EMBEDDING_MODE": "replay",
                "GEMINI_API_KEY": "",
                "EMBEDDING_API_KEY": "",
            },
            check=True,
        )
    finally:
        restore(state)
        print("Original Supabase routes restored.", flush=True)


if __name__ == "__main__":
    main()
