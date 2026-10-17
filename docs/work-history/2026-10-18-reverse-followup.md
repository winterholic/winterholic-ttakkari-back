# 리버스 엔지니어링 보완 2차 (2026-10-18)

보완본(cbe5eab/9e295e8) 검증 failed에 대한 수정. 이전 결함 1~6 해소는 확인됐다고
전제하고 아래만 고쳤다. 기획 결정: 합성 모델은 reasoning으로 승격하지 않고
task_type general(Sonnet 우선, 정책표 그대로). 실행은 worker 계정풀 경로,
기본 CLI 로그인 폴백 제거.

## 백엔드 변경(back)

- `src/ttakkari/reverse.py`
  - 상태 추가: waiting(모델 대기, 비종료)·partial(부분 완료, 종료+재시도 가능).
    TERMINAL에 PARTIAL 포함, RETRYABLE=(waiting, partial, failed).
  - 라우팅: `_model_route` 삭제. `_select_candidates`(availability general/personal +
    first_stage, 합성 Profile 폴백 없음) + `_run_model`(후보 순회 실행).
    codex/claude는 `desk._ask`(계정 env), antigravity는 읽기전용 one-shot
    (plan·sandbox·빈 작업 디렉터리·`--disable-slash-commands`·`--output-format json`).
    muse·qwen-local 등은 건너뛰고 기록. 한도 감지 시 cool_down+다음 후보, 한도·폴백
    모두 survey 단계 근거에 기록(엔진/모델/계정/via 포함).
  - 쿼터 소진 → `_QuotaExhausted` → waiting(재시도·기동 재개). 계정 자체가 없으면
    no_account → 부분 완료. 빈 합성(`_synth_usable` 거짓)은 '모델 합성 병합' 없이
    부분 완료. 모델 구간 ReverseError 외 예외도 부분 완료로.
  - `_strip_rel`('./' 한 겹만 제거). manifest·evidence·survey 전수 교체.
    rea `.agents/` 문서가 읽기와 인용에 포함된다.
  - 인젝션 방어: `INJECTION_GUARD` 문구, 발췌 고유 구분자(`_fence`, 작업별 토큰),
    overview를 `{text, evidence}`로(근거 없으면 합성 무효), modules path 실재 검사,
    줄 인용 키워드 겹침 대조(`_tokens` camel 분리+한국어 어미 정규화, 못 읽은 줄 면제).
  - `_excerpt` 3-튜플(텍스트·줄 수·마스킹 수). 비밀 값 패턴 마스킹(`_mask_secrets`).
  - 대상 검증: 제어 문자·비표준 포트(443 외) 거부, 회사 범위(`company_roots`,
    기본 `~/development/hyperez`) 거부.
  - `_clone` 진행 중 감시(1초 폴링, 상한 초과·타임아웃 시 그룹 kill+정리).
  - `_locked_finish`에 stage_status 추가. `retry_job`(행 잠금+재시도 전이),
    `resume_waiting_reverse_jobs`(기동 재개), `_target_from_job`.
  - `_public` 개요 객체 대응(구형 문자열도 읽음), waiting 현재 단계.
- `src/ttakkari/config.py`: `company_roots` 추가.
- `src/ttakkari/main.py`: 기동 시 대기 작업 재개 로그 추가.
- `src/ttakkari/api/reverse.py`: `POST /{job_id}/retry`(owner·agent 공통).
- `src/ttakkari/orchestrator/mcp_server.py`: `reverse_retry` 도구.
- `src/ttakkari/orchestrator/tool_policy.py`: REVERSE_TOOLS에 reverse_retry.
- `src/ttakkari/api/assistant.py`: 카탈로그 1행.
- `docs/adr/0040-reverse-model-synthesis.md`: 실제 동작으로 갱신.
- `tests/test_reverse.py`: 20건. 개요 객체·부분완료·대기+재시도(계정 시딩+cool_down)·
  계정풀 실선택(시딩+가짜 CLI 전송, partial에서 계정 기록 확인)·인젝션 격리·
  마스킹·대조·lstrip·제어문자/포트/회사.
- `tests/test_orchestrator.py`: reverse_retry 도구 목록 반영.
- `unit_tests/test_reverse_static.py`: 15건(DB 없이 샌드박스 실행용 미러).

## 프론트 변경(front-reverse → main)

- 상태 waiting/partial 칩·단계 라벨, 개요 객체 렌더+근거, 모델 계정·정적 정리 표시,
  재시도 버튼+`retryReverseJob`, 목록 지우기에 partial 포함, 테스트 9건.

## 검증

- `ruff check` 변경 파일 전체 통과.
- `pytest unit_tests/test_reverse_static.py`: 15 passed.
- `pytest unit_tests` 전체: 267 passed, muse 3건 실패는 기존 결함(깨끗한 트리 확인됨).
- `pytest tests/test_reverse.py`: 미실행 20건 전원 ERROR(세션 픽스처 Postgres 접속
  PermissionError). 샌드박스가 loopback TCP·유닉스 소켓 connect를 막아 스크래치 DB
  불가. 배포 컨트롤러(skip_tests=false)가 돌린다.
- 프론트: `tsc -b` 통과, `eslint` 통과, `vitest` 7 passed(페이지 테스트),
  `vite build` 성공.
- rea 실측: 이 환경에서 실제 `_pipeline`+실제 라우팅 실행은 불가(DB 없음).
  모델 함수(survey/synthesize/검증)는 claude CLI 실측으로 동작 확인(전 세션 rea
  rev ceeff5a4, dropped 0). codex·agy CLI는 샌드박스 EPERM으로 미실행.
  운영 측정은 배포 후 운영 API로 해야 한다(아래 미확인 항목).

## 배포

- 백엔드 main 푸시 후 `request_backend_deploy` 전체 SHA 예약(예약 ID 아래).
- 프론트 main 직접 반영(기존 방식).
