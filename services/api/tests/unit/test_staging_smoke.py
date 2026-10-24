import runpy

import httpx
import pytest

from tests.fixtures import ROOT

SMOKE = runpy.run_path(str(ROOT / "scripts/smoke_staging.py"))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/path",
        "https://example.com?token=private",
    ],
)
def test_staging_origin_rejects_insecure_or_credential_bearing_targets(url):
    with pytest.raises(ValueError):
        SMOKE["target"](url)


def test_health_without_scenario_credentials_remains_pending():
    def respond(request):
        assert "authorization" not in request.headers
        if request.url.path == "/v1/me":
            return httpx.Response(401)
        return httpx.Response(200, json={"status": "ok"}, headers={"Cache-Control": "no-store"})

    with httpx.Client(
        base_url="https://staging.example", transport=httpx.MockTransport(respond)
    ) as client:
        result = SMOKE["check"](client)
    assert result["status"] == "pending" and result["pending"]
    assert len(result["passed"]) == 3


def test_authenticated_smoke_uses_real_declared_routes_without_mutations():
    def respond(request):
        assert request.method == "GET"
        path = request.url.path
        if path in {"/v1/health", "/v1/ready"}:
            value = {"status": "ok"}
        elif path == "/v1/me":
            if "authorization" not in request.headers:
                return httpx.Response(401)
            value = {"memberships": [{"business_id": "business", "role": "owner"}]}
        else:
            assert request.headers["authorization"] == "Bearer synthetic-token"
            value = {
                "/v1/businesses/business/sources": {"items": [{"id": "source"}]},
                "/v1/businesses/business/sources/source/view": "synthetic source",
                "/v1/businesses/business/needs": {
                    "items": [{"id": "need", "reserved_pool_id": "pool"}]
                },
                "/v1/businesses/business/needs/need/comparisons": {
                    "offers": [{"quote_id": "quote"}]
                },
                "/v1/businesses/business/pools/pool/offer": {"state": "released"},
                "/v1/businesses/business/needs/need/order": {"implementation": "simulated"},
            }[path]
        return httpx.Response(200, json=value, headers={"Cache-Control": "no-store"})

    with httpx.Client(
        base_url="https://staging.example", transport=httpx.MockTransport(respond)
    ) as client:
        result = SMOKE["check"](client, "synthetic-token")
    assert result["status"] == "passed" and not result["pending"]
