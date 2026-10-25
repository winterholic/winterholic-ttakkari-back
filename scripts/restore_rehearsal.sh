#!/bin/bash
# SEC2-OPS-01: 백업 복원 리허설 스크립트.
#
# 하는 일: manifest가 가리키는 덤프를 격리된 DB에 복원하고,
#   테이블별 행 수가 manifest와 일치하는지 대조한다.
#   끝나면 리허설 DB를 삭제한다(--keep 제외).
#
# 안전장치:
#   1. manifest 파일이 없으면 DB에 손대기 전에 실패한다(RED 경로).
#   2. 복원 대상 이름에 test·restore·rehearsal 중 하나가 없으면 거부한다.
#      운영 DB(예: ttakkari)에 복원하는 실수를 막기 위해서다.
#
# 사용법:
#   scripts/restore_rehearsal.sh --manifest MANIFEST [--target-db NAME] [--db-url URL] [--keep]
#                                [--passphrase-file FILE]
# 환경 변수: TTAKKARI_DATABASE_URL(연결 기준 URL. 대상 DB명만 바꿔 접속한다)
#   BACKUP_PASSPHRASE_FILE(암호화 산출물 복호화용. 0600 암호 파일)
#
# 암호화 manifest면 같은 암호 파일로 임시 위치에 복호화한 뒤 대조하고,
# 끝나면 복호화 파일을 지운다(--keep이어도 복호화 파일은 남기지 않는다).
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/../.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "프로젝트 Python 환경 없음: $PYTHON" >&2; exit 3; }

MANIFEST=""
TARGET_DB="ttakkari_restore_rehearsal"
DB_URL="${TTAKKARI_DATABASE_URL:-}"
PASSPHRASE_FILE="${BACKUP_PASSPHRASE_FILE:-}"
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    --target-db) TARGET_DB="$2"; shift 2 ;;
    --db-url) DB_URL="$2"; shift 2 ;;
    --passphrase-file) PASSPHRASE_FILE="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "사용법: $0 --manifest MANIFEST [--target-db NAME] [--db-url URL] [--keep] [--passphrase-file FILE]" >&2; exit 2 ;;
  esac
done
[[ -n "$MANIFEST" ]] || { echo "오류: --manifest 필요" >&2; exit 2; }
[[ -f "$MANIFEST" ]] || { echo "오류: 백업 산출물 없음: $MANIFEST (먼저 backup_db.sh 실행)" >&2; exit 2; }

# DB 접속 전에 대상 이름을 검사한다. 운영 DB 덮어쓰기 방지.
case "$TARGET_DB" in
  *test*|*restore*|*rehearsal*) ;;
  *) echo "오류: 격리 대상 아님. 대상 DB명에 test·restore·rehearsal 중 하나가 있어야 한다: $TARGET_DB" >&2; exit 2 ;;
esac

if [[ -z "$DB_URL" ]]; then
  DB_URL="$(PYTHONPATH="$SCRIPT_DIR/../src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -c 'from ttakkari.config import Settings; print(Settings().database_url)')"
fi

command -v pg_restore >/dev/null || { echo "pg_restore 없음." >&2; exit 3; }
command -v psql >/dev/null || { echo "psql 없음." >&2; exit 3; }

if [[ -z "${TMPDIR:-}" ]]; then
  DEFAULT_OPS_TMP="$HOME/.ttakkari/operations/tmp"
  if [[ -d "$DEFAULT_OPS_TMP" ]]; then
    TMPDIR="$DEFAULT_OPS_TMP"
  fi
fi

# SQLAlchemy식 URL은 libpq 도구가 못 읽으므로 driver 부분을 걷어낸다.
LIBPQ_URL="$(printf '%s' "$DB_URL" | sed -E 's#^([a-zA-Z][a-zA-Z0-9.-]*)(\+[a-zA-Z0-9_.-]+)?://#\1://#')"
# 리허설은 로컬 PostgreSQL만 허용한다. 원격 운영 URL로 격리 DB를 만들거나
# 실수로 그 클러스터의 DB를 삭제할 경로를 닫는다.
TARGET_URL="$("$PYTHON" - "$LIBPQ_URL" "$TARGET_DB" <<'EOF'
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
import ipaddress
import os
import sys

url, target = sys.argv[1:]
if not target or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in target):
    print("오류: 대상 DB 이름은 영문·숫자·밑줄만 허용", file=sys.stderr)
    raise SystemExit(2)
p = urlsplit(url)
if p.scheme not in ("postgres", "postgresql") or p.fragment:
    print("오류: 로컬 PostgreSQL URL만 허용", file=sys.stderr)
    raise SystemExit(2)

def is_local_host(val: str) -> bool:
    v = val.strip().strip("[]").lower()
    if v.startswith("/") or v == "localhost":
        return True
    try:
        return ipaddress.ip_address(v).is_loopback
    except ValueError:
        return False

def hosts_list(raw) -> list[str]:
    items = [raw] if isinstance(raw, str) else list(raw or ())
    return [h for item in items for h in item.split(",") if h]

query = parse_qs(p.query)
if "service" in query or os.environ.get("PGSERVICE"):
    print("오류: 복원 리허설은 서비스 파일(원격 가능성)을 허용하지 않음", file=sys.stderr)
    raise SystemExit(2)

hosts = ([p.hostname] if p.hostname else []) + hosts_list(query.get("host"))
if not hosts and os.environ.get("PGHOST"):
    hosts = hosts_list(os.environ["PGHOST"])
addrs = hosts_list(query.get("hostaddr")) or hosts_list(os.environ.get("PGHOSTADDR"))

