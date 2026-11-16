from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from context_gateway.apps import create_public_app
from context_gateway.config import Settings
from context_gateway.web_serving import WebServingApp


@pytest.fixture
def web_dist(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!doctype html><title>Gateway UI</title>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app-123.js").write_text("console.log('built');")
    (assets / "app-123.css").write_text("body { color: black; }")
    (assets / "app.js.map").write_text('{"sources":["../../src/main.tsx"]}')
    (tmp_path / ".env").write_text("SECRET=not-for-serving")
    (tmp_path / "main.py").write_text("secret source")
    return tmp_path


@pytest.fixture
async def client(web_dist: Path):
    settings = Settings(
        database_url="postgresql+asyncpg://gateway:gateway@127.0.0.1/gateway",
        snapshot_dir=web_dist,
        control_token="",
        initialize_schema=False,
    )
    app = WebServingApp(create_public_app(settings), web_dist)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_serves_built_index_and_assets_with_security_headers(client: AsyncClient):
    for path, expected in (
        ("/", "Gateway UI"),
        ("/assets/app-123.js", "console.log"),
        ("/assets/app-123.css", "color: black"),
    ):
        response = await client.get(path)
        assert response.status_code == 200
        assert expected in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_api_mcp_and_health_are_forwarded_without_static_fallback(client: AsyncClient):
    api = await client.get("/api/v1/projects")
    mcp = await client.post("/mcp")
    health = await client.get("/healthz")
    assert api.status_code == mcp.status_code == 401
    assert health.json() == {"status": "ok"}
    for response in (api, mcp, health):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
    assert (await client.get("/control/v1/snapshots")).status_code == 404


@pytest.mark.asyncio
async def test_only_built_assets_are_exposed(client: AsyncClient):
    for path in (
        "/assets/app.js.map",
        "/.env",
        "/main.py",
        "/assets/%2e%2e/main.py",
        "/assets/../../main.py",
        "/openapi.json",
        "/docs",
    ):
        assert (await client.get(path)).status_code == 404, path
