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
# 환경 변수: TTAKKARI_DATABASE_URL(연결 기준 URL. 대상 DB명만 바꿔 접속한다)
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/../.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "프로젝트 Python 환경 없음: $PYTHON" >&2; exit 3; }

MANIFEST=""
TARGET_DB="ttakkari_restore_rehearsal"
DB_URL="${TTAKKARI_DATABASE_URL:-}"
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    --target-db) TARGET_DB="$2"; shift 2 ;;
    --db-url) DB_URL="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "사용법: $0 --manifest MANIFEST [--target-db NAME] [--db-url URL] [--keep]" >&2; exit 2 ;;
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

# SQLAlchemy식 URL은 libpq 도구가 못 읽으므로 driver 부분을 걷어낸다.
LIBPQ_URL="$(printf '%s' "$DB_URL" | sed -E 's#^([a-zA-Z][a-zA-Z0-9.-]*)(\+[a-zA-Z0-9_.-]+)?://#\1://#')"
# 리허설은 로컬 PostgreSQL만 허용한다. 원격 운영 URL로 격리 DB를 만들거나
# 실수로 그 클러스터의 DB를 삭제할 경로를 닫는다.
TARGET_URL="$("$PYTHON" - "$LIBPQ_URL" "$TARGET_DB" <<'EOF'
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
import sys

url, target = sys.argv[1:]
if not target or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in target):
    print("오류: 대상 DB 이름은 영문·숫자·밑줄만 허용", file=sys.stderr)
    raise SystemExit(2)
p = urlsplit(url)
if p.scheme not in ("postgres", "postgresql") or p.fragment:
    print("오류: 로컬 PostgreSQL URL만 허용", file=sys.stderr)
    raise SystemExit(2)
host = (p.hostname or "").lower()
if host not in ("localhost", "127.0.0.1", "::1"):
    socket_hosts = parse_qs(p.query).get("host", [])
    if host or not socket_hosts or not all(h.startswith("/") for h in socket_hosts):
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
  # $1: 보고 싶은 항목(dump_file|sha256|tables|count:TABLE)
  "$PYTHON" - "$MANIFEST" "$1" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
want = sys.argv[2]
if want == "tables":
    print("\n".join(sorted(m["tables"])))
elif want.startswith("count:"):
    print(m["tables"][want[len("count:"):]])
else:
    print(m[want])
EOF
}

DUMP="$(read_manifest dump_file)"
[[ -f "$DUMP" ]] || { echo "오류: 덤프 파일 없음: $DUMP" >&2; exit 2; }

# 덤프 무결성을 먼저 확인한다.
WANT="$(read_manifest sha256)"
GOT="$(shasum -a 256 "$DUMP" | awk '{print $1}')"
[[ "$WANT" == "$GOT" ]] || { echo "오류: 덤프 sha256 불일치" >&2; exit 1; }

DB_CREATED=0
cleanup_rehearsal_db() {
  local rc=$?
  trap - EXIT
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
