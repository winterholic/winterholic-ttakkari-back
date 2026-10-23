# 범용 수집 플랫폼 S2 완성 — 2026-10-23

## 요청과 범위
사용자: "범용수집플랫폼 최대한 완성도 100에 가깝게 지난 맥락들 확인하면서 완성해서 배포까지 해줘". 개인 프로젝트라 검증 후 main 커밋·푸시·배포가 기본이다. S3(ML 라벨링·학습·calibration)와 검색 엔진 최종 선정은 범위 밖이다.
작업 위치: origin/main 기준 새 worktree `back-collector-s2`, `front-collector-s2`(브랜치 feat/collector-s2-20261023). 공유 back/front 체크아웃의 다른 세션 미커밋 변경은 건드리지 않았다.

## 0단계: 막힌 배포 복구
- 직전 예약 cac14fcf(a89d891)은 `unit_tests/kb_eval/test_design_status.py::test_adr_records_user_directed_minimal_productization_without_claiming_quality_gate` 한 건 때문에 게이트에서 멈췄다. ADR 0063 상태 문구에 시험이 요구하는 "2026-10-23 사용자의 명시 지시"가 빠져 있었다.
- 같은 수정을 다른 세션도 동시에 main에 푸시했다(c6d5540). rebase 때 그 문구를 채택했고, 시험은 통과한다(3 passed).
- 베이스라인 전체 게이트(보안 556 passed, 나머지 3830 passed)에서 `test_host_resource_process_state` 한 건이 부하 중 한 번 실패했으나 단독 재실행은 32 passed였다(부하 의존 흔들림, 게이트는 실패 시험을 한 번 재시도한다).

## 이미 main에 있던 것 (중복 구현하지 않음)
1b83b67 수집 코드, 85a595a 관리형 출처·KB 전달·scope 검색·품질 검토 UI, a89d891. 코드로 확인한 뒤 그 위에 아래를 더했다.

## 이번 구현
- CP-S2-05: `collector/budget.py`(day→job 순 선예약 commit 뒤 호출, 환불은 receipt 시 차액만, Asia/Seoul 날짜, 한도 미증명 엔진 거부, unknown 전액 점유, 고아 예약 정리), `cache.py`, Alembic 0066, 설정 기본값(60k 토큰/20호출/$1/일, job 12k/4/$0.2).
- CP-S2-10: `runtime.py` DB 일시정지 플래그, `pilot.py` 파일럿 3곳 + 첫 tick 1회 등록, `_collector_control`이 둘을 사용. 설정 파일/비밀 수정 없이 자동 수집이 운영에서 돈다(근거는 ADR 추가 절).
- CP-S2-08: 전달 재시도(backoff 5회)·reconcile, Alembic 0067. 페이지 제목을 envelope 확장에 담아 KB 문서 제목으로 쓴다(전에는 모든 문서가 출처 이름).
- CP-S2-07: 백엔드 summary/budgets/review/review-audit/items/{id}/deliveries/reconcile/runtime/pilot, 프론트 `/collect`(현황·출처·실행·검토·품질/한도).
- CP-S2-06: 보류 표본 감사(출처×사유 층화, 하루 고정).
- CP-S2-09: `tests/test_collector_integration.py`.
- `register_source`를 control로 옮겨 API와 파일럿이 같은 검증을 쓴다. 일일 페이지 상한의 날짜도 Asia/Seoul로 통일.

## 검증 중 발견·수정한 것
- 실서버 스모크: tech_docs 프로필이 Kubernetes 쪽 전부를 `tech_docs_version_unknown`으로 보류(URL에 버전 없는 latest 스냅샷). 파일럿 두 출처만 `require_version=false`.
- 스모크 첫 판에서 모든 KB 문서 제목이 출처 이름이라 검색 결과를 구분할 수 없었다 → 페이지 제목 사용.
- 검토 대기 목록이 보류만 보여 자동 적재가 꺼진 출처의 통과 자료(GOV.UK)가 빠졌다 → 통과했지만 아직 적재되지 않은 자료 포함.
- 통합 시험 작성 중 outbox의 `default` sink 행(job 자체 등록)이 전달 목록·집계에 섞이는 것을 발견 → 전달 sink만 센다.
- 마이그레이션 왕복 시험이 새 ORM 컬럼 때문에 깨져 0067을 별도 revision으로 분리하고 시험에 포함.

## 실행 근거 (자체 검증이며 독립 검증 아님)
- WBS별 명령 결과: `reports/collector/s2-final/verification-2026-10-23.txt`(예: 계약 51 passed, jobs/api/generation 128 passed, fetch/parser 132 passed, 예산·캐시 16 passed, 통합 39 passed, ruff clean).
- 예산 재생: `reports/collector/s2-05/budget-replay.json`(30/300/3000 요청 모두 20건).
- 실서버 스모크(공용 인터넷, 운영 기본 NetGuard, 격리 DB): `reports/collector/s2-10/live-pilot-smoke-2026-10-23.txt`. 26쪽 fetch → 24건 추출 → 23 통과/1 보류 → 15건 KB 적재(+9건 소유자 검토 대기) → 영어·한국어 검색 왕복. 2차 실행은 304로 8건 `not_modified`, KB 문서 수 15 유지.
- 프론트: vitest 1357 passed, tsc·eslint·ui-lint(기준선 138 유지)·build 통과. 브라우저(Chrome, 합성 API) 320/390/768/1440 × 6개 경로에서 가로 넘침 0·작은 터치 타깃 0·콘솔 오류 0.

## 남은 것 (숨기지 않음)
- 운영 인증이 없어 인증된 /collect 화면, 운영 DB의 실제 적재, 운영 DB identity는 확인하지 못했다.
- hold 재처리 job, IPv6 실접속, 외부 sink용 delivery_unknown 경로.
- 파일럿 권리 판단은 운영자의 원문 읽기이며 법률 자문이 아니다.
- 모델 호출(teacher/judge)은 연결하지 않았다. S3는 runbook 6절의 착수 조건을 따른다.
- 배포 결과는 아래 기록에 이어 적는다(예약 수락은 성공이 아니다).

## 게시·배포
(푸시·예약 후 기록)
