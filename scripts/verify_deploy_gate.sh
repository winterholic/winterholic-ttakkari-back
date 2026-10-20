#!/usr/bin/env bash
# 따까리 백엔드 배포 게이트 사전 검증 및 pre-push 훅 지원 스크립트
#
# 배포 게이트(ttakkari.deployment: uv run pytest -q -x)와 동일한 조건으로
# 로컬 검증을 수행하여, 깨진 커밋이 main에 푸시되어 배포 큐를 막는 사태를 방지합니다.
#
# 사용법:
#   ./scripts/verify_deploy_gate.sh                 # 전체 게이트 테스트 실행
#   ./scripts/verify_deploy_gate.sh --fast          # 빠른 단위/보안 게이트만 실행
#   ./scripts/verify_deploy_gate.sh --install-hook  # git pre-push 훅 자동 설치

set -euo pipefail

BACK="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACK"

if [ "${1:-}" = "--install-hook" ]; then
    HOOK_DIR="$BACK/.git/hooks"
    if [ ! -d "$HOOK_DIR" ]; then
        echo "오류: .git/hooks 디렉터리를 찾을 수 없습니다." >&2
        exit 1
    fi
    PRE_PUSH="$HOOK_DIR/pre-push"
    cat << 'HOOK_EOF' > "$PRE_PUSH"
#!/usr/bin/env bash
# ttakkari deploy pre-push hook: 푸시 전 배포 게이트 검증 강제
# 건너뛰려면 git push --no-verify 를 사용하세요.
BACK="$(git rev-parse --show-toplevel)"
if [ -x "$BACK/scripts/verify_deploy_gate.sh" ]; then
    echo "==> [Pre-Push] 배포 게이트 사전 검증 실행 중..."
    "$BACK/scripts/verify_deploy_gate.sh" --fast
fi
HOOK_EOF
    chmod +x "$PRE_PUSH"
    echo "성공: git pre-push 훅이 설치되었습니다 ($PRE_PUSH)."
    echo "이제 git push 시 자동으로 사전 검증이 실행됩니다. (긴급 우회: git push --no-verify)"
    exit 0
fi

FAST_MODE=0
if [ "${1:-}" = "--fast" ]; then
    FAST_MODE=1
fi

echo "========================================================"
echo " 따까리 백엔드 배포 게이트 사전 검증"
echo " 디렉터리: $BACK"
echo " 모드: $([ $FAST_MODE -eq 1 ] && echo "FAST (핵심 회귀·보안·단위)" || echo "FULL (전체 게이트 pytest -q -x)")"
echo "========================================================"

# 1. 미반영 마이그레이션 및 배포 계약 일치 검사
echo "--> [1/3] 배포 계약 및 마이그레이션 헤드 일치 검사..."
uv run pytest -q tests/test_generation_deployment.py

# 2. 비서/도구 카탈로그 일치 검사
echo "--> [2/3] 도구 카탈로그 일치 검사..."
uv run pytest -q tests/test_context.py -k test_tools_listing_matches_mcp_server

# 3. 테스트 실행
if [ $FAST_MODE -eq 1 ]; then
    echo "--> [3/3] 빠른 단위 및 보안 테스트 실행 중..."
    uv run pytest -q -x tests/test_agent_token_security.py tests/test_account_selection.py unit_tests/
else
    echo "--> [3/3] 전체 게이트 테스트 실행 중 (uv run pytest -q -x)..."
    uv run pytest -q -x
fi

echo "========================================================"
echo " 사전 검증 통과: 안전하게 푸시하고 배포를 요청할 수 있습니다."
echo "========================================================"
