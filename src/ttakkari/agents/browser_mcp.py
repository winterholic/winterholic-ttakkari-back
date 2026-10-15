"""User-authorized UI testing through the official Playwright stdio MCP.

No shell-command broker, persistent user profile, browser install tool, or unsafe
Node execution tool is exposed. Runtime installation is an operator step.
"""
from __future__ import annotations

import shutil
from pathlib import Path

MCP_CLI = Path(__file__).resolve().parents[3] / ".browser-runtime/node_modules/@playwright/mcp/cli.js"
SERVER_NAME = "ttakkari_browser"
BROWSER_TOOLS = frozenset({
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_take_screenshot",
    "browser_resize", "browser_close", "browser_console_messages", "browser_network_requests",
    "browser_click", "browser_type", "browser_fill_form", "browser_press_key", "browser_select_option",
    "browser_hover", "browser_tabs", "browser_wait_for", "browser_handle_dialog", "browser_emulate_media",
})


def server_spec(outbox: Path) -> dict | None:
    node = shutil.which("node")
    if not node or not MCP_CLI.is_file():
        return None
    return {
        "command": node,
        "args": [str(MCP_CLI), "--headless", "--isolated", "--sandbox", "--browser", "chrome", "--no-webmcp",
                 "--output-dir", str(outbox / "browser"), "--file-paths", "absolute",
                 "--timeout-navigation", "30000", "--idle-timeout", "120000"],
        "cwd": str(outbox),
        "env": {},
    }
