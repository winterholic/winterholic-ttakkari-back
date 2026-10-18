# Qwen 사용자 활성화 경로 · 2026-10-19

실제 모델 수락 Run `35384cde-e6b1-4115-843b-111c80dc3dd7`는 라우팅 준비 단계에서
`Qwen local blocked: disabled`로 끝났다. 모델 호출·파일 수정·테스트·resume는 시작하지
않았다. 작업자 `4ec504cc-10b7-4530-ab43-eb922fe8e51a`의 영수증은 `selected:null`,
`readiness:disabled`다. 이 기록만으로 OS 실행 권한 실패를 주장하지 않는다.

사용자가 해결을 요청해 운영 설정을 편집하지 않고 소유자가 앱에서 직접 활성화할 수
있는 경로를 만들었다. 기본은 계속 꺼짐이다. `TTAKKARI_QWEN_LOCAL_ENABLED`를 명시한
환경은 true/false를 그대로 우선하고 스위치를 잠근다. 환경 설정을 생략한 경우에만
소유자 인증으로 `app_settings.qwen_worker_activation`을 바꾼다. agent 토큰/MCP에는
활성화 쓰기를 추가하지 않았다. 준비 검사를 통과해야 켜지고, 모델 허용 목록·개인
범위·비상 조건·OS 격리를 유지한다. 다음 시도는 DB 상태를 새로 읽으므로 재시작을
요구하지 않는다. 이미 실행 중인 어댑터는 바꾸지 않는다.

검증: 새 API 테스트 최초 8개 실패(404/미구현)를 확인한 뒤 구현했다. 최종
`.venv/bin/python -m pytest unit_tests tests/test_qwen_worker_settings.py tests/test_worker_quota_recovery.py tests/test_qwen_emergency_e2e.py tests/test_qwen_local_run.py -q --tb=short`
결과 `761 passed, 6 warnings, 11 subtests passed in 29.71s`. 환경 고정값 우선,
익명/agent 거부, doctor 실패 시 저장 금지, 회사 범위 제외, 별도 DB 세션에서 동적
반영, 가짜 worker 실행과 동일 Qwen ID resume를 검사했다. ruff 지정 4파일 통과.
`uv run`은 이 Codex 샌드박스의 캐시 경로에서 EPERM이라 저장소 `.venv`의 실행기를
사용했다. 이를 운영 실행 실패로 일반화하지 않는다.

프론트는 기존 `front`의 미커밋 변경을 보존하려 별도 `front-qwen-settings` worktree에
만들었다. 관련 Vitest 11개, TypeScript, ESLint, production build를 확인했다.
설정 섹션은 기존 디자인 토큰을 쓰며 390px 브라우저 시험 fixture에서 스위치 클릭 후
`준비됨`과 checked 상태를 관측했다. fixture는 운영 API·모델·사용자 인증을 사용하지
않았고 검증 후 제거했다. favicon 404 외 앱 콘솔 오류는 관측되지 않았다.

증거: 이번 outbox `f9d9a0b5-2112-465c-ad76-c34eed3674b2`의 backend-checks.log,
frontend-build.log, browser 스냅샷·스크린샷. 배포 예약 접수와 실제 활성화는 별개다.
새 UI의 운영 반영, 실제 환경의 config_locked 여부, 사용자 활성화, 실모델 실행·파일
수정·테스트·재개는 아직 미검증이다. 사용자용 설정 API를 agent 권한으로 호출하거나
보호 설정 파일·운영 DB를 직접 바꾸지 않았다.
