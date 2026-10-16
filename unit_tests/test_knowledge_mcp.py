"""Exercise tool discovery in the actual stdio entrypoint, not only an import."""

import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_entrypoint_registers_knowledge_tools():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ttakkari.orchestrator.mcp_server"],
        env={**os.environ, "TTK_ROLE": "agent"},
    )
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as client,
    ):
        await client.initialize()
        result = await client.list_tools()
        names = {t.name for t in result.tools}
        assert {
            "knowledge_extraction_status",
            "knowledge_retry_extraction",
            "knowledge_recall",
            "knowledge_assertion",
            "knowledge_start_extraction",
            "knowledge_next_batch",
            "knowledge_submit_batch",
            "knowledge_review_assertion",
        } <= names
        tools = {t.name: t for t in result.tools}
        assert tools["knowledge_recall"].annotations.read_only_hint is True
        assert not (
            tools["knowledge_submit_batch"].annotations
            and tools["knowledge_submit_batch"].annotations.read_only_hint
        )
