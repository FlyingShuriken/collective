from httpx import ASGITransport, AsyncClient

from collective.main import create_app


async def test_health_does_not_require_credentials():
    async with AsyncClient(transport=ASGITransport(create_app()), base_url="http://test") as client:
        response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
