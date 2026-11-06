# Reverse reconstruction platform v2

## API

정본 JSON·요청·응답 예시는 [contract-v2.md](contract-v2.md)다. 기존 `POST /api/reverse`, `/api/agent/reverse/start`, 목록·상세·retry·messages·delete를 유지한다. 신규 `POST`는 `title`, `target_type (auto|cli|web|api|source)`, `mode (auto|analyze|reconstruct|analyze_reconstruct)`, `references`, `max_repair_attempts (0..3)`를 받는다. `PATCH /api/reverse/{id}`에서 제목·유형·모드·참고자료·수정 한도를 바꾼다. analyze→reconstruct 변경은 같은 구현 세션과 기존 observation을 재사용한다.

## 판정과 회차

`ReverseJob.result` JSONB 안에 설정, 추천 근거·계획, `latest`, `history`를 저장한다. 별도 DB 모델·마이그레이션은 없다. 서버는 소스·실행 검사·직접 비교·독립 검증 상태를 별도 공개한다. 직접 비교 JSON은 `.reverse/comparisons/` 내부의 SHA-256 증거를 읽어 입력·사전 상태·환경·원본/복제 출력을 대조하고 stdout, stderr, 종료코드 일치를 재계산한다. 원본 실행 실패는 null/unverified와 사유를 보존한다. 잘못된 레코드도 제거하지 않고 unverified로 보인다. 결과 문구나 `passed`만으로 완료를 승격하지 않는다.

직접 불일치는 `max_repair_attempts`(최대 3) 횟수 안에서 기존 구현 세션에 결함 중심 Run을 큐에 넣는다. 매 회차와 독립 검증 Run은 rounds에 남고 이전 latest 보고서·실패는 history로 이동한다. 분석 모드는 소스 없이 관찰·가설·미확인으로 종료할 수 있다. 정확도 종합 점수는 제공하지 않는다.

재현 보고서에 README, 환경, 의존성, 실행 명령, 테스트 명령이 있어야 하며 README·소스·테스트·실행 로그를 기존 다운로드 ZIP에 담는다. 파일 접근은 Workspace 경로 정책과 기존 API 계약을 따른다. 수정 결과마다 별도 실행 로그가 검증되어야 한다.

## 실행과 한계

```bash
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_reverse_tasks.py tests/test_reverse_reconstruction.py tests/test_reverse.py unit_tests/test_reverse_static.py tests/test_reverse_compare.py
/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/ruff check src/ttakkari/reverse_tasks.py src/ttakkari/api/reverse.py src/ttakkari/reverse.py src/ttakkari/reverse_compare.py unit_tests/test_reverse_tasks.py tests/test_reverse_reconstruction.py tests/test_reverse_compare.py
git diff --check
```

통합 테스트는 `tests/conftest.py`가 만든 프로세스별 임시 PostgreSQL 클러스터/DB를 쓴다. `TTAKKARI_TEST_DB`를 주면 해당 전용 테스트 DB만 사용하며 운영 DB `ttakkari`는 거부한다. 전체 실행 환경·실측 결과는 [verification.md](verification.md)에 기록한다.

제한: 비교 캡처의 해시와 필드 정합성은 서버에서 확인하지만, 해시는 기록 이후 변조 여부만 증명하며 원본이 실제로 접근 가능한 대상의 참된 동작을 의미적으로 보증하지 않는다. 독립 검증도 선언한 입력 범위와 실행 환경에 한정된다. 외부 참조 접근·환경 차이는 unknown/unverified로 남길 수 있다. 동시 실행·계정풀·소유권·중지/재개는 공용 Run manager가 계속 소유한다.
