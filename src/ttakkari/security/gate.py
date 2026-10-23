"""SEC2-GATE-01: 보안 회귀 묶음과 배포 게이트 정의.

수정한 보안 결함이 이후 기능 작업에서 회귀하지 않도록 배포 후보 검사 단계에서
보안 테스트 묶음을 필수 검증한다.
"""
from __future__ import annotations

from pathlib import Path

# SEC2-GATE-01 보안 회귀 테스트 묶음 파일 목록 (상대 경로).
# 새 보안 테스트를 추가하거나 기존 보안 테스트를 수정할 때 이 목록에 등록하고,
# 해당 파일 상단에 `pytestmark = [pytest.mark.security]`를 지정한다.
# tests/test_security_gate_inventory.py가 누락 여부를 상시 감시한다.
SECURITY_TEST_FILES: frozenset[str] = frozenset({
    # 라우트·도구 인벤토리 (SEC2-GATE-00)
    "tests/test_route_inventory.py",
    # 게이트 자체와 인벤토리 역누락 검사
    "tests/test_security_gate.py",
    "tests/test_security_gate_inventory.py",
    # 에이전트 토큰 보안·프로세스 argv 노출 방지 (SEC2-AGT-01/02)
    "tests/test_agent_token_security.py",
    # 격리 범위 매트릭스 (SEC2-AUTHZ-03)
    "tests/test_authz03_agent_scope.py",
    "tests/test_authz03_host_scope.py",
    "tests/test_authz03_orchestrator_scope.py",
    "tests/test_agent_scope_auth.py",
    # 로그인 브루트포스 차단·루프백 위조 방어 (SEC2-AUTH-03/LGN)
    "tests/test_login_throttle_shadow.py",
    # 운영 환경 설정 fail-fast (SEC2-AUTH-02)
    "unit_tests/test_operational_settings.py",
    # 백업 및 복구 권한/상태 (SEC2-BAK)
    "unit_tests/test_backup_restore.py",
    "unit_tests/test_backup_ops_remaining.py",
    "unit_tests/test_backup_status_allowlist.py",
    # 실행 가드 및 샌드박스 정책 (SEC2-EXEC)
    "tests/test_guard.py",
    "unit_tests/test_muse_guard.py",
    "tests/test_claude_hook_fail_closed.py",
    "tests/test_seatbelt.py",
    # CSRF 방어 (SEC2-AUTH-05)
    "tests/test_csrf_defense.py",
    # 게임 서빙 격리 및 롤백 무결성 (SEC2-GAME)
    "tests/test_game_serving_security.py",
    "tests/test_game_rollback.py",
    # 인증 세션 파기 (SEC2-AUTH)
    "tests/test_password_session_revocation.py",
    # 입력 살균
    "tests/test_nul_sanitize.py",
    # 하네스 조회: 경로 화이트리스트·심볼릭 링크 탈출·비밀 마스킹·소유자 전용 (ADR 0069)
    "tests/test_harness_view_safe.py",
    "tests/test_harness_view_api.py",
    # 로그 마스킹 및 비밀 노출 방지 (SEC2-SECRET-01)
    "tests/test_runs_events_masking.py",
    "unit_tests/test_oplog_scrub.py",
    # 에이전트 계정 및 프로필 격리
    "unit_tests/test_antigravity_account_isolation.py",
    "unit_tests/test_antigravity_managed_profiles.py",
    # 깃허브 API 및 도구 하드닝
    "unit_tests/test_github_hardening.py",
})


def build_security_gate_command(
    python_bin: str | Path,
    *,
    timeout: int = 120,
    fail_fast: bool = False,
    last_failed: bool = False,
) -> tuple[str, ...]:
    """배포 게이트용 보안 회귀 테스트 실행 명령 생성.

    strict xfail은 Phase 3 등 향후 계획된 결함이거나 예상된 동작이므로
    배포를 불필요하게 막지 않도록 `-o xfail_strict=false`로 처리한다.
    """
    cmd = [
        str(python_bin),
        "-m",
        "pytest",
        "-q",
        "-m",
        "security",
        "-o",
        "xfail_strict=false",
        f"--timeout={timeout}",
    ]
    if fail_fast:
        cmd.append("-x")
    if last_failed:
        cmd.append("--lf")
    return tuple(cmd)
