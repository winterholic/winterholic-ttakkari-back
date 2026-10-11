#!/usr/bin/env bash
# 백엔드 수동 배포(Mac Studio). CI/CD 없이 이 스크립트 하나로 끝낸다.
#   ./deploy/deploy.sh            # git pull → 의존성 → 마이그레이션 → 테스트 → 재시작 → 확인
#   ./deploy/deploy.sh --no-pull  # 지금 작업 트리 그대로 배포
#   ./deploy/deploy.sh --skip-tests
#   ./deploy/deploy.sh status     # 서비스·health·최근 로그만 본다
#   ./deploy/deploy.sh rollback   # 직전 배포 커밋으로 되돌려 재시작(마이그레이션은 되돌리지 않는다)
set -euo pipefail
BACK="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACK"
LABEL="net.winterholic.ttakkari.backend"
DOMAIN="gui/$(id -u)"
PUBLIC="${TTK_PUBLIC_URL:-https://ttakkari-api.winterholic.net}"
STATE="$HOME/.ttakkari/deploy"
mkdir -p "$STATE"
c() { printf '\033[1m%s\033[0m\n' "$*"; }

health() {
  local ok=0
  for _ in $(seq 1 30); do
    if curl -fsS -m 3 http://127.0.0.1:8787/api/health >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  [ "$ok" = 1 ] && echo "local  : ok (127.0.0.1:8787)" || { echo "local  : 응답 없음"; return 1; }
  local code host ip
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$PUBLIC/api/health" || true)
  if [ "$code" = "000" ]; then
    # 이 맥의 DNS 캐시(Tailscale 등)가 새 서브도메인을 아직 모를 수 있다. 공용 DNS 로 한 번 더 본다.
    host="${PUBLIC#https://}"; host="${host%%/*}"
    ip=$(dig +short "$host" @1.1.1.1 | head -1)
    [ -n "$ip" ] && code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' --resolve "$host:443:$ip" "$PUBLIC/api/health" || true) (1.1.1.1 경유, 이 맥 DNS 캐시 확인 필요)"
  fi
  echo "public : $code ($PUBLIC)"
}

restart() {
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart -k "$DOMAIN/$LABEL"
  else
    "$BACK/deploy/install-services.sh" backend
  fi
}

status() {
  c "서비스"
  for n in backend tunnel; do
    local st
    st=$(launchctl print "$DOMAIN/net.winterholic.ttakkari.$n" 2>/dev/null | awk '/^\tstate =/{print $3; exit}')
    echo "$n: ${st:-설치 안 됨}"
  done
  c "배포 커밋"; cat "$STATE/current" 2>/dev/null || echo "(기록 없음)"; git log --oneline -1
  c "health"; health || true
  c "최근 로그"; tail -n 15 "$HOME/.ttakkari/logs/backend.log" 2>/dev/null || true
}

case "${1:-}" in
  status) status; exit 0 ;;
  rollback)
    prev=$(cat "$STATE/previous" 2>/dev/null || true)
    [ -n "$prev" ] || { echo "되돌릴 이전 배포 기록이 없습니다."; exit 1; }
    c "롤백 → $prev"
    git checkout --detach "$prev"
    uv sync --frozen --quiet
    restart; health
    echo "$prev" > "$STATE/current"
    echo "마이그레이션은 되돌리지 않았습니다. 필요하면: uv run alembic downgrade <revision>"
    exit 0 ;;
esac

PULL=1; TESTS=1
for a in "$@"; do
  case "$a" in
    --no-pull) PULL=0 ;;
    --skip-tests) TESTS=0 ;;
    *) echo "알 수 없는 옵션: $a"; exit 2 ;;
  esac
done

# 실행 중인 에이전트 작업이 있으면 재시작이 끊는다(interrupted). 먼저 알린다.
active=$(psql -d ttakkari -Atc "select count(*) from runs where status in ('queued','running')" 2>/dev/null || echo "?")
if [ "$active" != "0" ] && [ "$active" != "?" ]; then
  echo "실행·대기 중인 작업 ${active}개가 재시작으로 중단됩니다(대기 중인 건 재시작 뒤 이어서 돕니다)."
  read -r -p "계속할까요? [y/N] " yn; [ "$yn" = "y" ] || exit 1
fi

git rev-parse HEAD > "$STATE/previous"
if [ "$PULL" = 1 ]; then
  c "1/5 git pull"
  [ -z "$(git status --porcelain)" ] || { echo "커밋 안 된 변경이 있습니다. 커밋하거나 --no-pull 로 배포하세요."; exit 1; }
  git pull --ff-only
fi
c "2/5 의존성"; uv sync --frozen --quiet
c "3/5 마이그레이션"; uv run ttakkari migrate
if [ "$TESTS" = 1 ]; then
  c "4/5 테스트"
  if ! uv run pytest -q -x > "$STATE/last-test.log" 2>&1; then
    tail -n 15 "$STATE/last-test.log"
    echo "테스트 실패 → 배포 중단(서비스는 이전 코드 그대로). 전체 로그: $STATE/last-test.log"
    exit 1
  fi
  tail -n 1 "$STATE/last-test.log"
fi
c "5/5 재시작"; restart
health
git rev-parse HEAD > "$STATE/current"
c "배포됨: $(git log --oneline -1)"
