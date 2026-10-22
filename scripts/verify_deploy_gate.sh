#!/usr/bin/env bash
# 따까리 백엔드 push 전 사전 검증(ADR 0066 D3a 첫 구현) 및 pre-push 훅 설치 스크립트
#
# 이 스크립트는 배포 게이트(ttakkari.generation_deployment: pytest -q --timeout=120 전체 실행)를
# 대신하지 않는다. 게이트가 막을 만한 실패를 push 전에 먼저 잡는 빠른 피드백이다.
#
# 동작(ADR 0066 D3a 첫 구현):
#   1. 검사 대상은 작업 트리가 아니라 지정 커밋(기본 HEAD)이다. 커밋을 git archive 로 임시 디렉터리에
#      풀어(배포 게이트와 같은 방식) 거기서 검사하므로, 다른 세션의 미커밋 변경이 결과에 섞이지 않는다.
#      미커밋 변경은 검사하지 않는다.
#   2. 계약 묶음(pytest -m contract)은 항상 실행한다: 도구 카탈로그·라우트/MCP 분류표·세대 계약↔
#      마이그레이션·보안 게이트 목록·단일 마이그레이션 head. 손 목록이 아니라 마커로 묶고
#      (tests/test_contract_marker.py 가 누락을 검사), 0건 선택이면 거짓 통과 대신 실패한다.
#   3. 바뀐 테스트 파일: 기준(--base)부터 커밋까지 바뀐 테스트 파일을 함께 돌린다(LOG-124).
#      import 그래프 기반 선택은 측정상 효과가 낮아 W1에서 뺐다.
#   4. -x 없이 실패를 전부 모아 마지막에 출력한다. 계약 묶음 목표 30초, 전체 목표 2분 안팎이며 넘으면 경고한다.
#
# 사용법:
#   ./scripts/verify_deploy_gate.sh                   # 계약 묶음 + 바뀐 테스트 파일 (기본)
#   ./scripts/verify_deploy_gate.sh --commit REV --base REV
#   ./scripts/verify_deploy_gate.sh --contracts-only  # 계약 묶음만
#   ./scripts/verify_deploy_gate.sh --full            # 전체 pytest(-x 없음, 슬롯 제한 안). 10분 이상 걸린다
#   ./scripts/verify_deploy_gate.sh --fast            # 기본과 같다(예전 옵션 이름 호환)
#   ./scripts/verify_deploy_gate.sh --install-hook    # git pre-push 훅 설치
#   ./scripts/verify_deploy_gate.sh --uninstall-hook  # 훅 제거
# 환경변수: TTK_SKIP_GATE=1 훅 건너뜀, TTK_GATE_ALL_REFS=1 main 외 브랜치 push 도 검사, TTK_GATE_KEEP=1 임시 디렉터리 보존

set -uo pipefail

BACK="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACK"

# git 훅은 GIT_DIR·GIT_INDEX_FILE 같은 저장소 지정 환경변수를 자식 프로세스에 그대로 넘긴다(worktree 에서 push 하면
# GIT_DIR 이 공유 저장소의 .git/worktrees/<이름> 이다). 이 값이 테스트까지 새면 테스트용 `git init`/`git commit` 이
# 임시 디렉터리가 아니라 공유 저장소에 적용돼 core.bare=true 로 바뀌고 worktree 브랜치가 테스트 커밋으로 덮인다.
# 이후의 git 호출은 현재 디렉터리(BACK)로 저장소를 찾으므로 그대로 동작한다.
unset $(git rev-parse --local-env-vars) $(compgen -e | grep -E '^GIT_CONFIG_(KEY|VALUE)_[0-9]+$')

MODE=impacted
COMMIT=HEAD
BASE=
PRE_PUSH=0
ACTION=
TIME_TARGET=120

die() { echo "오류: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --fast) MODE=impacted ;;
        --contracts-only) MODE=contracts ;;
        --full) MODE=full ;;
        --commit) COMMIT="${2:?--commit 값 필요}"; shift ;;
        --base) BASE="${2:?--base 값 필요}"; shift ;;
        --pre-push) PRE_PUSH=1 ;;
        --install-hook) ACTION=install ;;
        --uninstall-hook) ACTION=uninstall ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *)
            # pre-push 훅은 <remote> <url> 위치 인자를 넘긴다.
            if [ "$PRE_PUSH" -eq 0 ]; then die "알 수 없는 옵션: $1"; fi ;;
    esac
    shift
done

hook_path() { git rev-parse --git-path hooks/pre-push; }

if [ "$ACTION" = install ]; then
    HOOK="$(hook_path)"
    mkdir -p "$(dirname "$HOOK")"
    if [ -e "$HOOK" ] && ! grep -q "verify_deploy_gate.sh" "$HOOK"; then
        die "다른 pre-push 훅이 이미 있습니다($HOOK). 덮어쓰지 않았습니다. 직접 병합하세요."
    fi
    cat > "$HOOK" << 'HOOK_EOF'
