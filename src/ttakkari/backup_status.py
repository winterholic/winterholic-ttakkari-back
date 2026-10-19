"""SEC2-OPS-01: 마지막 백업·스냅샷 성공 상태 조회.

백업 스크립트가 성공할 때마다 BACKUP_DIR/latest.json(DB)과
latest-data.json(데이터 스냅샷)을 원자적으로 갱신한다.
여기에는 비밀·토큰이 없다. 개수·해시·경로만 있다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DB_STATUS_FILE = "latest.json"
DATA_STATUS_FILE = "latest-data.json"


def backup_dir() -> Path:
    override = os.environ.get("BACKUP_DIR")
    if override:
        return Path(override)
    return Path.home() / ".ttakkari-backups"


def read_status_file(path: Path) -> dict | None:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or not body.get("created_at"):
        return None
    return body


def backup_status(root: Path | str | None = None) -> dict:
    base = Path(root) if root else backup_dir()
    return {
        "db": read_status_file(base / DB_STATUS_FILE),
        "data": read_status_file(base / DATA_STATUS_FILE),
    }
