from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    snapshot_dir: Path
    control_token: str
    rate_limit_per_minute: int = 120
    initialize_schema: bool = True
    mcp_allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("CONTEXT_GATEWAY_DATABASE_URL", ""),
            snapshot_dir=Path(
                os.environ.get("CONTEXT_GATEWAY_SNAPSHOT_DIR", "/var/lib/context-gateway/snapshots")
            ),
            control_token=os.environ.get("CONTEXT_GATEWAY_CONTROL_TOKEN", ""),
            rate_limit_per_minute=int(os.environ.get("CONTEXT_GATEWAY_RATE_LIMIT", "120")),
            initialize_schema=os.environ.get("CONTEXT_GATEWAY_INITIALIZE_SCHEMA", "true").lower()
            == "true",
            mcp_allowed_hosts=tuple(
                value.strip()
                for value in os.environ.get(
                    "CONTEXT_GATEWAY_MCP_ALLOWED_HOSTS",
                    "localhost,127.0.0.1,localhost:*,127.0.0.1:*",
                ).split(",")
                if value.strip()
            ),
        )