all_endpoints = [h for h in (*hosts, *addrs) if h]
if not all(is_local_host(h) for h in all_endpoints):
    print("오류: 복원 리허설은 localhost/loopback PostgreSQL만 허용", file=sys.stderr)
    raise SystemExit(2)
print(urlunsplit((p.scheme, p.netloc, "/" + quote(target, safe=""), p.query, "")))
EOF
)"
ADMIN_URL="$("$PYTHON" - "$TARGET_URL" <<'EOF'
from urllib.parse import urlsplit, urlunsplit
import sys
p = urlsplit(sys.argv[1])
print(urlunsplit((p.scheme, p.netloc, "/postgres", p.query, "")))
EOF
)"

read_manifest() {
  # $1: 보고 싶은 항목(dump_file|sha256|tables|encrypted|count:TABLE)
  "$PYTHON" - "$MANIFEST" "$1" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
want = sys.argv[2]
if want == "tables":
    print("\n".join(sorted(m["tables"])))
elif want == "encrypted":
    print("true" if m.get("encrypted") else "false")
elif want.startswith("count:"):
    print(m["tables"][want[len("count:"):]])
else:
    print(m.get(want, ""))
EOF
}

# 암호 파일은 소유자만 읽을 수 있어야 한다.
check_passphrase_file() {
  [[ -f "$1" ]] || { echo "오류: 복호화 암호 파일 없음: $1" >&2; exit 2; }
  local mode
  if mode="$(stat -f %Lp "$1" 2>/dev/null)"; then :;
  elif mode="$(stat -c %a "$1" 2>/dev/null)"; then :;
  else echo "오류: 암호 파일 권한을 확인할 수 없음: $1" >&2; exit 2; fi
  if (( (8#$mode & 077) != 0 )); then
    echo "오류: 암호 파일은 소유자만 읽을 수 있어야 한다(0600): $1" >&2; exit 2;
  fi
}

secure_delete() {
  if command -v shred >/dev/null 2>&1; then shred -u -- "$1";
  elif rm -P "$1" 2>/dev/null; then :;
  else rm -f -- "$1"; fi
}

DUMP="$(read_manifest dump_file)"
[[ -f "$DUMP" ]] || { echo "오류: 덤프 파일 없음: $DUMP" >&2; exit 2; }

# 덤프 무결성을 먼저 확인한다.
WANT="$(read_manifest sha256)"
GOT="$(shasum -a 256 "$DUMP" | awk '{print $1}')"
[[ "$WANT" == "$GOT" ]] || { echo "오류: 덤프 sha256 불일치" >&2; exit 1; }

# 암호화 산출물이면 복호화한 뒤 평문 해시를 대조한다.
DECRYPT_TMP=""
if [[ "$(read_manifest encrypted)" == "true" ]]; then
  [[ -n "$PASSPHRASE_FILE" ]] || { echo "오류: 암호화 백업이므로 복호화 암호가 필요하다(--passphrase-file)." >&2; exit 2; }
  check_passphrase_file "$PASSPHRASE_FILE"
  command -v openssl >/dev/null || { echo "openssl 없음. 암호화 백업을 복원할 수 없다." >&2; exit 3; }
  DECRYPT_TMP="$(mktemp "${TMPDIR:-/tmp}/rehearse-decrypt-XXXXXX")"
  chmod 600 "$DECRYPT_TMP"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -in "$DUMP" -out "$DECRYPT_TMP" -pass "file:$PASSPHRASE_FILE" \
    || { echo "오류: 복호화 실패(암호가 틀렸거나 산출물이 손상됨)" >&2; exit 1; }
  WANT_PLAIN="$(read_manifest plaintext_sha256)"
  GOT_PLAIN="$(shasum -a 256 "$DECRYPT_TMP" | awk '{print $1}')"
  [[ "$WANT_PLAIN" == "$GOT_PLAIN" ]] || { echo "오류: 복호화 평문 sha256 불일치" >&2; exit 1; }
  DUMP="$DECRYPT_TMP"
fi

DB_CREATED=0
cleanup_rehearsal_db() {
  local rc=$?
  trap - EXIT
  if [[ -n "$DECRYPT_TMP" ]]; then
    secure_delete "$DECRYPT_TMP"
  fi
  if [[ "$DB_CREATED" -eq 1 && "$KEEP" -eq 0 ]]; then
    if dropdb --maintenance-db="$ADMIN_URL" "$TARGET_DB" >/dev/null; then
      echo "리허설 DB 삭제: $TARGET_DB"
    else
      echo "오류: 리허설 DB 삭제 실패: $TARGET_DB" >&2
      rc=1
    fi
  fi
  exit "$rc"
}
trap cleanup_rehearsal_db EXIT

createdb --maintenance-db="$ADMIN_URL" "$TARGET_DB" >/dev/null
DB_CREATED=1
pg_restore -d "$TARGET_URL" --no-owner "$DUMP"

FAIL=0
TABLES="$(read_manifest tables)"
while IFS= read -r t; do
  [[ -n "$t" ]] || continue
  WANT_N="$(read_manifest "count:$t")"
  GOT_N="$(psql -tAc "SELECT count(*) FROM \"$t\";" "$TARGET_URL" | tr -d ' ')"
  if [[ "$WANT_N" == "$GOT_N" ]]; then
    echo "MATCH $t: ${GOT_N}행"
  else
    echo "MISMATCH $t: 백업 ${WANT_N}행, 복원 ${GOT_N}행"
    FAIL=1
  fi
done <<< "$TABLES"

if [[ "$FAIL" -eq 0 ]]; then
  echo "복원 리허설 통과: 모든 테이블 행 수 일치"
else
  echo "복원 리허설 실패: 행 수 불일치" >&2
  exit 1
fi
