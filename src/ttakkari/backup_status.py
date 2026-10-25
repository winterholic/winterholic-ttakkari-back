"""SEC2-OPS-01: 마지막 백업·스냅샷 성공 상태 조회.

백업 스크립트가 성공할 때마다 BACKUP_DIR/latest.json(DB)과
latest-data.json(데이터 스냅샷)을 원자적으로 갱신한다.
API 응답은 허용 목록 필드(개수·해시·시각)만 내보낸다.
상태 파일에 임의 필드·전체 경로가 섞여 있어도 절대 반환하지 않는다.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DB_STATUS_FILE = "latest.json"
DATA_STATUS_FILE = "latest-data.json"

# API로 내보내도 안전한 필드만 둔다. 경로(manifest·dump_file·archive)와
# 그 외 임의 필드는 여기에 없어서 응답에 나갈 수 없다.
_COMMON_FIELDS: dict[str, type] = {
    "kind": str,
    "created_at": str,
    "sha256": str,
    "encrypted": bool,
}
_DB_FIELDS: dict[str, type] = {
    **_COMMON_FIELDS,
    "key_fingerprint": str,
    "tables": int,
    "rows": int,
}
_DATA_FIELDS: dict[str, type] = {
    **_COMMON_FIELDS,
    "key_fingerprint": str,
    "file_count": int,
    "total_bytes": int,
}


# 키 신원 표시(PBKDF2 파생 값 앞 16자 hex)만 허용한다. 키 내용·경로가 섞인 값은 형식이 달라 버려진다.
_KEY_FINGERPRINT = re.compile(r"[0-9a-f]{16}")


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


def sanitize(section: str, body: dict | None) -> dict | None:
    """허용 목록 필드만 type 검사와 함께 추린다. 경로·임의 필드는 버린다."""
    if not isinstance(body, dict):
        return None
    allow = _DB_FIELDS if section == "db" else _DATA_FIELDS
    clean: dict = {}
    for key, want in allow.items():
        value = body.get(key)
        if not isinstance(value, want):
            continue
        if key == "key_fingerprint" and not _KEY_FINGERPRINT.fullmatch(value):
            continue
        clean[key] = value
    if not clean.get("created_at"):
        return None
    return clean


def backup_status(root: Path | str | None = None) -> dict:
    base = Path(root) if root else backup_dir()
    return {
        "db": sanitize("db", read_status_file(base / DB_STATUS_FILE)),
        "data": sanitize("data", read_status_file(base / DATA_STATUS_FILE)),
    }