#!/usr/bin/env bash
# ttakkari pre-push 훅: main push 전에 verify_deploy_gate.sh(계약 묶음 + 바뀐 테스트 파일)를 돌린다.
# 비상 우회: git push --no-verify  또는  TTK_SKIP_GATE=1 git push
[ "${TTK_SKIP_GATE:-}" = 1 ] && { echo "[pre-push] TTK_SKIP_GATE=1: 사전 검증을 건너뜁니다."; exit 0; }
TOP="$(git rev-parse --show-toplevel)"
SCRIPT="$TOP/scripts/verify_deploy_gate.sh"
[ -x "$SCRIPT" ] || exit 0
exec "$SCRIPT" --pre-push "$@"
HOOK_EOF
    chmod +x "$HOOK"
    echo "설치됨: $HOOK"
    echo "main push 때마다 계약 묶음 + 바뀐 테스트 파일을 돌립니다(목표 ${TIME_TARGET}초 안팎)."
    echo "비상 우회: git push --no-verify  또는  TTK_SKIP_GATE=1 git push"
    exit 0
fi
if [ "$ACTION" = uninstall ]; then
    HOOK="$(hook_path)"
    if [ -e "$HOOK" ] && grep -q "verify_deploy_gate.sh" "$HOOK"; then rm "$HOOK"; echo "제거됨: $HOOK"; else echo "제거할 따까리 훅이 없습니다."; fi
    exit 0
fi

ZERO=0000000000000000000000000000000000000000

if [ "$PRE_PUSH" -eq 1 ]; then
    # stdin: <local ref> <local sha> <remote ref> <remote sha>
    COMMIT=
    while read -r _lref lsha rref rsha; do
        [ "$lsha" = "$ZERO" ] && continue
        case "$rref" in
            refs/heads/main|refs/heads/master) ;;
            *) [ "${TTK_GATE_ALL_REFS:-}" = 1 ] || continue ;;
        esac
        COMMIT="$lsha"
        if [ "$rsha" != "$ZERO" ] && git cat-file -e "$rsha^{commit}" 2>/dev/null; then BASE="$rsha"; fi
    done
    if [ -z "$COMMIT" ]; then exit 0; fi
    echo "==> [pre-push] 사전 검증: ${COMMIT:0:12} (건너뛰려면 TTK_SKIP_GATE=1 또는 --no-verify)"
fi

SHA="$(git rev-parse --verify "$COMMIT^{commit}" 2>/dev/null)" || die "커밋을 찾을 수 없습니다: $COMMIT"
if [ -z "$BASE" ]; then
    if git rev-parse --verify -q origin/main >/dev/null; then BASE="$(git merge-base "$SHA" origin/main)"; fi
    # 이미 origin/main 에 들어간 커밋이면 마지막 커밋 하나만 본다.
    if [ -z "$BASE" ] || [ "$BASE" = "$SHA" ]; then BASE="$(git rev-parse --verify -q "$SHA~1" || echo "$SHA")"; fi
else
    BASE="$(git rev-parse --verify "$BASE^{commit}" 2>/dev/null)" || die "기준 커밋을 찾을 수 없습니다."
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/ttk-gate-XXXXXX")"
cleanup() { if [ "${TTK_GATE_KEEP:-}" = 1 ]; then echo "임시 디렉터리 보존: $TMP"; else rm -rf "$TMP"; fi; }
trap cleanup EXIT

PYTEST_BASE=(-q --timeout=120 -p no:cacheprovider --tb=short -rf)
FAILED=()
STAGES=()

echo "========================================================"
echo " 따까리 백엔드 사전 검증 (배포 게이트의 전체 실행을 대신하지 않음)"
echo " 대상 커밋: ${SHA:0:12} $(git log -1 --format=%s "$SHA" | cut -c1-60)"
echo " 변경 기준: ${BASE:0:12}  (작업 트리의 미커밋 변경은 검사하지 않음)"
echo " 모드: $MODE"
echo "========================================================"

git archive "$SHA" | tar -x -C "$TMP" || die "git archive 실패"
cd "$TMP"
unset VIRTUAL_ENV
export TTAKKARI_RELEASE_REVISION="$SHA"
export TTAKKARI_DEPLOY_SOURCE_ROOT="$BACK"
uv sync --frozen --quiet || die "임시 디렉터리의 uv sync 실패(의존성 설치). 비상이면 --no-verify 로 우회하세요."
PY="$TMP/.venv/bin/python"
# 검사 도구는 대상 커밋이 아니라 이 체크아웃의 것을 쓴다. 도구가 생기기 전의 옛 커밋도 같은 기준으로 검사할 수 있다.
[ -f tests/test_migration_single_head.py ] || cp "$BACK/tests/test_migration_single_head.py" tests/

stage() {  # stage <이름> <명령...>: 실패해도 멈추지 않고 기록한다.
    local name="$1"; shift
    local t0=$SECONDS
    echo "--> $name"
    "$@"
    local rc=$?
    # pytest exit 5(선택된 테스트 없음)는 실패가 아니다.
    if [ $rc -eq 0 ] || [ $rc -eq 5 ]; then STAGES+=("통과  $name ($((SECONDS - t0))초)"); else FAILED+=("$name"); STAGES+=("실패  $name ($((SECONDS - t0))초, exit $rc)"); fi
}

