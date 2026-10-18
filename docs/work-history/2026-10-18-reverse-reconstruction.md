# 자연어 역분석 → 작업공간 소스 재현으로 기능 전환

사용자의 이번 명시 지시: 분석 대상을 GitHub 주소 대신 채팅처럼 자연어로 자세히 설명하고,
분석·검증해서 사용자의 작업공간에 소스로 작성한다. 기존 GitHub 요약기 전제를 폐기했다.
정본: [ADR 0041](../adr/0041-reverse-reconstruction.md). ADR 0038·0040은 이전 구현 이력으로 표시했다.

## 변경

- 신규 description/workspace_id API·MCP와 textarea·작업공간 선택. 주소만 받던 입력 계약 제거.
- reverse_tasks.py는 기존 auto/general 도구 작업 세션으로 실행하며 작업별 폴더에 소스·테스트·실행법·증거를 남긴다.
- 서버는 소스·관찰·실행 로그 실재를 확인하고 auto/verification의 별도 세션을 큐에 넣는다.
  독립 검증 기록이 있어야 done. 실패/부분/추가 설명 필요/실행 대기를 구분한다.
- 같은 폴더에 후속 지시를 이어가며 추가 요구와 이전 검증 라운드도 보존한다.
  이전 검증 라운드가 목록에서 빠지는 결함을 회귀 테스트로 재현한 뒤 job-id 태그 조회로 수정했다.
- 선언 소스·검사 로그·README·tests만 ZIP으로 묶어 기존 Artifact 수집기에 넘긴다.
- 링크·절대경로·민감 파일·빈 소스를 검사 기록에서 거부한다. 원본 실행물은 자동 ZIP에 넣지 않는다.
- 새 작업은 legacy 30분 stale/기동 복구에서 제외하고 기존 Run 소유권·대기·재개를 사용한다.
- DB 0044는 nullable 작업공간·구현·검증 세션·결과 경로를 추가한다. 이전 기록 유지.

## 직접 실행한 검증

Codex 명령 실행 환경에서 Postgres 접속에 성공했다. 과거 worker의 EPERM을 이번 환경에 일반화하지 않았다.
전용 DB: ttakkari_reconstruction_3c0ade4a(운영 DB 아님).

```
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_reconstruction_3c0ade4a PYTHONPATH=src ../back/.venv/bin/python -m pytest tests/test_reverse.py tests/test_reverse_reconstruction.py tests/test_orchestrator.py tests/test_artcraft_tasks.py tests/test_api_runs.py tests/test_worker_routing.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_static.py -q --timeout=45
207 passed in 49.59s
```

- real_manager 통합 사례는 실제 DB·Run manager·계정풀 선택·프로세스 실행·소스 실행·별도 검증·ZIP Artifact 수집을 거친다.
  claude/codex 모델만 결정적인 테스트 실행물로 대체했다. 상용 모델의 실제 응답 실측은 아니다.
- 새 빈 DB ttakkari_reconstruction_migration_3c0ade4a에 alembic upgrade head 실행,
  0043 → 0044_reverse_reconstruction 적용 성공. DB 두 개는 격리된 검사 기록용으로 남겼다.
- 변경 파일 ruff, git diff --check 통과.
- 프론트 npm test -- --run: 613 passed, 1 skipped. 타입 검사 포함 npm run build 성공.
  변경 파일 eslint 통과, ui-lint 138건(기존 기준선 139 이내).
- configured ttakkari_browser에서 로컬 빌드+API 합성 fixture로 390px/1280px 화면 확인.
  여러 줄 자연어 설명 → POST(description) → 상세 이동을 실행했다.
  textarea box-sizing 누락을 관측해 수정. 완료된 과정은 접어서 소스 결과가 먼저 보이게 했다.
  최종 로컬 빌드 브라우저 콘솔 0 errors. 초기 dev 서버의 서비스워커 MIME 경고와
  shell fixture의 tasks 응답 누락으로 생긴 오류는 production 실패 근거가 아니며 fixture를 수정했다.
- 스크린샷과 스냅샷: 이번 Run outbox/browser. 화면의 완료 결과는 합성 데이터임을 본문에 표시했다.

## 범위

실제 상용 모델이 임의 웹·앱·바이너리를 끝까지 분석한 품질은 미검증이다.
소스·로그가 있다고 동작 일치를 서버가 의미적으로 증명하는 것은 아니며 별도 검증자의 실행 대조 결과를 표시한다.
배포는 아래 예약 결과와 분리해서 보고한다. 이번 작업에서 launchctl/백엔드 재시작은 실행하지 않았다.
