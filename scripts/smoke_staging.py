"""Read-only staging checks. Missing credentials/scenario data are pending, never a pass."""

import argparse
import json
import os
from urllib.parse import urlsplit

import httpx


def target(value):
    url = urlsplit(value)
    if (
        url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError(
            "Use an origin URL without credentials, path, query or fragment"
        )
    if not url.hostname or (
        url.scheme != "https"
        and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        )
    ):
        raise ValueError("Staging requires HTTPS; HTTP is permitted only on loopback")
    return value.rstrip("/")


def check(client, token=None):
    passed, pending = [], []

    def get(path, *, authorized=False):
        response = client.get(
            path, headers={"Authorization": "Bearer " + token} if authorized else {}
        )
        response.raise_for_status()
        assert response.headers.get("cache-control") == "no-store"
        return response.json()

    for path in ("/v1/health", "/v1/ready"):
        assert get(path)["status"] == "ok"
        passed.append(path)
    assert client.get("/v1/me").status_code == 401
    passed.append("anonymous access denied")
    if not token:
        return {
            "passed": passed,
            "pending": [
                "Authenticated staging scenario: configure COLLECTIVE_STAGING_TOKEN"
            ],
            "status": "pending",
        }
    identity = get("/v1/me", authorized=True)
    passed.append("verified sign-in")
    owners = [item for item in identity["memberships"] if item["role"] == "owner"]
    assert owners, "Use an owner token for the declared synthetic staging scenario"
    root = "/v1/businesses/" + owners[0]["business_id"]
    sources = get(root + "/sources", authorized=True)["items"]
    if sources:
        response = client.get(
            root + f"/sources/{sources[0]['id']}/view?variant=original",
            headers={"Authorization": "Bearer " + token},
        )
        response.raise_for_status()
        assert response.content
        passed.append("private source storage read")
    else:
        pending.append("Seed and verify a private synthetic source")
    needs = get(root + "/needs", authorized=True)["items"]
    if needs:
        need = root + f"/needs/{needs[0]['id']}"
        comparison = get(need + "/comparisons", authorized=True)
        assert comparison["offers"], "No staged supplier quotes"
        passed.append("quote comparisons")
        pool = needs[0]["reserved_pool_id"]
        if pool:
            response = client.get(
                root + f"/pools/{pool}/offer",
                headers={"Authorization": "Bearer " + token},
            )
            response.raise_for_status()
            assert response.json()
            passed.append("pool read")
        else:
            pending.append("Run the staged pool scenario")
        response = client.get(
            need + "/order", headers={"Authorization": "Bearer " + token}
        )
        if response.status_code == 200:
            assert response.json()["implementation"] == "simulated"
            passed.append("issued simulated PO read")
        else:
            assert response.status_code == 404
            pending.append("Run the staged approval and simulated PO scenario")
    else:
        pending.append("Seed and verify quote, pool and simulated PO scenario")
    return {
        "passed": passed,
        "pending": pending,
        "status": "pending" if pending else "passed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, type=target)
    args = parser.parse_args()
    try:
        with httpx.Client(
            base_url=args.base_url, timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            result = check(client, os.environ.get("COLLECTIVE_STAGING_TOKEN"))
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "passed" else 2)
    except (httpx.HTTPError, AssertionError, KeyError) as error:
        # Never print response/request objects: they can contain authorization credentials.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
