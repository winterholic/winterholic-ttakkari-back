#!/usr/bin/env bash
# 예약 접수만 수행한다. 독립 감독이 검사 후 세대 전환 또는 legacy idle 재시작을 맡는다.
# ./deploy/deploy.sh [--no-pull] [--skip-tests]
# ./deploy/deploy.sh status [job-id]
set -euo pipefail
BACK="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACK"
if [ -n "${TTAKKARI_RUN_ID:-}" ] && [ "${1:-}" != "status" ]; then
  echo '따까리 작업에서는 request_backend_deploy MCP 도구로 예약하세요.' >&2
  exit 1
fi
case "${1:-}" in
  status) shift; exec uv run --no-sync python -m ttakkari.deployment status "$@" ;;
  rollback)
    prev=$(cat "$HOME/.ttakkari/deploy/previous")
    if ! git cat-file -e "$prev:src/ttakkari/deployment.py" 2>/dev/null; then
      echo '이전 커밋에는 배포 감독·버전 확인이 없어 자동 롤백할 수 없습니다. 수동 복구가 필요합니다.' >&2
      exit 1
    fi
    [ -z "$(git status --porcelain)" ] || { echo '커밋 안 된 변경이 있습니다.'; exit 1; }
    git checkout --detach "$prev"
    exec uv run --no-sync python -m ttakkari.deployment request --no-pull
    ;;
  *) exec uv run --no-sync python -m ttakkari.deployment request "$@" ;;
esac
