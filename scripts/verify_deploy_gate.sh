#!/usr/bin/env bash
# 따까리 백엔드 push 전 사전 검증(ADR 0066 D3a 첫 구현) 및 pre-push 훅 설치 스크립트
#
# 이 스크립트는 배포 게이트(ttakkari.generation_deployment: pytest -q --timeout=120 -x 전체 실행)를
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
#   5. pytest 입력은 후보 archive만 사용한다. 체커도 현재 committed HEAD에 고정하고 exact source를 함께 archive한다.
#      체커 파일이 미커밋 상태와 다르면 중단한다. TTK_GATE_KEEP=1이면 후보와 체커 입력·manifest를 보존한다.
#      슬롯은 협조형 입장 제어이며 직접 실행된 pytest나 배포 gate 우선권을 보장하지 않는다.
#
# 사용법:
#   ./scripts/verify_deploy_gate.sh                   # 계약 묶음 + 바뀐 테스트 파일 (기본)
#   ./scripts/verify_deploy_gate.sh --commit REV --base REV
#   ./scripts/verify_deploy_gate.sh --contracts-only  # 계약 묶음만
#   ./scripts/verify_deploy_gate.sh --full            # 전체 pytest(-x 없음, 공용 슬롯 사용). 10분 이상 걸린다
#   ./scripts/verify_deploy_gate.sh --fast            # 기본과 같다(예전 옵션 이름 호환)
#   ./scripts/verify_deploy_gate.sh --install-hook    # git pre-push 훅 설치
#   ./scripts/verify_deploy_gate.sh --uninstall-hook  # 훅 제거
#   ./scripts/verify_deploy_gate.sh --preflight       # 읽기 전용 실행환경 점검(uv, initdb, SysV 공유메모리 여유). 아무것도 만들거나 지우지 않는다
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
        --preflight) ACTION=preflight ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
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

if [ "$ACTION" = preflight ]; then
    # DB 의존 시험은 tests/pg_sandbox.py 의 세션 전용 클러스터(initdb)를 쓴다. 호스트의 SysV 공유메모리
    # 세그먼트가 소진되면 initdb 가 shmget 으로 죽는데 원인이 불투명하므로 시작 전에 읽기 전용으로 확인한다.
    # 다른 세션의 IPC 자원은 건드리지 않는다. 커널 한도 상향은 호스트 소유자 몫이다.
    PRE_FAIL=0
    if command -v uv >/dev/null 2>&1; then echo "ok   uv: $(command -v uv)"; else echo "FAIL uv 없음(공식 gate 의 archive 의존성 준비에 필요)"; PRE_FAIL=1; fi
    PG_BIN=
    for d in "${TTAKKARI_TEST_PG_BIN:-}" /opt/homebrew/opt/postgresql@17/bin /opt/homebrew/opt/postgresql@16/bin /usr/local/opt/postgresql@17/bin; do
        if [ -n "$d" ] && [ -x "$d/initdb" ] && [ -x "$d/pg_ctl" ]; then PG_BIN="$d"; break; fi
    done
    if [ -n "$PG_BIN" ]; then echo "ok   initdb/pg_ctl: $PG_BIN"; else echo "FAIL initdb/pg_ctl 없음(TTAKKARI_TEST_PG_BIN 으로 지정)"; PRE_FAIL=1; fi
    if [ "$(uname -s)" = Darwin ]; then
        SHM_MAX="$(sysctl -n kern.sysv.shmmni 2>/dev/null)"
        SYSCTL_RC=$?
        IPCS_OUT="$(ipcs -m 2>&1)"
        IPCS_RC=$?
        if [ "$SYSCTL_RC" -ne 0 ] || ! [[ "$SHM_MAX" =~ ^[0-9]+$ ]]; then
            echo "FAIL SysV 공유메모리 한도를 확인할 수 없음(sysctl 차단 또는 잘못된 값)"
            PRE_FAIL=1
        elif [ "$IPCS_RC" -ne 0 ] || [[ "$IPCS_OUT" != *"Shared Memory:"* ]] || [[ "$IPCS_OUT" == *"ipcs: "* ]]; then
            echo "FAIL SysV 공유메모리 사용량을 확인할 수 없음(ipcs 차단 또는 잘못된 출력)"
            PRE_FAIL=1
        else
            SHM_USED="$(printf '%s\n' "$IPCS_OUT" | awk '$1 == "m" {n++} END {print n + 0}')"
            if [ $((SHM_MAX - SHM_USED)) -ge 2 ]; then
                echo "ok   SysV 공유메모리 세그먼트: 사용 $SHM_USED / 한도 $SHM_MAX"
            else
                echo "FAIL SysV 공유메모리 세그먼트 소진: 사용 $SHM_USED / 한도 $SHM_MAX. 세션 전용 PG initdb 가 shmget 으로 실패한다. 다른 세션의 자원은 지우지 말고 종료를 기다리거나 호스트 소유자가 한도를 올린다."
                PRE_FAIL=1
            fi
        fi
    fi
    if [ -n "${TTAKKARI_TEST_DB:-}" ]; then echo "info TTAKKARI_TEST_DB 지정됨: 소유자가 관리하는 시험 전용 DB일 때만 쓴다(운영 DB 'ttakkari'는 conftest 가 거부)."; fi
    [ "$PRE_FAIL" -eq 0 ] && echo "preflight 통과" || echo "preflight 실패"
    exit "$PRE_FAIL"
fi

ZERO=0000000000000000000000000000000000000000

