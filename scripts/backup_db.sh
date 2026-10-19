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

DB_URL="${TTAKKARI_DATABASE_URL:-postgresql://localhost/ttakkari}"
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

# SQLAlchemy식 URL(+asyncpg 등)은 libpq 도구가 못 읽으므로 driver 부분을 걷어낸다.
LIBPQ_URL="$(printf '%s' "$DB_URL" | sed -E 's#^([a-zA-Z][a-zA-Z0-9+.-]*)(\+[a-zA-Z0-9_.-]+)?://#\1://#')"

command -v pg_dump >/dev/null || { echo "pg_dump 없음. PostgreSQL 클라이언트를 설치한다." >&2; exit 3; }
command -v psql >/dev/null || { echo "psql 없음. PostgreSQL 클라이언트를 설치한다." >&2; exit 3; }
command -v python3 >/dev/null || { echo "python3 없음." >&2; exit 3; }

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
DUMP="$BACKUP_DIR/ttakkari-$TS.dump"
MANIFEST="$BACKUP_DIR/ttakkari-$TS.manifest.json"

pg_dump -Fc -f "$DUMP" "$LIBPQ_URL"

TABLES="$(psql -tAc "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1;" "$LIBPQ_URL")"
export MANIFEST_TABLES="$TABLES"
export MANIFEST_PATH="$MANIFEST" MANIFEST_DUMP="$DUMP" MANIFEST_TS="$TS" MANIFEST_DB="$LIBPQ_URL"
python3 - "$LIBPQ_URL" <<'EOF'
import json, os, subprocess, sys

db_url = sys.argv[1]
tables = [t for t in os.environ["MANIFEST_TABLES"].splitlines() if t.strip()]
counts = {}
for t in tables:
    r = subprocess.run(
        ["psql", "-tAc", f'SELECT count(*) FROM "{t}";', db_url],
        capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"행 수 조회 실패: {t}: {r.stderr.strip()}\n")
        sys.exit(4)
    counts[t] = int(r.stdout.strip())

dump = os.environ["MANIFEST_DUMP"]
h = subprocess.run(["shasum", "-a", "256", dump], capture_output=True, text=True)
if h.returncode != 0:
    sys.stderr.write("sha256 계산 실패\n")
    sys.exit(4)

manifest = {
    "created_at": os.environ["MANIFEST_TS"],
    "db": os.environ["MANIFEST_DB"],
    "dump_file": dump,
    "sha256": h.stdout.split()[0],
    "tables": counts,
}
with open(os.environ["MANIFEST_PATH"], "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
print(f"테이블 {len(counts)}개, 총 {sum(counts.values())}행 기록")
EOF

# 보존 기간이 지난 백업을 정리한다.
find "$BACKUP_DIR" -maxdepth 1 -name 'ttakkari-*.dump' -mtime +"$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -maxdepth 1 -name 'ttakkari-*.manifest.json' -mtime +"$RETENTION_DAYS" -delete

echo "백업 완료: $MANIFEST"
