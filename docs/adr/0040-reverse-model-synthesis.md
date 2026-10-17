# ADR 0040: 리버스 엔지니어링 모델 합성 단계

- 상태: 구현(2026-10-18). ADR 0038의 1차 구현(정적 휴리스틱)이 독립 검증에서
  failed 판정을 받아 뜯어고친 설계다. 0038의 보안 불변(실행 금지·비밀 제외)과
  단계별 근거 타임라인은 그대로 둔다.

## 왜 바꾸나

- 0038 파이프라인은 파일 통계·import in-degree·파일명 키워드 템플릿 문장으로
  결과를 채웠다. 모델이 코드를 읽지 않으므로 "뜯고 곱씹어서 정리"라는 요구를
  충족하지 못한다.

## 결정

- 정적 단계(inventory·structure·flows)는 "사전 조사"로 남긴다. 모델이 읽기 전
  지도를 만드는 용도다. 파일명 키워드 기반 템플릿 아이디어(`_ideas`)는 삭제한다.
  아이디어는 모델 합성에서만 나온다.
- 새 단계 survey → synthesize를 flows 뒤에 둔다. 단계는 총 8개다.
  - survey: 모델이 사전 조사 지도(파일 목록·진입점·허브·체인·README)를 보고
    읽을 파일(최대 24개)과 중간 관찰 메모를 JSON으로 지정한다.
    백엔드가 파일을 읽기 전용으로 읽어 줄번호 발췌로 만든다. 목록에 없는
    경로는 버리고, 비밀 파일은 절대 읽지 않는다.
  - synthesize: 발췌만으로 개요·아키텍처·모듈 실제 역할·데이터/제어 흐름·
    차용 아이디어·한계 구조화 JSON을 만든다. 모든 주장에 파일:줄 근거를 붙이고,
    목록에 없는 파일을 근거로 든 주장은 버린다(버린 건수를 한계에 명시).
- 모델 호출은 앱의 엔진 실행 경로를 재사용한다.
  라우팅은 `worker_routing`(engine auto, task_type=reasoning)을 따른다.
  1순위는 availability의 실행 가능 계정, 없으면 정책표 ROUTES reasoning의
  desk 실행 가능(codex/claude) 첫 후보로 폴백한다. 실행은 `desk._ask`(CLI 스폰)와
  같은 프리미티브다. 모델은 clone 절대경로를 모른다(상대경로+발췌만 전달).
  테스트는 `MODEL_REVIEW` 교체 지점으로 가짜를 끼운다.
- 모델 실패 시 파이프라인 전체를 실패시키지 않고 정적 폴백으로 DONE한다.
  survey·synthesize는 failed로 남고, 한계에 실패 원인을 적는다.
- 수집·걷기 강화: clone은 `-c http.followRedirects=false -c credential.helper=
  -c protocol.file.allow=never`로 고정해 리다이렉트·소유자 자격 증명을 막는다.
  git 타임아웃은 프로세스 그룹 kill+wait로 회수한다. clone 상한 512MiB,
  reverse 루트 상한 2GiB(초과 시 끝난 작업부터 오래된 순 정리, 근거 열람용 보존).
  걷기는 os.walk 그 자리 가지치기 + WALK_CAP에서 정지한다.
  SKIP_DIRS에 .venv/site-packages/uv-cache/.stt-runtime 등을 추가한다.
  테스트·예제·website는 진입점 후보에서 내린다. 심링크는 걷기·topdirs에서 제외한다.
- 대상 검증: 로컬은 `broker.policy.validate_workspace_root`로
  allowed_roots 안만 받는다(/, ~, ~/.ssh, /etc는 밖이라 거부).
  원격은 https + 허용 호스트(기본 github.com, 설정 확장)만 받는다.
  loopback·사설망·링크 로컬·localhost·IP 리터럴 비공개 대역은 거부한다.
- 재시작 복구: 기동 시 `recover_reverse_jobs`로 진행 중 작업을 실패 정리한다.
  목록·상세 조회에도 stale(30분) 판정을 적용한다. 종료 전이는 행 잠금
  SELECT FOR UPDATE + 조건부 전이로 FAILED↔DONE 덮기를 막는다.
- DB: stages/result를 JSONB로 맞추는 마이그레이션 0043 추가(0042_artcraft_tasks 다음). 0041은 배포 예약
  상태라 수정하지 않는다.

## 결과·한계

- 새 테이블 없음. 마이그레이션 1개 추가. generation protocol 변경 없음.
- 한계: DNS 재바인딩 검사는 범위 밖(clone 무자격·무 following + 타임아웃으로 완화).
  모델 근거 검증은 파일·줄 존재까지이며 문장-코드 의미 일치는 보지 않는다.
  codex CLI는 이 샌드박스에서 뜨지 않아 실측은 ROUTES reasoning의 claude 후보로
  했다(2026-10-18, rea rev ceeff5a4). 운영(계정 등록 상태)에서는 availability
  1순위 계정을 쓴다.