if [ "$PRE_PUSH" -eq 1 ]; then
    # stdin: <local ref> <local sha> <remote ref> <remote sha>
    PUSH_FAILED=0
    while read -r _lref lsha rref rsha; do
        [ "$lsha" = "$ZERO" ] && continue
        case "$rref" in
            refs/heads/main|refs/heads/master) ;;
            *) [ "${TTK_GATE_ALL_REFS:-}" = 1 ] || continue ;;
        esac
        REF_BASE=
        if [ "$rsha" != "$ZERO" ] && git cat-file -e "$rsha^{commit}" 2>/dev/null; then REF_BASE="$rsha"; fi
        echo "==> [pre-push] 검사 ref $rref: ${lsha:0:12}"
        if [ -n "$REF_BASE" ]; then
            "$BACK/scripts/verify_deploy_gate.sh" --commit "$lsha" --base "$REF_BASE" </dev/null || PUSH_FAILED=1
        else
            "$BACK/scripts/verify_deploy_gate.sh" --commit "$lsha" </dev/null || PUSH_FAILED=1
        fi
    done
    [ "$PUSH_FAILED" -eq 0 ] || exit 1
    exit 0
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

# The checker is an input too: pin it to a committed revision and keep its exact
# source beside the candidate archive when TTK_GATE_KEEP=1.
CHECKER_REVISION="$(git -C "$BACK" rev-parse --verify 'HEAD^{commit}' 2>/dev/null)" || die "체커 revision을 확인할 수 없습니다. git 저장소에서 실행하세요."
CHECKER_INPUT="$TMP/checker-input"
mkdir -p "$CHECKER_INPUT"
git -C "$BACK" archive "$CHECKER_REVISION" scripts/verify_deploy_gate.sh scripts/pytest_slots.py | tar -x -C "$CHECKER_INPUT" || die "고정된 체커 입력을 추출할 수 없습니다."
CHECKER_HASH="$(shasum -a 256 "$BACK/scripts/verify_deploy_gate.sh" | awk '{print $1}')"
SLOTS_HASH="$(shasum -a 256 "$BACK/scripts/pytest_slots.py" | awk '{print $1}')"
PINNED_CHECKER_HASH="$(shasum -a 256 "$CHECKER_INPUT/scripts/verify_deploy_gate.sh" | awk '{print $1}')"
PINNED_SLOTS_HASH="$(shasum -a 256 "$CHECKER_INPUT/scripts/pytest_slots.py" | awk '{print $1}')"
[ "$CHECKER_HASH" = "$PINNED_CHECKER_HASH" ] || die "체커 gate 파일이 revision $CHECKER_REVISION 과 다릅니다(미커밋 수정 거부)."
[ "$SLOTS_HASH" = "$PINNED_SLOTS_HASH" ] || die "체커 slots 파일이 revision $CHECKER_REVISION 과 다릅니다(미커밋 수정 거부)."
printf 'format=1\nchecker_revision=%s\ngate_sha256=%s\nslots_sha256=%s\ncandidate_revision=%s\nbase_revision=%s\nmode=%s\n' \
    "$CHECKER_REVISION" "$CHECKER_HASH" "$SLOTS_HASH" "$SHA" "$BASE" "$MODE" > "$CHECKER_INPUT/manifest.txt"
if [ "${TTK_GATE_KEEP:-}" = 1 ]; then echo "체커 입력 자료 보존: $CHECKER_INPUT"; fi
echo "체커 입력: revision $CHECKER_REVISION, gate $CHECKER_HASH, slots $SLOTS_HASH (SHA-256)"

PYTEST_BASE=(-q --timeout=120 -p no:cacheprovider --tb=short -rf)
FAILED=()
STAGES=()

echo "========================================================"
echo " 따까리 백엔드 사전 검증 (배포 게이트의 전체 실행·-x 를 대신하지 않음)"
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

stage() {  # stage <이름> <명령...>: 실패해도 멈추지 않고 기록한다.
    local name="$1"; shift
    local t0=$SECONDS
    echo "--> $name"
    "$@"
    local rc=$?
    if [ $rc -eq 0 ]; then STAGES+=("통과  $name ($((SECONDS - t0))초)"); else FAILED+=("$name"); STAGES+=("실패  $name ($((SECONDS - t0))초, exit $rc)"); fi
}

if [ "$MODE" = full ]; then
    stage "전체 pytest (-x 없음, 공용 슬롯)" python3 "$CHECKER_INPUT/scripts/pytest_slots.py" -- "$PY" -m pytest "${PYTEST_BASE[@]}"
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
        HAS_ACCOUNT_SELECTION=0
        for f in ${CHANGED[@]+"${CHANGED[@]}"}; do
            case "$f" in
                tests/test_*.py|tests/*/test_*.py|unit_tests/test_*.py|unit_tests/*/test_*.py|benchmark_tests/test_*.py|benchmark_tests/*/test_*.py)
                    [ -f "$f" ] && TESTFILES+=("$f") ;;
            esac
            [ "$f" = "tests/test_account_selection.py" ] && HAS_ACCOUNT_SELECTION=1
        done
        # 이전 배포에서 반복 실패한 LOG-124 회귀는 변경 파일 선택과 무관하게 고정 실행한다.
        if [ -f tests/test_account_selection.py ] && [ "$HAS_ACCOUNT_SELECTION" -eq 0 ]; then
            TESTFILES+=(tests/test_account_selection.py::test_agent_artifact_area_filter_and_move)
        fi
        if [ ${#TESTFILES[@]} -gt 0 ]; then
            stage "바뀐 테스트 파일 (${#TESTFILES[@]}개, 공용 슬롯)" python3 "$CHECKER_INPUT/scripts/pytest_slots.py" -- "$PY" -m pytest "${PYTEST_BASE[@]}" "${TESTFILES[@]}"
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
