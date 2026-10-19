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
#                        [--passphrase-file FILE]
# 환경 변수: TTAKKARI_DATABASE_URL, BACKUP_DIR, RETENTION_DAYS,
#   BACKUP_PASSPHRASE_FILE(덤프 암호화용. 0600 암호 파일. 비우면 평문 보관)
#
# 암호화 방식: openssl enc -aes-256-cbc -pbkdf2(20만 회, 저장소 관례.
#   age 미설치 환경에서도 도는 표준 도구). 암호화하면 평문 덤프는
#   디스크에서 지우고 manifest는 암호문을 가리킨다.
#   복원은 scripts/restore_rehearsal.sh가 같은 암호 파일로 복호화한다.
#
# 성공 시 manifest 경로를 stdout 마지막 줄에 출력한다.
# 최근 성공 시각 조회: cat "$BACKUP_DIR"/latest.json
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/../.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "프로젝트 Python 환경 없음: $PYTHON" >&2; exit 3; }

DB_URL="${TTAKKARI_DATABASE_URL:-}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/.ttakkari-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db-url) DB_URL="$2"; shift 2 ;;
    --dir) BACKUP_DIR="$2"; shift 2 ;;
    --retention-days) RETENTION_DAYS="$2"; shift 2 ;;
    --passphrase-file) PASSPHRASE_FILE="$2"; shift 2 ;;
    *) echo "사용법: $0 [--db-url URL] [--dir DIR] [--retention-days N] [--passphrase-file FILE]" >&2; exit 2 ;;
  esac
done
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || { echo "보존 기간은 0 이상의 정수여야 한다." >&2; exit 2; }

# 암호 파일은 소유자만 읽을 수 있어야 한다. 없으면 평문 보관(기존 동작).
check_passphrase_file() {
  [[ -f "$1" ]] || { echo "오류: 암호 파일 없음: $1" >&2; exit 2; }
  local mode
  if mode="$(stat -f %Lp "$1" 2>/dev/null)"; then :;
  elif mode="$(stat -c %a "$1" 2>/dev/null)"; then :;
  else echo "오류: 암호 파일 권한을 확인할 수 없음: $1" >&2; exit 2; fi
  if (( (8#$mode & 077) != 0 )); then
    echo "오류: 암호 파일은 소유자만 읽을 수 있어야 한다(0600): $1" >&2; exit 2;
  fi
  [[ -s "$1" ]] || { echo "오류: 암호 파일이 비어 있다: $1" >&2; exit 2; }
}

secure_delete() {
  if command -v shred >/dev/null 2>&1; then shred -u -- "$1";
  elif rm -P "$1" 2>/dev/null; then :;
  else rm -f -- "$1"; fi
}

ENCRYPT=0
if [[ -n "$PASSPHRASE_FILE" ]]; then
  check_passphrase_file "$PASSPHRASE_FILE"
  command -v openssl >/dev/null || { echo "openssl 없음. 암호화 백업을 만들 수 없다." >&2; exit 3; }
  ENCRYPT=1
fi

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
cleanup_partial() { rm -f "$DUMP_TMP" "$MANIFEST_TMP" "${ENC_TMP:-}"; }
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
         "sha256": digest, "tables": counts, "encrypted": False},
        f, indent=2, sort_keys=True,
    )
    f.write("\n")
print(f"테이블 {len(counts)}개, 총 {sum(counts.values())}행 기록")
EOF
mv "$DUMP_TMP" "$DUMP"
mv "$MANIFEST_TMP" "$MANIFEST"

# 암호화하면 평문 덤프를 지우고 manifest가 암호문을 가리키게 한다.
if [[ "$ENCRYPT" -eq 1 ]]; then
  ENC="$DUMP.enc"
  ENC_TMP="$ENC.partial.$$"
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in "$DUMP" -out "$ENC_TMP" -pass "file:$PASSPHRASE_FILE"
  "$PYTHON" - "$MANIFEST" "$ENC_TMP" "$ENC" <<'EOF'
import hashlib
import json
import sys

manifest_path, enc_tmp, enc_path = sys.argv[1:]
with open(enc_tmp, "rb") as f:
    enc_digest = hashlib.file_digest(f, "sha256").hexdigest()
with open(manifest_path, encoding="utf-8") as f:
    m = json.load(f)
m["plaintext_sha256"] = m["sha256"]
m["sha256"] = enc_digest
m["dump_file"] = enc_path
m["encrypted"] = True
with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(m, f, indent=2, sort_keys=True)
    f.write("\n")
EOF
  mv "$ENC_TMP" "$ENC"
  secure_delete "$DUMP"
  echo "덤프 암호화 완료: $ENC"
fi

# 최근 성공 상태를 원자적으로 갱신한다(API·runbook이 읽는다).
"$PYTHON" - "$MANIFEST" "$BACKUP_DIR/latest.json" <<'EOF'
import json
import sys

manifest_path, latest_path = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as f:
    m = json.load(f)
latest = {
    "kind": "db",
    "created_at": m["created_at"],
    "manifest": manifest_path,
    "dump_file": m["dump_file"],
    "sha256": m["sha256"],
    "encrypted": bool(m.get("encrypted", False)),
    "tables": len(m["tables"]),
    "rows": sum(m["tables"].values()),
}
tmp = latest_path + ".partial"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(latest, f, indent=2, sort_keys=True)
    f.write("\n")
import os
os.replace(tmp, latest_path)
EOF

# 보존 기간이 지난 백업을 정리한다.
if ! find "$BACKUP_DIR" -maxdepth 1 -name 'ttakkari-*.dump' -mtime +"$RETENTION_DAYS" -delete; then
  echo "경고: 덤프 보존 기간 정리 실패" >&2
fi
if ! find "$BACKUP_DIR" -maxdepth 1 -name 'ttakkari-*.dump.enc' -mtime +"$RETENTION_DAYS" -delete; then
  echo "경고: 암호화 덤프 보존 기간 정리 실패" >&2
fi
if ! find "$BACKUP_DIR" -maxdepth 1 -name 'ttakkari-*.manifest.json' -mtime +"$RETENTION_DAYS" -delete; then
  echo "경고: manifest 보존 기간 정리 실패" >&2
fi

echo "백업 완료: $MANIFEST"
