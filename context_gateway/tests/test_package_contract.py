from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from context_gateway.apps import create_control_app, create_public_app
from context_gateway.config import Settings


def test_gateway_source_has_no_private_application_imports() -> None:
    source_root = Path(__file__).parents[1] / "src" / "context_gateway"
    for source in source_root.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(not name.startswith("ttakkari") for name in names), source.name


def test_openapi_is_31_and_control_routes_are_not_mounted_publicly() -> None:
    settings = Settings(
        "postgresql+asyncpg://test:test@127.0.0.1:1/unused", Path("/tmp/cg"), "synthetic", 10
    )
    public = create_public_app(settings)
    control = create_control_app(settings)
    try:
        spec = public.openapi()
        assert spec["openapi"] == "3.1.0"
        assert set(spec["paths"]) >= {
            "/api/v1/auth/exchange",
            "/api/v1/projects",
            "/api/v1/projects/{project_id}",
            "/api/v1/projects/{project_id}/snapshots",
            "/api/v1/projects/{project_id}/context",
            "/api/v1/projects/{project_id}/documents/{doc_id}",
            "/api/v1/projects/{project_id}/search",
            "/api/v1/projects/{project_id}/export",
        }
        assert all("/control/" not in path for path in spec["paths"])
        assert control.openapi_url is None
        assert not hasattr(public.state, "control_app")
    finally:
        asyncio.run(public.state.session_factory.kw["bind"].dispose())
        asyncio.run(control.state.session_factory.kw["bind"].dispose())


def test_python_mcp_sdk_registers_only_four_read_only_tools() -> None:
    from mcp.client import Client

    settings = Settings(
        "postgresql+asyncpg://test:test@127.0.0.1:1/unused", Path("/tmp/cg"), "synthetic", 10
    )
    public = create_public_app(settings)

    async def list_registered_tools():
        async with Client(public.state.mcp_server) as client:
            return await client.list_tools()

    try:
        tools = asyncio.run(list_registered_tools())
        assert {tool.name for tool in tools.tools} == {
            "list_projects",
            "get_project_context",
            "search_project_context",
            "get_document",
        }
        assert all(tool.annotations and tool.annotations.read_only_hint for tool in tools.tools)
    finally:
        asyncio.run(public.state.session_factory.kw["bind"].dispose())
