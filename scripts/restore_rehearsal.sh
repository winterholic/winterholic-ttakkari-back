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

MANIFEST=""
TARGET_DB="ttakkari_restore_rehearsal"
DB_URL="${TTAKKARI_DATABASE_URL:-postgresql://localhost/ttakkari}"
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

command -v pg_restore >/dev/null || { echo "pg_restore 없음." >&2; exit 3; }
command -v psql >/dev/null || { echo "psql 없음." >&2; exit 3; }

# SQLAlchemy식 URL은 libpq 도구가 못 읽으므로 driver 부분을 걷어낸다.
LIBPQ_URL="$(printf '%s' "$DB_URL" | sed -E 's#^([a-zA-Z][a-zA-Z0-9+.-]*)(\+[a-zA-Z0-9_.-]+)?://#\1://#')"
# 기준 URL의 DB명 부분을 대상 DB명으로 바꾼다.
TARGET_URL="$(printf '%s' "$LIBPQ_URL" | sed -E 's#/[^/?]*([?]|$)#/'"$TARGET_DB"'\1#')"

read_manifest() {
  # $1: 보고 싶은 항목(dump_file|sha256|tables|count:TABLE)
  python3 - "$MANIFEST" "$1" <<'EOF'
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

dropdb --if-exists "$TARGET_URL" >/dev/null
createdb "$TARGET_URL" >/dev/null
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

if [[ "$KEEP" -eq 0 ]]; then
  dropdb "$TARGET_URL" >/dev/null
  echo "리허설 DB 삭제: $TARGET_DB"
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "복원 리허설 통과: 모든 테이블 행 수 일치"
else
  echo "복원 리허설 실패: 행 수 불일치" >&2
  exit 1
fi
