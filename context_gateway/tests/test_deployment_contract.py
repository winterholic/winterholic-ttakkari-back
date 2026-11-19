from __future__ import annotations

import asyncio
import re
from pathlib import Path

from context_gateway.apps import create_control_app
from context_gateway.config import Settings


def test_homeserver_proxy_reaches_all_control_routes_and_keeps_public_isolated() -> None:
    config = (
        Path(__file__).parents[2] / "deploy/context-gateway/nginx.homeserver.conf"
    ).read_text()
    public, control = config.split("# Publisher authentication", maxsplit=1)
    assert "172.31.0.2:8788" not in public
    assert "access_log off;" in control
    assert "location / { return 404; }" in control
    locations = re.findall(r"location\s+\^~\s+(\S+)\s*\{([^{}]*)\}", control)
    assert locations, "No explicit control proxy locations found"

    app = create_control_app(
        Settings("postgresql+asyncpg://test:test@127.0.0.1:1/unused", Path("/tmp/cg"), "synthetic", 10)
    )
    try:
        routes = [route.path for route in app.routes if route.path.startswith("/control/")]
        assert routes
        for path in routes:
            matches = [(prefix, body) for prefix, body in locations if path.startswith(prefix)]
            assert matches, f"Control route blocked by homeserver proxy: {path}"
            _, body = max(matches, key=lambda match: len(match[0]))
            assert "proxy_pass http://172.31.0.2:8788;" in body, path
            assert "proxy_set_header Host $host;" in body, path
            assert "proxy_set_header X-Forwarded-Proto https;" in body, path
        assert {prefix for prefix, _ in locations} == {"/control/v1/", "/control/v2/"}
    finally:
        asyncio.run(app.state.session_factory.kw["bind"].dispose())
