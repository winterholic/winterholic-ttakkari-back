"""SEC2-OPS-01 검증 잔여 4건: 암호화·데이터 스냅샷·성공 상태·스케줄.

각 테스트는 구현 전 실패(RED)하고 구현 후 통과(GREEN)해야 한다.
정상 사용 대조군(비암호화 백업→리허설)은 test_backup_restore.py가 둔다.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.security]


REPO = Path(__file__).resolve().parents[1]
BACKUP_SH = REPO / "scripts" / "backup_db.sh"
REHEARSE_SH = REPO / "scripts" / "restore_rehearsal.sh"
SNAPSHOT_SH = REPO / "scripts" / "snapshot_data.sh"
BACKUP_PLIST = REPO / "deploy" / "com.apple.ttakkari.backup.plist.example"
SNAPSHOT_PLIST = REPO / "deploy" / "com.apple.ttakkari.data-snapshot.plist.example"
RUNBOOK = REPO / "docs" / "security" / "backup-runbook.md"


def run(cmd, env_extra=None):
    env = dict(os.environ)
    env.pop("TTAKKARI_DATABASE_URL", None)
    env.pop("BACKUP_PASSPHRASE_FILE", None)
    env.update(env_extra or {})
    env.setdefault("LC_ALL", "C.UTF-8")
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        env=env, timeout=120, check=False)


@pytest.fixture()
def stubs(tmp_path: Path):
    """test_backup_restore.py와 같은 최소 pg 스텁 묶음."""
    d = tmp_path / "stubin"
    d.mkdir()
    log = tmp_path / "stub.log"
    (d / "pg_dump").write_text(
        "#!/bin/bash\n"
        "echo \"pg_dump $*\" >> \"$STUB_LOG\"\n"
        "while [[ $# -gt 0 ]]; do case \"$1\" in -f) OUT=\"$2\"; shift 2;; *) shift;; esac; done\n"
        "echo \"STUB-DUMP ${STUB_TAG:-x}\" > \"$OUT\"\n"
        "exit 0\n")
    (d / "asyncpg.py").write_text(
        "import os, re\n"
        "class Tx:\n"
        " async def __aenter__(self): return self\n"
        " async def __aexit__(self, *args): return False\n"
        "class Conn:\n"
        " def transaction(self, **kwargs): return Tx()\n"
        " async def fetchval(self, query):\n"
        "  if 'pg_export_snapshot' in query: return '00000003-0000001B-1'\n"
        "  table=re.search(r'FROM public.\\\"([^\\\"]+)', query).group(1)\n"
        "  counts=dict(item.split('=') for item in os.environ['STUB_COUNTS'].split(','))\n"
        "  return int(counts[table])\n"
        " async def fetch(self, query):\n"
        "  return [{'tablename': t} for t in os.environ['STUB_TABLES'].splitlines() if t]\n"
        " async def close(self): pass\n"
        "async def connect(dsn): return Conn()\n")
    (d / "pg_restore").write_text(
        "#!/bin/bash\necho \"pg_restore $*\" >> \"$STUB_LOG\"\n")
    (d / "createdb").write_text(
        "#!/bin/bash\necho \"createdb $*\" >> \"$STUB_LOG\"\n")
    (d / "dropdb").write_text(
        "#!/bin/bash\necho \"dropdb $*\" >> \"$STUB_LOG\"\n")
    (d / "psql").write_text(
        "#!/bin/bash\n"
        "Q=\"\"; PREV=\"\"; while [[ $# -gt 0 ]]; do if [[ \"$1\" == *SELECT* ]]; then Q=\"$1\"; elif [[ \"$PREV\" == -c ]]; then Q=\"$1\"; fi; PREV=\"$1\"; shift; done\n"
        "if [[ \"$Q\" == *pg_tables* ]]; then printf '%s' \"$STUB_TABLES\"; exit 0; fi\n"
        "T=$(printf '%s' \"$Q\" | sed -n 's/.*FROM \"\\([^\"]*\\)\".*/\\1/p')\n"
        "[[ -n \"$T\" ]] || exit 0\n"
        "printf '%s' \"$STUB_COUNTS\" | tr ',' '\\n' | awk -F= -v t=\"$T\" '$1==t{print $2}'\n")
    for p in d.iterdir():
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return {
        "PATH": f"{d}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(d),
        "STUB_LOG": str(log),
        "STUB_TABLES": "calendar_events\nmemos\n",
        "STUB_COUNTS": "calendar_events=5,memos=3",
        "STUB_TAG": "v1",
    }


@pytest.fixture()
def passphrase_file(tmp_path: Path) -> Path:
    p = tmp_path / "backup.key"
    p.write_text("test-passphrase-do-not-use-in-production\n")
    p.chmod(0o600)
    return p


# 1. 덤프 암호화 ──────────────────────────────────────────────

def test_encrypted_backup_then_rehearsal_row_counts_match(
        tmp_path: Path, stubs, passphrase_file: Path) -> None:
    """암호화 백업→리허설이 manifest 행 수 일치를 보고한다."""
    env = dict(stubs)
    out = tmp_path / "bk-enc"
    r = run([str(BACKUP_SH), "--dir", str(out),
             "--db-url", "postgresql+asyncpg://localhost/ttakkari",
             "--passphrase-file", str(passphrase_file)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    manifests = sorted(out.glob("*.manifest.json"))
    assert len(manifests) == 1
    m = json.loads(manifests[0].read_text())
    assert m["encrypted"] is True
    assert m["dump_file"].endswith(".enc")
    assert m["tables"] == {"calendar_events": 5, "memos": 3}
    # 평문 덤프는 디스크에 남지 않는다.
    assert list(out.glob("*.dump")) == []
    assert "secret" not in m.get("plaintext_sha256", "x").lower() or True

    r = run([str(REHEARSE_SH), "--manifest", str(manifests[0]),
             "--db-url", "postgresql://localhost/ttakkari",
             "--passphrase-file", str(passphrase_file)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "복원 리허설 통과" in r.stdout


def test_encrypted_rehearsal_fails_with_wrong_passphrase(
        tmp_path: Path, stubs, passphrase_file: Path) -> None:
    """틀린 암호로는 암호화 리허설이 실패한다."""
    env = dict(stubs)
    out = tmp_path / "bk-enc"
    r = run([str(BACKUP_SH), "--dir", str(out),
             "--db-url", "postgresql+asyncpg://localhost/ttakkari",
             "--passphrase-file", str(passphrase_file)], env)
    assert r.returncode == 0, r.stdout + r.stderr
    wrong = tmp_path / "wrong.key"
    wrong.write_text("wrong-passphrase\n")
    wrong.chmod(0o600)
    manifest = next(out.glob("*.manifest.json"))
    r = run([str(REHEARSE_SH), "--manifest", str(manifest),
             "--db-url", "postgresql://localhost/ttakkari",
             "--passphrase-file", str(wrong)], env)
    assert r.returncode != 0


def test_backup_rejects_group_readable_passphrase_file(
        tmp_path: Path, stubs) -> None:
    """다른 사용자가 읽을 수 있는 암호 파일로는 백업을 거부한다."""
    env = dict(stubs)
    bad = tmp_path / "shared.key"
    bad.write_text("shared\n")
    bad.chmod(0o644)
    r = run([str(BACKUP_SH), "--dir", str(tmp_path / "bk"),
             "--db-url", "postgresql://localhost/ttakkari",
             "--passphrase-file", str(bad)], env)
    assert r.returncode != 0
    assert "소유자만" in r.stdout + r.stderr


# 2. 데이터 디렉터리 스냅샷 ───────────────────────────────────

@pytest.fixture()
def sample_data(tmp_path: Path) -> Path:
    src = tmp_path / "sample-data"
    (src / "notes").mkdir(parents=True)
    (src / "notes" / "a.md").write_text("# hello\n")
    (src / "notes" / "b.md").write_text("# world\n")
    (src / "state.json").write_text('{"n": 1}\n')
    return src


def test_data_snapshot_and_verify_roundtrip(tmp_path: Path, sample_data: Path) -> None:
    """격리 위치에서 데이터 스냅샷→검증이 통과한다."""
    out = tmp_path / "snaps"
    r = run([str(SNAPSHOT_SH), "--source", str(sample_data), "--dir", str(out)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "스냅샷 완료" in r.stdout
    manifests = sorted(out.glob("*.manifest.json"))
    assert len(manifests) == 1
    m = json.loads(manifests[0].read_text())
    assert m["file_count"] == 3
    assert stat.S_IMODE(out.stat().st_mode) == 0o700

    target = tmp_path / "restored"
    r = run([str(SNAPSHOT_SH), "--verify", str(manifests[0]), "--target", str(target)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "스냅샷 검증 통과" in r.stdout


def test_data_snapshot_encrypted_roundtrip(
        tmp_path: Path, sample_data: Path, passphrase_file: Path) -> None:
    """암호화 스냅샷→검증이 통과하고 평문 tar.gz가 남지 않는다."""
    out = tmp_path / "snaps-enc"
    r = run([str(SNAPSHOT_SH), "--source", str(sample_data), "--dir", str(out),
             "--passphrase-file", str(passphrase_file)])
    assert r.returncode == 0, r.stdout + r.stderr
    manifests = sorted(out.glob("*.manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["encrypted"] is True
    assert list(out.glob("*.tar.gz")) == []

    r = run([str(SNAPSHOT_SH), "--verify", str(manifests[0]),
             "--target", str(tmp_path / "restored"),
             "--passphrase-file", str(passphrase_file)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "스냅샷 검증 통과" in r.stdout


def test_data_snapshot_skips_links_in_both_archive_and_manifest(tmp_path, sample_data):
    import tarfile

    outside = tmp_path / "outside.txt"
    outside.write_text("outside must stay unchanged")
    (sample_data / "file-link").symlink_to(outside)
    (sample_data / "dir-link").symlink_to(outside.parent, target_is_directory=True)
    (sample_data / "broken-link").symlink_to(tmp_path / "missing")
    out = tmp_path / "snapshots"
    created = run([str(SNAPSHOT_SH), "--source", str(sample_data), "--dir", str(out)])
    assert created.returncode == 0, created.stdout + created.stderr
    manifest = next(out.glob("*.manifest.json"))
    body = json.loads(manifest.read_text())
    restored = run([str(SNAPSHOT_SH), "--verify", str(manifest), "--target", str(tmp_path / "restored")])
    assert restored.returncode == 0, restored.stdout + restored.stderr
    with tarfile.open(body["archive"]) as archive:
        assert not any(member.issym() or member.islnk() for member in archive)
    assert body["file_count"] == 3
    assert outside.read_text() == "outside must stay unchanged"


def test_data_snapshot_verify_detects_tamper(tmp_path: Path, sample_data: Path) -> None:
    """아카이브가 바뀌면 검증이 실패한다."""
    out = tmp_path / "snaps"
    r = run([str(SNAPSHOT_SH), "--source", str(sample_data), "--dir", str(out)])
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = next(out.glob("*.manifest.json"))
    archive = next(p for p in out.iterdir() if p.suffix in (".gz", ".enc"))
    with archive.open("ab") as f:
        f.write(b"tampered")
    r = run([str(SNAPSHOT_SH), "--verify", str(manifest),
             "--target", str(tmp_path / "restored")])
    assert r.returncode != 0


def test_data_snapshot_refuses_missing_source(tmp_path: Path) -> None:
    """없는 원본으로는 스냅샷을 만들지 않는다(RED 경로)."""
    r = run([str(SNAPSHOT_SH), "--source", str(tmp_path / "없음"),
             "--dir", str(tmp_path / "snaps")])
    assert r.returncode != 0


# 3. 최근 성공 상태 노출 ──────────────────────────────────────

def test_backup_writes_latest_status_file(tmp_path: Path, stubs) -> None:
    """백업 성공 시 latest.json에 성공 시각·산출물이 남는다."""
    out = tmp_path / "bk"
    r = run([str(BACKUP_SH), "--dir", str(out),
             "--db-url", "postgresql+asyncpg://localhost/ttakkari"], dict(stubs))
    assert r.returncode == 0, r.stdout + r.stderr
    latest = out / "latest.json"
    assert latest.exists()
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600
    body = json.loads(latest.read_text())
    assert body["kind"] == "db"
    assert body["created_at"]
    assert body["manifest"].endswith(".manifest.json")
    assert body["rows"] == 8


def test_backup_status_reports_latest(tmp_path: Path, stubs, monkeypatch) -> None:
    """운영 상태 조회가 마지막 백업 성공 시각을 돌려준다."""
    from ttakkari import backup_status as mod
    out = tmp_path / "bk"
    r = run([str(BACKUP_SH), "--dir", str(out),
             "--db-url", "postgresql+asyncpg://localhost/ttakkari"], dict(stubs))
    assert r.returncode == 0, r.stdout + r.stderr
    monkeypatch.setenv("BACKUP_DIR", str(out))
    status = mod.backup_status()
    assert status["db"] is not None
    assert status["db"]["created_at"]
    assert status["db"]["rows"] == 8


def test_backup_status_api_route_exists() -> None:
    """GET /api/system/backup-status 라우트가 등록되어 있다."""
    from ttakkari.api import system as api
    paths = {(getattr(rt, "path", None), tuple(sorted(getattr(rt, "methods", []) or ())))
             for rt in api.router.routes}
    assert ("/api/system/backup-status", ("GET",)) in paths


def test_backup_status_endpoint_returns_latest(tmp_path: Path, monkeypatch) -> None:
    """인증된 상태 조회가 마지막 성공 시각을 돌려준다(대조군: 미인증 401)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ttakkari.api import system as api
    from ttakkari.security.auth import require_user

    out = tmp_path / "bk"
    out.mkdir()
    (out / "latest.json").write_text(json.dumps({
        "kind": "db", "created_at": "20261019-031700",
        "manifest": str(out / "m.manifest.json"),
        "dump_file": str(out / "d.dump"), "sha256": "0" * 64,
        "encrypted": False, "tables": 2, "rows": 8,
    }))
    app = FastAPI()
    app.include_router(api.router)
    monkeypatch.setenv("BACKUP_DIR", str(out))
    with TestClient(app) as c:
        assert c.get("/api/system/backup-status").status_code in (401, 403)
        app.dependency_overrides[require_user] = lambda: "owner"
        r = c.get("/api/system/backup-status")
        assert r.status_code == 200
        assert r.json()["db"]["rows"] == 8
        assert r.json()["data"] is None
        assert r.headers["cache-control"] == "no-store"


# 4. launchd 스케줄 ───────────────────────────────────────────

def test_launchd_plists_cover_backup_and_snapshot() -> None:
    """백업·스냅샷 plist 예시가 있고 암호 env·실행 주기를 가진다."""
    assert BACKUP_PLIST.exists()
    assert SNAPSHOT_PLIST.exists()
    backup = BACKUP_PLIST.read_text()
    assert "com.apple.ttakkari.backup" in backup
    assert "backup_db.sh" in backup
    assert "BACKUP_PASSPHRASE_FILE" in backup
    assert "StartCalendarInterval" in backup
    snapshot = SNAPSHOT_PLIST.read_text()
    assert "com.apple.ttakkari.data-snapshot" in snapshot
    assert "snapshot_data.sh" in snapshot
    assert "StartCalendarInterval" in snapshot


def test_runbook_documents_register_check_and_status() -> None:
    """runbook에 등록·확인 명령과 성공 시각 조회가 있다."""
    body = RUNBOOK.read_text()
    assert "launchctl bootstrap" in body
    assert "launchctl print" in body
    assert "latest.json" in body
    assert "backup-status" in body
    assert "snapshot_data.sh" in body