if [ "$MODE" = full ]; then
    stage "전체 pytest (-x 없음, 슬롯 제한 안)" python3 "$BACK/scripts/pytest_slots.py" -- "$PY" -m pytest "${PYTEST_BASE[@]}"
else
    # macOS 기본 bash 3.2 에는 mapfile 이 없다.
    CHANGED=()
    while IFS= read -r line; do CHANGED+=("$line"); done < <(git -C "$BACK" diff --name-only "$BASE" "$SHA")
    echo "    변경 파일 ${#CHANGED[@]}개"

    PYFILES=()
    for f in ${CHANGED[@]+"${CHANGED[@]}"}; do
        case "$f" in *.py) [ -f "$f" ] && PYFILES+=("$f") ;; esac
    done
    if [ ${#PYFILES[@]} -gt 0 ]; then
        stage "ruff (변경된 파이썬 ${#PYFILES[@]}개)" "$TMP/.venv/bin/ruff" check --quiet "${PYFILES[@]}"
    fi

    # 계약 묶음: 마커 선택 0건이면 마커가 없는 옛 커밋이거나 누락이므로 거짓 통과 대신 실패한다.
    # W1 이전의 과거 커밋 검증 시에는 마커가 코드에 없으므로 계약 대상 파일들을 직접 찾아 실행한다.
    CT0=$SECONDS
    echo "--> 계약 묶음 (pytest -m contract)"
    if [ -f tests/test_contract_marker.py ]; then
        COUT="$("$PY" -m pytest "${PYTEST_BASE[@]}" -m contract 2>&1)"; CRC=$?
    else
        LEGACY_CONTRACT_FILES=()
        for f in tests/test_context.py tests/test_route_inventory.py tests/test_generation_deployment.py tests/test_migration_single_head.py tests/test_security_gate.py tests/test_security_gate_inventory.py; do
            [ -f "$f" ] && LEGACY_CONTRACT_FILES+=("$f")
        done
        COUT="$("$PY" -m pytest "${PYTEST_BASE[@]}" "${LEGACY_CONTRACT_FILES[@]}" 2>&1)"; CRC=$?
    fi
    echo "$COUT"
    if echo "$COUT" | grep -q "no tests ran" || [ "$CRC" -eq 5 ]; then
        FAILED+=("계약 묶음 (선택 0건: contract 마커 누락)")
        STAGES+=("실패  계약 묶음 (선택 0건, $((SECONDS - CT0))초)")
    elif [ "$CRC" -ne 0 ]; then
        FAILED+=("계약 묶음")
        STAGES+=("실패  계약 묶음 ($((SECONDS - CT0))초, exit $CRC)")
    else
        STAGES+=("통과  계약 묶음 ($((SECONDS - CT0))초)")
        if [ $((SECONDS - CT0)) -gt 30 ]; then
            echo " 경고: 계약 묶음이 목표 30초를 넘었습니다."
        fi
    fi

    if [ "$MODE" = impacted ]; then
        TESTFILES=()
        for f in ${CHANGED[@]+"${CHANGED[@]}"}; do
            case "$f" in
                tests/test_*.py|unit_tests/test_*.py|benchmark_tests/test_*.py)
                    [ -f "$f" ] && TESTFILES+=("$f") ;;
            esac
        done
        if [ ${#TESTFILES[@]} -gt 0 ]; then
            stage "바뀐 테스트 파일 (${#TESTFILES[@]}개)" "$PY" -m pytest "${PYTEST_BASE[@]}" "${TESTFILES[@]}"
        else
            STAGES+=("건너뜀  바뀐 테스트 파일 (선택된 파일 없음)")
        fi
    fi
fi

ELAPSED=$SECONDS
echo "========================================================"
printf ' %s\n' ${STAGES[@]+"${STAGES[@]}"}
echo " 총 ${ELAPSED}초"
if [ "$MODE" != full ] && [ "$ELAPSED" -gt "$TIME_TARGET" ]; then
    echo " 경고: 목표 ${TIME_TARGET}초를 넘었습니다. 훅이 느리면 우회가 늘어나니 --base 로 범위를 좁히거나 알려 주세요."
fi
if [ ${#FAILED[@]} -gt 0 ]; then
    echo " 사전 검증 실패 ${#FAILED[@]}건: 위 pytest 'FAILED' 줄이 실패 전체 목록입니다."
    printf '   - %s\n' "${FAILED[@]}"
    echo " (비상 우회: git push --no-verify 또는 TTK_SKIP_GATE=1. 배포 게이트는 같은 실패로 멈춥니다.)"
    echo "========================================================"
    exit 1
fi
if [ "$MODE" = full ]; then
    echo " 전체 pytest 통과(-x 없음). 배포 게이트의 보안 게이트·재시도 정책은 별도입니다."
else
    echo " 사전 검증 통과: 계약 묶음과 바뀐 테스트 파일 범위만 확인했습니다. 전체 통과를 뜻하지 않습니다."
fi
echo "========================================================"
