#!/bin/bash
# SEC2-OPS-01: 따까리 DB 백업 스크립트.
#
# 하는 일: pg_dump(-Fc, 일관 스냅샷, 서비스 무중단)로 덤프를 뜨고,
#   public 스키마 테이블별 행 수를 센 manifest(JSON)를 함께 보관한다.
#   복원 리허설(scripts/restore_rehearsal.sh)이 이 manifest와 행 수를 대조한다.
#
# 운영 DB에는 쓰지 않는다. pg_dump와 SELECT count(*)는 읽기 전용이다.
# 백업 위치 기본값은 에이전트 쓰기 루트(~/development, ~/.claude, ~/.codex 등) 밖이다.
# 같은 macOS 사용자라는 한계(위협 모델 6.1절)는 Time Machine 오프머신 복구로 보완한다.
#
# 사용법:
#   scripts/backup_db.sh [--db-url URL] [--dir DIR] [--retention-days N]
# 환경 변수: TTAKKARI_DATABASE_URL, BACKUP_DIR, RETENTION_DAYS
#
# 성공 시 manifest 경로를 stdout 마지막 줄에 출력한다.
# 최근 성공 시각 조회: ls -t "$BACKUP_DIR"/*.manifest.json | head -1
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/../.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "프로젝트 Python 환경 없음: $PYTHON" >&2; exit 3; }

DB_URL="${TTAKKARI_DATABASE_URL:-}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.ttakkari-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db-url) DB_URL="$2"; shift 2 ;;
    --dir) BACKUP_DIR="$2"; shift 2 ;;
    --retention-days) RETENTION_DAYS="$2"; shift 2 ;;
    *) echo "사용법: $0 [--db-url URL] [--dir DIR] [--retention-days N]" >&2; exit 2 ;;
  esac
done
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || { echo "보존 기간은 0 이상의 정수여야 한다." >&2; exit 2; }

if [[ -z "$DB_URL" ]]; then
  DB_URL="$(PYTHONPATH="$SCRIPT_DIR/../src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -c 'from ttakkari.config import Settings; print(Settings().database_url)')"
fi

# SQLAlchemy식 URL(+asyncpg 등)은 libpq 도구가 못 읽으므로 driver 부분을 걷어낸다.
LIBPQ_URL="$(printf '%s' "$DB_URL" | sed -E 's#^([a-zA-Z][a-zA-Z0-9.-]*)(\+[a-zA-Z0-9_.-]+)?://#\1://#')"

command -v pg_dump >/dev/null || { echo "pg_dump 없음. PostgreSQL 클라이언트를 설치한다." >&2; exit 3; }
command -v psql >/dev/null || { echo "psql 없음. PostgreSQL 클라이언트를 설치한다." >&2; exit 3; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)-$$"
DUMP="$BACKUP_DIR/ttakkari-$TS.dump"
MANIFEST="$BACKUP_DIR/ttakkari-$TS.manifest.json"
DUMP_TMP="$DUMP.partial.$$"
MANIFEST_TMP="$MANIFEST.partial.$$"
[[ ! -e "$DUMP" && ! -e "$MANIFEST" ]] || { echo "동일한 백업 이름이 이미 있다." >&2; exit 1; }
cleanup_partial() { rm -f "$DUMP_TMP" "$MANIFEST_TMP"; }
trap cleanup_partial EXIT

"$PYTHON" - "$LIBPQ_URL" "$DUMP_TMP" "$DUMP" "$MANIFEST_TMP" "$TS" <<'EOF'
import asyncio
import hashlib
import json
import subprocess
import sys

import asyncpg

db_url, dump_tmp, dump_path, manifest_tmp, timestamp = sys.argv[1:]


async def backup():
    conn = await asyncpg.connect(dsn=db_url)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            snapshot = await conn.fetchval("SELECT pg_export_snapshot()")
            names = await conn.fetch(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' ORDER BY 1"
            )
            counts = {}
            for row in names:
                table = row["tablename"]
                quoted = '"' + table.replace('"', '""') + '"'
                counts[table] = await conn.fetchval(
                    f"SELECT count(*) FROM public.{quoted}"
                )
            result = subprocess.run(
                ["pg_dump", "-Fc", "--snapshot", snapshot, "-f", dump_tmp, db_url],
                capture_output=True, text=True,
            )
            if result.returncode:
                sys.stderr.write(result.stderr)
                raise SystemExit(result.returncode)
        return counts
    finally:
        await conn.close()


counts = asyncio.run(backup())
with open(dump_tmp, "rb") as f:
    digest = hashlib.file_digest(f, "sha256").hexdigest()
with open(manifest_tmp, "w", encoding="utf-8") as f:
    json.dump(
        {"created_at": timestamp, "dump_file": dump_path,
         "sha256": digest, "tables": counts},
        f, indent=2, sort_keys=True,
    )
    f.write("\n")
print(f"테이블 {len(counts)}개, 총 {sum(counts.values())}행 기록")
EOF
mv "$DUMP_TMP" "$DUMP"
mv "$MANIFEST_TMP" "$MANIFEST"

# 보존 기간이 지난 백업을 정리한다.
if ! find "$BACKUP_DIR" -maxdepth 1 -name 'ttakkari-*.dump' -mtime +"$RETENTION_DAYS" -delete; then
  echo "경고: 덤프 보존 기간 정리 실패" >&2
fi
if ! find "$BACKUP_DIR" -maxdepth 1 -name 'ttakkari-*.manifest.json' -mtime +"$RETENTION_DAYS" -delete; then
  echo "경고: manifest 보존 기간 정리 실패" >&2
fi

echo "백업 완료: $MANIFEST"
