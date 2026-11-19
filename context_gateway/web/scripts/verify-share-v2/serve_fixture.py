"""Run synthetic Context Gateway v2 control/public apps on an isolated local DB."""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.responses import PlainTextResponse, RedirectResponse

ROOT = Path(__file__).resolve().parents[4]
WEB_BUILD = Path(os.environ["CG_V2_PUBLIC_WEB_DIR"]).resolve(strict=True)
DB_NAME = "test_cg_v2_public_verify_20261119"
DB_URL = f"postgresql+asyncpg://{getpass.getuser()}@127.0.0.1:5432/{DB_NAME}"
CONTROL_TOKEN = "synthetic-control-only-context-sharing-v2-20261119"
ORIGIN = "http://127.0.0.1:8747"
FIXTURE_ALIASES = {"primary", "withdraw-link", "withdraw-project", "expired"}


def snapshot(slug: str) -> dict[str, Any]:
    return {
        "project_slug": slug,
        "project_name": f"합성 검수 {slug}",
        "project_description": "공개 Reader 독립 검수 전용 합성 프로젝트입니다.",
        "source_refs": [f"fixture://{slug}/approved-source"],
        "source_revision": "synthetic-revision-20261119",
        "generated_at": "2026-11-19T01:00:00Z",
        "verified_at": "2026-11-19T01:05:00Z",
        "documents": [
            {
                "id": "overview",
                "title": "합성 승인 문서",
                "content": (
                    "# 승인된 합성 문서\n\n"
                    "합성 출처와 시각을 확인하는 본문입니다.\n\n"
                    "<script>window.syntheticXss = true</script>\n\n"
                    "![외부 추적 이미지](https://tracker.invalid/pixel.png)\n\n"
                    "<img src=\"https://tracker.invalid/html.png\" onerror=\"alert(1)\">"
                ),
            }
        ],
    }


class FixtureGateway:
    def __init__(self, control, web, tokens: dict[str, str]):
        self.control = control
        self.web = web
        self.tokens = tokens
        self.stack = AsyncExitStack()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await self.stack.enter_async_context(self.control.router.lifespan_context(self.control))
                    await self.stack.enter_async_context(self.web.public_app.router.lifespan_context(self.web.public_app))
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await self.stack.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        path = scope.get("path", "")
        if path.startswith("/control/"):
            await self.control(scope, receive, send)
        elif path.startswith("/__fixture-entry/"):
            alias = path.removeprefix("/__fixture-entry/")
            token = self.tokens.get(alias) if alias in FIXTURE_ALIASES else None
            response = (
                RedirectResponse(f"/s#token={token}", status_code=303)
                if token
                else PlainTextResponse("Not Found", status_code=404)
            )
            await response(scope, receive, send)
        else:
            await self.web(scope, receive, send)


async def provision(client: httpx.AsyncClient) -> tuple[dict[str, str], dict[str, str]]:
    headers = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    tokens: dict[str, str] = {}
    metadata: dict[str, str] = {}
    projects: dict[str, str] = {}
    for slug in ("public-reader", "withdraw-link", "withdraw-project"):
        published = await client.post("/control/v1/snapshots", headers=headers, json=snapshot(slug))
        if published.status_code != 201:
            raise RuntimeError(f"synthetic publish failed with HTTP {published.status_code}")
        projects[slug] = published.json()["project_id"]
        snapshot_id = published.json()["id"]
        issued = await client.post(
            "/control/v2/credentials",
            headers=headers,
            json={
                "credential_type": "share_link_v2",
                "label": f"synthetic {slug}",
                "ttl_days": 3,
                "projects": {
                    projects[slug]: {
                        "scopes": ["read", "export"] if slug == "public-reader" else ["read"],
                        "snapshot_policy": "pinned",
                        "snapshot_id": snapshot_id,
                    }
                },
            },
        )
        if issued.status_code != 201:
            raise RuntimeError(f"synthetic credential issue failed with HTTP {issued.status_code}")
        tokens["primary" if slug == "public-reader" else slug] = issued.json()["key"]
        if slug == "withdraw-link":
            metadata["withdraw_link_id"] = issued.json()["key_id"]
        if slug == "withdraw-project":
            metadata["expired_link_id"] = issued.json()["key_id"]
            metadata["withdraw_project_id"] = projects[slug]

    # An already expired fixture exercises the same public exchange/context guard without waiting.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DB_URL)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE gateway_api_keys SET expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": metadata["expired_link_id"]},
        )
    await engine.dispose()
    tokens["expired"] = tokens.pop("withdraw-project")
    return tokens, metadata


async def main() -> None:
    sys.path.insert(0, str(ROOT / "context_gateway" / "src"))
    from context_gateway.apps import create_control_app, create_public_app
    from context_gateway.config import Settings
    from context_gateway.web_serving import WebServingApp

    settings = Settings(
        database_url=DB_URL,
        snapshot_dir=Path("/private/tmp/context-gateway-share-v2-public-verify-20261119/snapshots"),
        control_token=CONTROL_TOKEN,
        rate_limit_per_minute=200,
        initialize_schema=True,
    )
    control = create_control_app(settings)
    public = create_public_app(settings)
    tokens: dict[str, str] = {}
    app = FixtureGateway(control, WebServingApp(public, WEB_BUILD), tokens)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8747, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if server_task.done():
                await server_task
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient(base_url=ORIGIN, timeout=10) as client:
            health = await client.get("/healthz")
            if health.status_code != 200:
                raise RuntimeError(f"local Gateway health failed with HTTP {health.status_code}")
            seeded_tokens, metadata = await provision(client)
            tokens.update(seeded_tokens)
        print(
            f"READY origin={ORIGIN} database={DB_NAME} cases=primary,withdraw-link,expired "
            f"withdraw_link_id={metadata['withdraw_link_id']} "
            f"withdraw_project_id={metadata['withdraw_project_id']}",
            flush=True,
        )
        await server_task
    finally:
        server.should_exit = True
        await server_task


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
