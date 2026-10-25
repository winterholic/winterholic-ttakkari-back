# Antigravity 세션의 다이어그램 스킬 연결

## 문제와 변경

`ttakkari-diagram`은 공유 `~/.claude/skills`에 설치됐지만 Antigravity는 계정별 HOME과 별도 임시 실행 cwd를 사용하므로 해당 설치를 자동 발견한다고 볼 수 없었다. 계정 홈에는 쓰지 않고, 기존 세션별 MCP 프로젝트의 `.agents/skills/`에 `diagram-design`과 `ttakkari-diagram` 두 링크를 연결한다.

발견 위치의 근거는 [공식 CLI 마이그레이션 문서](https://antigravity.google/docs/cli/gcli-migration)의 workspace `.agents/skills/` 경로다. 공유 원본은 `LaunchSpec.write_roots`에서 이미 명시적으로 허용된 `.claude` 루트만 사용한다. 별도 filesystem 권한이나 계정 HOME 접근을 추가하지 않는다. 두 스킬이 없거나 허용 루트가 없으면 설치를 추측하지 않는다.

## 보존 조건

- 얇은 파생 스킬의 형제 상대 참조가 유지되도록 두 링크를 함께 등록한다. 원본 자산은 복제하지 않는다.
- 공유 루트 밖을 가리키는 폴더·SKILL.md, 임시 경로의 symlink 디렉터리, 기존 파일 충돌을 거부한다.
- 자체 manifest에 적힌 일치하는 링크만 다음 Run 준비·terminal cleanup에서 지운다. 기존 일반 파일과 공유 원본은 보존한다.
- 기존 conversation ID, MCP 구성의 권한·수명, sandbox/추가경로 인자는 유지한다.

## 실행 증거와 범위

최초 기준: `23e0cbe691efed2ca699a7a0c1d984510fb543f7`, 별도 `back-diagram-skills` worktree. 독립 리뷰 뒤 최신 main `fb68a86061925ae1958482a74798e2fd3551657a`로 충돌 없이 rebase하고 같은 테스트 32개와 Ruff를 재검증했다.

```text
PYTHONPATH=src <back/.venv/bin/python> -m pytest -q tests/test_antigravity_adapter.py tests/test_antigravity_run.py tests/test_antigravity_policy.py
32 passed, 2 warnings in 5.48s
<back/.venv/bin/python> -m ruff check src/ttakkari/agents/antigravity.py tests/test_antigravity_adapter.py
All checks passed!
```

TDD로 등록/충돌/범위 이탈/권한 철회 테스트의 실패를 먼저 확인했다. 추가로 cleanup 시 `.agents`가 symlink로 바뀐 반례의 외부 파일 삭제를 재현하고 경로 검사를 선행시켜 차단했다. 경고 2건은 기존 timeout fixture의 `run_timeout_seconds=1.5`와 정수 스키마 사이 Pydantic 직렬화 경고다.

독립 검증 세션 `fbbe2558-4b50-44a2-abfd-9149e1ede116`에서도 32개 테스트·Ruff와 추가 symlink 반례를 확인했고 수정이 필요한 결함은 발견하지 않았다. 기본 워크스페이스 생성 시의 기본값만으로 기존 저장 설정을 보증할 수 없다는 검토에 따라, 현재 기획 Run의 실제 `TTAKKARI_GUARD_WRITE_ROOTS`에 `/Users/winterholic/.claude`가 명시돼 있음을 확인했다. 이는 현재 Run의 실행 경로 증거이며 모든 저장 워크스페이스에 대한 주장은 아니다.

이 증거는 로컬 adapter·fake CLI lifecycle 범위다. 배포와 실제 Antigravity의 스킬 목록·선택·생성은 아직 미검증이다. 배포 성공 후 새 Run에서 확인해야 하며, 요청 접수만으로 배포 성공을 기록하지 않는다.

## 통합 main의 계약 누락 보완

푸시 전 실제 계약 게이트에서 최신 main `fb68a86`의 OpenCode 추가분에 두 누락이 드러났다. `POST /api/accounts/{account_id}/opencode/check`의 실제 User 인증 의존성과 일치하는 `owner-plain` 인벤토리 행, 이미 보안 게이트 파일 목록에 포함된 `test_opencode_accounts.py`의 `pytest.mark.security` 선언을 추가했다. 테스트·인벤토리 표식만 수정하며 OpenCode 실행 코드는 변경하지 않는다.
