# NotebookLM 백그라운드 질의 독립 검증 후속 수정

- 사용자 요구: 분석 중 다른 페이지로 이동해도 중단·결과 유실 없이 복구.
- 입력 판정: 검증 작업자 8ddf8df1은 backend `2e2e5af` / frontend `7eded20`에서 부분 통과를 보고. 원 구현 작업자 f312a156의 독립 검증 상태는 `failed`로 기록했다. 이전 보고의 완료 주장은 해당 스냅샷에 대한 재작업 필요 판정으로 정정한다.
- 분리 worktree: `/private/tmp/ttk-nlm-review-back`, `/private/tmp/ttk-nlm-review-front`. 원본의 미커밋 변경은 수정하지 않았다.
- 프론트: 계정 최근 20개 job 목록에서 밀린 저장된 ID는 단건 GET으로 복원·폴링한다. 단건 404만 작업 없음으로 처리하고 일시 네트워크 실패는 재질문 없이 상태 재조회한다. 시작 응답 전 화면 이탈에도 mutation 완료 시 job ID를 즉시 저장한다.
- 브라우저 저장: 완료한 노트북의 캐시는 최대 20개 및 1,000,000 문자 예산으로 오래된 것부터 정리한다. 진행 중 job ID가 있는 캐시는 이 예산의 정리에서 제외한다(활성 작업 때문에 상한은 초과 가능). 노트북당 최근 20턴 정책은 유지한다. 저장 거부는 경고 토스트로 알린다. 서버 기록 전체를 브라우저 기록으로 제공한다는 보장은 없다.
- 백엔드: ask 응답 이후 출처 보강 중 사용자 취소 또는 서버 task 취소가 발생해도 이미 받은 답·인용·선택한 ledger export를 succeeded 결과로 보존한다. ask 전송 중 취소는 기존 불명/취소 정책을 유지한다. Google 계정이 다른 job의 단건·목록 조회 차단도 테스트했다.
- 재현(수정 전): 프론트 신규 검사 5 failed, 기존 27 passed. 백엔드 취소 경합 2 failed(cancelled/unknown으로 답 유실), 교차 Google 계정 조회 1 passed.
- 수정 후 검증: `PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_notebooklm_service.py` → 50 passed. `pytest -q unit_tests` → 857 passed, 1 skipped, 50 subtests passed. 변경 파일 Ruff 통과. 첫 전체 단위 검사는 worktree에 `.venv/bin/python` 경로가 없어 백업 스크립트 16개가 실패했고, 기존 venv 링크 설정 후 전체를 다시 실행했다. 이는 격리 worktree 준비 문제이며 배포 장애 근거가 아니다.
- 프론트 전체 `vitest run` → 1150 passed, 10 skipped(126 files passed, 2 skipped). 이후 캐시 문자 예산 경계 검사 1개 추가: NotebookLM 컴포넌트 34 passed. tsc, ESLint, build, ui-lint 통과(ui-lint 기존 기준선 138건 유지).
- 브라우저: 승인된 ttakkari_browser에서 실제 QueryPanel·API 클라이언트 + 모의 NotebookLM 응답으로 질문→1200ms 지연 응답 전에 다른 화면 이동→재진입→답변 복원→새로고침 후 복원을 확인. job 목록에는 다른 노트북 작업 20건만 제공했다. 최초 favicon 404 외 앱 오류는 관찰되지 않았다. 브라우저를 닫았다.
- 미검증: 실제 Google 계정 분석을 운영 웹에서 실행하는 흐름, Vercel 새 배포 반영, 운영 백엔드 전환. 코드 테스트와 운영 반영은 별개다. 백엔드 LOG-124 아티팩트 테스트 결함 수정 작업(9d9619b7)은 별도로 진행 중이며 배포 게이트를 건너뛰지 않는다.
- 스크린샷: 이 Run outbox `browser/page-2026-10-20T13-47-48-749Z.png`. 배포 예약 상태는 사용자 보고와 outbox 검증 기록에 별도로 남긴다.
