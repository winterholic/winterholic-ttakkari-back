"""SEC2-OPS-01: 백업 스크립트와 복원 리허설 테스트.

DB 없이 도는 테스트(기본): 스크립트 배관·manifest 규격·안전장치를
스텁 pg 도구(pg_dump/psql/createdb/dropdb/pg_restore)로 검증한다.
실DB 리허설은 호스트에서만 돈다. TTAKKARI_LIVE_REHEARSAL=1
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BACKUP_SH = REPO / "scripts" / "backup_db.sh"
REHEARSE_SH = REPO / "scripts" / "restore_rehearsal.sh"


def run(cmd, env_extra=None, input_text=None):
    env = dict(os.environ)
    env.pop("TTAKKARI_DATABASE_URL", None)
    env.update(env_extra or {})
    env.setdefault("LC_ALL", "C.UTF-8")
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        input=input_text, env=env, timeout=120)


@pytest.fixture()
def stubs(tmp_path: Path):
    """실제 pg 도구처럼 말하는 최소 스텁 묶음. DB 접속은 하지 않는다."""
    d = tmp_path / "stubin"
    d.mkdir()
    log = tmp_path / "stub.log"
    (d / "pg_dump").write_text(
        "#!/bin/bash\n"
        "while [[ $# -gt 0 ]]; do case \"$1\" in -f) OUT=\"$2\"; shift 2;; *) shift;; esac; done\n"
        "echo \"STUB-DUMP ${STUB_TAG:-x}\" > \"$OUT\"\n")
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
    env = {
        "PATH": f"{d}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_TABLES": "calendar_events\nmemos\n",
        "STUB_COUNTS": "calendar_events=5,memos=3",
        "STUB_TAG": "v1",
    }
    return env, log


def test_rehearsal_fails_without_backup_artifact(tmp_path: Path) -> None:
    """백업 산출물 없이 복원 리허설을 돌리면 DB에 손대기 전에 실패한다(RED)."""
    r = run([str(REHEARSE_SH), "--manifest", str(tmp_path / "없음.manifest.json")])
    assert r.returncode != 0
    assert "백업 산출물 없음" in r.stderr


def test_rehearsal_refuses_non_isolated_target(tmp_path: Path) -> None:
    """운영 DB명으로 보이는 대상에는 복원을 거부한다."""
    manifest = tmp_path / "m.manifest.json"
    manifest.write_text(json.dumps(
        {"created_at": "x", "db": "u", "dump_file": str(tmp_path / "d.dump"),
         "sha256": "0" * 64, "tables": {}}))
    r = run([str(REHEARSE_SH), "--manifest", str(manifest),
             "--target-db", "ttakkari"])
    assert r.returncode != 0
    assert "격리 대상 아님" in r.stderr


def test_backup_then_rehearsal_row_counts_match(tmp_path: Path, stubs) -> None:
    """백업→복원 배관이 manifest 행 수 일치를 보고한다(GREEN)."""
    env, log = stubs
    out = tmp_path / "bk"
    r = run([str(BACKUP_SH), "--dir", str(out),
             "--db-url", "postgresql://localhost/ttakkari"], env)
    assert r.returncode == 0, r.stderr
    manifests = sorted(out.glob("*.manifest.json"))
    assert len(manifests) == 1
    m = json.loads(manifests[0].read_text())
    assert m["tables"] == {"calendar_events": 5, "memos": 3}
    assert m["dump_file"].endswith(".dump")
    assert len(m["sha256"]) == 64

    r = run([str(REHEARSE_SH), "--manifest", str(manifests[0]),
             "--db-url", "postgresql://localhost/ttakkari"], env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MATCH memos: 3행" in r.stdout
    assert "MATCH calendar_events: 5행" in r.stdout
    assert "복원 리허설 통과" in r.stdout
    assert "dropdb" in log.read_text()  # 리허설 DB 정리


def test_rehearsal_detects_row_count_mismatch(tmp_path: Path, stubs) -> None:
    """복원 뒤 행 수가 달라지면 리허설이 실패한다."""
    env, log = stubs
    out = tmp_path / "bk"
    r = run([str(BACKUP_SH), "--dir", str(out),
             "--db-url", "postgresql://localhost/ttakkari"], env)
    assert r.returncode == 0, r.stderr
    manifests = sorted(out.glob("*.manifest.json"))

    env2 = dict(env, STUB_COUNTS="calendar_events=5,memos=99")
    r = run([str(REHEARSE_SH), "--manifest", str(manifests[0]),
             "--db-url", "postgresql://localhost/ttakkari"], env2)
    assert r.returncode != 0
    assert "MISMATCH memos" in r.stdout


@pytest.mark.skipif(os.environ.get("TTAKKARI_LIVE_REHEARSAL") != "1",
                    reason="실DB 리허설은 호스트에서만 실행")
def test_live_rehearsal_against_postgres(tmp_path: Path) -> None:
    """호스트 실DB 경로. 백업(읽기 전용)→격리 DB 복원→행 수 일치."""
    db_url = os.environ.get("TTAKKARI_DATABASE_URL", "postgresql://localhost/ttakkari")
    out = tmp_path / "live"
    r = run([str(BACKUP_SH), "--dir", str(out), "--db-url", db_url])
    assert r.returncode == 0, r.stderr
    manifests = sorted(out.glob("*.manifest.json"))
    assert len(manifests) == 1
    r = run([str(REHEARSE_SH), "--manifest", str(manifests[0]), "--db-url", db_url])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "복원 리허설 통과" in r.stdout
