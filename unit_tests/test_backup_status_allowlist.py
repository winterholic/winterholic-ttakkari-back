"""SEC2-OPS-01 비밀 노출 재현: 상태 JSON의 임의 필드·전체 경로는 API에 나가면 안 된다.

수정 전 RED (응답에 비밀·전체 경로가 그대로 포함됨),
수정 후 GREEN (허용 목록 필드만 반환) 이어야 한다.
"""
import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.security]

FAKE_SECRETS = {
    "database_url": "postgresql://owner:s3cr3t-pw1234@db.internal:5432/ttakkari",
    "password": "s3cr3t-pw1234",
    "s3_secret_key": "AKIAFAKEFAKEFAKEFAKE",
    "backup_url": "https://objects.example.com/bucket/ttakkari?token=fake-token-abc",
}


def _poisoned_db(out: Path) -> dict:
    return {
        "kind": "db",
        "created_at": "20261019-031700",
        "manifest": str(out / "m.manifest.json"),
        "dump_file": str(out / "d.dump"),
        "sha256": "a" * 64,
        "encrypted": False,
        "tables": 2,
        "rows": 8,
        **FAKE_SECRETS,
    }


def _poisoned_data(out: Path) -> dict:
    return {
        "kind": "data",
        "created_at": "20261019-031700",
        "manifest": str(out / "d.manifest.json"),
        "archive": str(out / "data.tar.gz"),
        "sha256": "b" * 64,
        "encrypted": False,
        "file_count": 3,
        "total_bytes": 123,
        **FAKE_SECRETS,
    }


def _assert_no_leak(payload: dict) -> None:
    raw = json.dumps(payload)
    for name, secret in FAKE_SECRETS.items():
        assert name not in payload.get("db", {}) | payload.get("data", {}), name
        assert secret not in raw, name
    for section in ("db", "data"):
        body = payload.get(section)
        if body is None:
            continue
        for key in ("manifest", "dump_file", "archive"):
            assert key not in body, key
        for value in body.values():
            if isinstance(value, str) and "/" in value and " " not in value:
                assert not value.startswith("/"), f"absolute path leaked: {value!r}"


def test_backup_status_hides_secrets_and_paths(tmp_path: Path, monkeypatch) -> None:
    from ttakkari import backup_status as mod

    out = tmp_path / "bk"
    out.mkdir()
    (out / "latest.json").write_text(json.dumps(_poisoned_db(out)))
    (out / "latest-data.json").write_text(json.dumps(_poisoned_data(out)))
    monkeypatch.setenv("BACKUP_DIR", str(out))
    payload = mod.backup_status()
    assert payload["db"]["rows"] == 8
    assert payload["data"]["file_count"] == 3
    _assert_no_leak(payload)


def test_backup_status_endpoint_hides_secrets_and_paths(tmp_path: Path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ttakkari.api import system as api
    from ttakkari.security.auth import require_user

    out = tmp_path / "bk"
    out.mkdir()
    (out / "latest.json").write_text(json.dumps(_poisoned_db(out)))
    (out / "latest-data.json").write_text(json.dumps(_poisoned_data(out)))
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setenv("BACKUP_DIR", str(out))
    app.dependency_overrides[require_user] = lambda: "owner"
    with TestClient(app) as c:
        r = c.get("/api/system/backup-status")
        assert r.status_code == 200
        assert r.json()["db"]["rows"] == 8
        _assert_no_leak(r.json())
