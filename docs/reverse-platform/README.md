# Reverse reconstruction platform v2

## API

정본 JSON·요청·응답 예시는 [contract-v2.md](contract-v2.md)다. 기존 `POST /api/reverse`, `/api/agent/reverse/start`, 목록·상세·retry·messages·delete를 유지한다. 신규 `POST`는 `title`, `target_type (auto|cli|web|api|source)`, `mode (auto|analyze|reconstruct|analyze_reconstruct)`, `references`, `max_repair_attempts (0..3)`를 받는다. `PATCH /api/reverse/{id}`에서 제목·유형·모드·참고자료·수정 한도를 바꾼다. analyze→reconstruct 변경은 같은 구현 세션과 기존 observation을 재사용한다.

## 판정과 회차

`ReverseJob.result` JSONB 안에 설정, 추천 근거·계획, `latest`, `history`를 저장한다. 별도 DB 모델·마이그레이션은 없다. 서버는 소스·실행 검사·직접 비교·독립 검증 상태를 별도 공개한다. 직접 비교 JSON은 `.reverse/comparisons/` 내부의 SHA-256 증거를 읽어 입력·사전 상태·환경·원본/복제 출력을 대조하고 stdout, stderr, 종료코드 일치를 재계산한다. 원본 실행 실패는 null/unverified와 사유를 보존한다. 잘못된 레코드도 제거하지 않고 unverified로 보인다. 결과 문구나 `passed`만으로 완료를 승격하지 않는다.

CLI 비교는 `python -m ttakkari.reverse_compare --workspace <workspace> --spec <json>`으로 실행한다. API 비교는 HTTP(S) allowlist를 사용하고 redirect를 따라가지 않으며 URL의 민감 쿼리와 비밀 헤더를 캡처에서 가린다. 비교기 및 실측 `/usr/bin/basename` 증거는 [harness.md](harness.md), [contract-v2.md](contract-v2.md), [evidence](evidence/basename-direct-run-20261107)에 있다.

구현 또는 독립 검증의 직접 비교 불일치는 `max_repair_attempts`(최대 3) 횟수 안에서 기존 구현 세션에 결함 중심 Run을 큐에 넣고 매번 새 독립 검증을 만든다. verifier는 자신의 비교가 1건 이상이고 모두 match이며 필수 검사 증거가 유효해야 통과한다. verifier 비교·실패·검사 기록은 `latest.verification_*`과 `verification_report`에 구현 결과와 구분해 보존한다. 매 회차와 독립 검증 Run은 rounds에 남고 이전 latest 보고서·실패는 history로 이동한다. 중지된 Run은 부분 상태로 남아 retry할 수 있다. 분석 모드는 소스 없이 직접 관찰 또는 문서 근거와 분리된 가설·미확인으로 종료할 수 있다. 정확도 종합 점수는 제공하지 않는다.

관찰은 `level=direct|document|inferred|unverified`와 근거를 보존한다. level이 없는 구형 보고서는 `unverified`다. auto 모드는 “분석만/재현하지 말고” 의도를 분석 전용으로 추천하고, 제목이 없으면 대상 중심 짧은 제목을 제안한다. 상세 조회의 `result`는 v2 객체를 유지하며 최상위 `latest`, `history`, `result_state`는 그 객체와 같은 최신 상태를 가리킨다.

재현 보고서에 README, 환경, 의존성, 실행 명령, 테스트 명령이 있어야 하며 README·소스·안전한 의존성 명세·테스트·실행 로그와 허용된 비교 manifest/raw capture를 기존 다운로드 ZIP에 담는다. 경로 탈출·심링크·민감 파일·의존성 파일 안의 비밀 문자열은 제외한다. 파일 접근은 Workspace 경로 정책과 기존 API 계약을 따른다. 수정 결과마다 별도 실행 로그가 검증되어야 한다.

## 실행과 한계

```bash
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_reverse_compare.py tests/test_reverse_reconstruction.py tests/test_reverse.py unit_tests/test_reverse_static.py
/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py src/ttakkari/api/reverse.py src/ttakkari/reverse.py src/ttakkari/reverse_compare.py unit_tests/test_reverse_tasks.py unit_tests/test_reverse_compare.py tests/test_reverse_reconstruction.py tests/test_reverse_compare.py
git diff --check
```

통합 테스트는 `tests/conftest.py`가 만든 프로세스별 임시 PostgreSQL 클러스터/DB를 쓴다. `TTAKKARI_TEST_DB`를 주면 해당 전용 테스트 DB만 사용하며 운영 DB `ttakkari`는 거부한다. 전체 실행 환경·실측 결과는 [verification.md](verification.md)에 기록한다.

제한: 비교 캡처의 해시와 필드 정합성은 서버에서 확인하지만, 해시는 기록 이후 변조 여부만 증명하며 원본이 실제로 접근 가능한 대상의 참된 동작을 의미적으로 보증하지 않는다. 독립 검증도 선언한 입력 범위와 실행 환경에 한정된다. 외부 참조 접근·환경 차이는 unknown/unverified로 남길 수 있다. 동시 실행·계정풀·소유권·중지/재개는 공용 Run manager가 계속 소유한다.
