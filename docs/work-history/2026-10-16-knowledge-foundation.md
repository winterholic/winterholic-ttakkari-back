# 2026-10-16 지식 기반 재설계와 첫 구현

## 사용자 결정

전체 지식 누락률 미측정은 구조 개선을 보류할 이유가 아니다. 원본 보존·관계 발견/누락·교차 프로젝트·개인 맥락·시간/정정 요구를 종합해 설계와 WBS를 갱신하고 바로 구현 착수하라는 명시 지시를 받았다. 과거 '기존 유지/감사 승인 대기'를 대체한다. 원본 영구삭제·민감 추론 자동확정·scope 확대는 포함하지 않는다.

## 정본과 WBS

- docs/knowledge/architecture.md: 요구→설계→인수 추적표, 목표 모델·API·단계/실패/롤백·평가 계약.
- ADR 0031; ADR 0009·README는 현 운영과 구현 예정 구조를 구분해 교차 링크.
- PMS project e99a75de-7ccf-48e6-a5d1-46e455eb79b8 / 실행 parent 6d82e98c-c8f7-4993-a027-e12430c71b15 (5.4.6).
- 기존 6개 task 재사용 + KD/K6/K7/K8 4개 추가 = 10개. 외부 승인 gate 의존 제거, 선행관계 명시.
- 5.4.4는 이번 지시 반영으로 done, 과거 설계 문서 작성/감사는 역사적 사실을 보존하고 새 정본으로 연결.
- KD(a85ec3e-9cd8-4fcc-9f96-499c8674792f) 설계 작성 done. K1(8b908a58-8a50-4140-a2e4-9372e8609e1a) doing: 원본 parser 첫 tranche 구현, 전체 lineage schema까지 완료한 것은 아니다.

## 첫 구현

context-inventory CLI: 모든 ChatGPT shard/node/message/branch occurrence의 안정키·source SHA/JSON pointer·내용 hash·발화자·처리 사유. 문서 제목·원문 텍스트·asset locator를 출력하지 않음. 미지원 modality와 null node도 원장에 남김. 중복/충돌·깨진 JSON·parent/cycle·no-input·symlink·상한을 명시. DB/모델 호출 없음. 출력은 원본 밖 새 private bundle, 기존 결과 덮어쓰기 금지. 새 CLI parser는 현재 검색/색인에 자동 연결하지 않는다.

## 실행 증거와 한계

- RED: 신규 원본 원장 기능이 없어 Source inventory feature is missing으로 실패를 관찰한 뒤 구현.
- 초기 unit 22 passed. 새 deadline 음성 대조 실패를 확인한 뒤 POSIX standalone CLI timer 추가; 최종 unit 25 passed.
- 실제 export root 존재 확인 후 두 probe가 os.scandir 디렉터리 열거에서 머물러 중단(KeyboardInterrupt/exit130). 명령 실행 환경의 현상이며 운영 파일 접근 실패로 일반화하지 않음. 수정한 CLI --timeout3은 exit2 / counts={} / inventory_deadline_exceeded로 종료. 원본 census·실제 지식 보존율은 미검증.
- 합성 fixture CLI: 1 conversation/3 nodes/2 messages; no_message/pending_extraction/unsupported_content 각각1. 원본 실측 결과가 아니다.
- 첫 전체 회귀: 전용 ttakkari_knowledge_test_20261016 DB에서 964 passed/2 failed/2 xfailed/6 subtests. 두 실패는 격리 worktree의 .venv/bin/alembic 부재. 기존 가상환경 symlink로 테스트 실행 경로를 맞추고 전체 재실행. 최종 결과는 아래에 추가.
- Ruff changed Python files: All checks passed. git diff --check 통과.
- 설계 HTML은 configured ttakkari_browser에서 390x844/1280x900 확인, 최종 console errors0. 실제 앱의 새 지식 UI는 아직 구현 전.
- 현재 API /api/version 응답 revision 110cbe857f21bac2849b76d0ed7d4ffb6f0b9964, /api/health {ok:true}. 로컬 deploy/current 파일의 9d0b6fa 값과 달랐다. current 파일만으로 활성 revision을 판단하지 않는다. /api/ready는 HTTPError로 응답했으며 이번에는 status/body 미수집. 이 사실을 generation 기능 부재나 장애로 판정하지 않는다. 운영 DB와 감사 로컬 snapshot 동일성은 미검증.

산출물: outbox/253bbc75-ed96-4373-a259-dfceaea22f3b의 설계 HTML·MD, WBS JSON/CSV, synthetic-inventory-example, chatgpt-source-inventory(unknown timeout 원장), pytest 로그, browser screenshots.

## 남은 작업

K0 runtime/DB identity와 corpus 기준선, K1 후속 immutable schema·adapter·기존 원본↔문서/기억/관계 연결, K2 assertion/history, K3 추출, K4 검색, K5 정정, K6 독립 평가, K7 모바일 가시화, K8 전수 staging/shadow/canary. 현재 source inventory 기능만 구현한 것을 전체 리팩터링 완료나 검색 개선으로 보고하지 않는다.

## 최종 검증

- 최종 전체 회귀 명령: `PYTHONPATH=src TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_knowledge_test_20261016 .venv/bin/python -m pytest -q`.
- 출력: `969 passed, 2 xfailed, 6 subtests passed in 187.38s`. 전체 재실행에서 초기 두 환경 실패를 포함해 정상 통과.
- unit25 및 Ruff clean. 실제 export CLI는 bounded timeout으로 unknown/incomplete/exit2를 반환; raw 전수 집계 성공으로 보고하지 않는다.
- WBS 순서를 KD→K0→K1→K2→K3→K4→K5→K6→K7→K8로 정렬. K1은 5.4.6.3 doing, 설계 KD는 5.4.6.1 done. 전체 리팩터링 완료는 아니다.
- 검증된 첫 tranche를 커밋·main push하고 standing policy의 request_backend_deploy로 예약한다. 예약 ID·최종 활성화 상태는 별도 앱 기록/사용자 보고로 남기며 예약 수락을 배포 성공으로 기록하지 않는다.

## 후속 구현 — 2026-10-16 18시경

사용자의 “후속 구현을 끝까지 이어가며 계속 검증” 지시에 따라 첫 CLI 이후를 직접 구현했다. backend 전용 worktree `back-knowledge-foundation`/branch `feat/knowledge-foundation-20261016`, frontend `front-knowledge-evidence`/branch `feat/knowledge-evidence-ui-20261016`에서 작업했다. 별도 작업자/모델을 띄우지 않았다. 현재 구현·운영 명령과 미완료 계약은 `docs/knowledge/operations.md`에 기록했다.

### 새 코드

- 0034의 7개 원장 테이블, 불변 unit/evidence/review SQL guard와 exact span/hash 검사, source binding·scope·출처 비활성/삭제 차단.
- 원문 Markdown capture/backfill 및 ChatGPT 모든 분기/part/첨부 처리상태 adapter. raw revision SHA와 exact text/quote SHA 구분, 메시지/부모/JSON pointer/part char offset. 원문 파일 변경 없음.
- 후보/assertion·검토/정정/철회·version 충돌·이력·impact API. agent가 user_confirmed를 위조할 수 없고 옛 정정/철회 주장을 재활성화하지 않는다. raw retrieval에도 알려진 정정/철회 annotation을 붙인다. 데이터 있는0034 downgrade는 거부한다(운영 rollback은 app만).
- 7개 knowledge MCP, stdio 실제 프로세스 등록 검사. 처음에는 main entrypoint가 decorator보다 앞에 있어 등록 누락이 발생하는 RED를 관찰하고 수정했다. 역할별 명시 allowlist/readOnlyHint/자동 도구 정책 검사도 갱신했다.
- 기존 recall 결과 순위는 유지하고 추가 knowledge evidence package를 연결. 질문·권한 필터를 candidate limit보다 먼저 적용하여 신규500건이 오래된 관련 주장을 가리는 RED를 고쳤다. current-branch reviewed graph의 1/2hop·cycle/budget·valid time. `as_of`는 현재 검토 상태에서 valid time을 조회하며 recorded-time 당시 상태의 replay는 아직 없다.
- 추출 batch100/20,000char/cursor/manifest/outcome 및 후보만 생성하는 MCP 프로토콜. 별도 provider를 자동 호출하지 않으며 실제 전수 모델 추출·비용 측정은 남았다.
- dev/holdout group leakage·macro Recall와 hit rate·graded nDCG/MRR·paired bootstrap·BM25/hybrid 비교 CLI.
- frontend c8190d3: 도서관 근거·검토 화면, 발화자/시점/분기/hash/이력과 확인/보류/정정/철회. 압축 검색 응답에는 history가 없다는 실제 API 계약을 놓친 UI RED를 추가해, 상세를 불러온 뒤 검토하도록 고쳤다. 다른 세션의 AccountsSection 변경은 포함하지 않았다.

### 실제 staging 측정과 분모

운영을 수정하지 않고 로컬 `ttakkari`의 ctx 테이블만 read-only repeatable-read snapshot으로 새 DB `ttakkari_knowledge_stage_20261016`에 복사했다. source79/docs347/chunks5159/entities47/relations15/memories145. 이는 배포 runtime와 동일한 DB임을 입증한 것이 아니다. token/credential/계정 비밀 테이블은 복사하지 않았다.

등록 문서347 중346 raw capture,1개 읽기/입력 실패로 partial. 원문 unit15,073, 문자3,138,247. 대상은 등록 문서이며 Downloads 원본 export 전수가 아니다. 알려진 동일 raw directory 열거 실패는 반복하지 않았다.

기존 source-reviewed gold-v1 dev24, relevance occurrence32, unique gold path8 중7개가 captured corpus에 존재. 사용자 확정 라벨0. 7/8을 과거31/32나 전체 보존율과 혼동하지 않는다. 최초 원문 unit 검색 Recall@5 .3333은 기존 .4167보다 낮았다. 문서별 중복 제거 후 원문 .4167, nDCG .3210; 기존 .4167/.3321. BM25 .5/.3640, hybrid RRF .5208/.4348. hybrid paired Recall delta+.1042, 95% CI[-.0417,.2708]. 독립 holdout이 아니고 CI가0을 포함하므로 개선 입증/기본 전환 판정은 하지 않는다. 아직 assertion 전수 추출 전이므로 graph의 관계 발견력을 평가한 결과도 아니다.

초기 BM25 구현은 corpus document frequency를 문서마다 재계산했고, 전체 staging 실험 중 이를 발견해 query당1회로 고쳤다. 첫 실험은 자연 종료(exit0); 후속 bounded CLI는 timeout120 안에 succeeded. 이 Run에서 pgrep는 sysmond service not found, 해당 실험 Python에 대한 shell kill은 operation not permitted였다. 다른 환경/운영 불능으로 일반화하거나 같은 거부를 재시도하지 않았다. 임시 Vite는 관리된 write_stdin Ctrl-C로 종료했다.

### 검증

- 초기 전체 회귀: 1018 passed/5 failed/2 xfailed. 5개는 새 MCP 도구를 기존 explicit expected tool/read-only set에 반영하지 않은 정합성 검사였다. allowlist를 실제 도구별로 갱신하고 targeted109 passed.
- 그 뒤 전체1026 passed/2 xfailed/6 subtests. 추가 정정 체인·문서 다양성·populated downgrade·잘못된 source identity 입력 검증까지 넣은 최종 실행:
  `PYTHONPATH=src TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_knowledge_v2_test .venv/bin/python -m pytest -q`
  **1030 passed, 2 xfailed, 6 subtests passed in214.73s**.
- PostgreSQL 실제 migration up/down(빈 ledger)와 데이터 있는 downgrade 거부·보존을 검증. unit/evidence SQL rewrite, source 삭제, work privacy, forged confirmation, cursor/replay, 정정·철회 재수입 등 포함.
- frontend `node node_modules/vitest/vitest.mjs run`: **495 passed,1 skipped**. typecheck·Vite production build·대상 ESLint 및 backend Ruff/diff-check 통과.
- configured browser의 합성 예시320/390/1280px 가로 넘침0, 원문/hash/history 펼치기와 빈 사유 제출 방지. 마지막 browser console errors0/warnings0. 임시 측정 fixture의 ResizeObserver loop window 이벤트가 Vite 로그에 있었으며 실제 앱 동작의 오류로 일반화하지 않는다. 운영 인증/실사용자 흐름 완료는 아니다. browser와 Vite 종료, preview 파일 제거.

로그·수입집계·old/new dev 평가와 브라우저 스냅샷/이미지는 outbox/dd217941-3fda-4c00-85d0-5996b3651f92에 저장했다. staging DB와 독립 테스트 DB는 재현을 위해 남아 있다. production 원본·ctx 데이터는 이번 측정으로 변경하지 않았다.

### 남은 실행과 상태

PMS 5.4.6의 K0~K8은 구현/증거/남은 점을 각각 갱신하고 doing으로 유지했다. 전체 원본/독립 관계 recall 미측정을 “문제 없음”이나 “전체 완료”로 바꾸지 않는다. 기존 배포63fb45ac...는 controller test-stage에서 local STT fixture1초 TimeoutError로 failed였다; 이 사실은 운영 STT 실패를 뜻하지 않는다. 이번 전체 검사에서는 해당 기존 STT 테스트가 통과했다.

후속: 검증한 backend를 clean main에 합치고 push, standing policy로 독립 controller 배포 예약. 예약 직후 턴을 반환하고 결과를 기다리기 위한 polling을 하지 않는다. 활성화 결과 뒤 runtime/DB identity 확인 → production registered backfill → 접근 가능한 raw shard inventory/import → bounded 실제 추출/검토 → 독립 holdout·관계 recall·citation/p95 → canary. meetings/app-chat adapter, legacy provenance/정정 overlay, bitemporal recorded-time 조회, 오류 큐 UI도 남아 있다. 별도 승인 대기로 돌리지 않는다. 배포 예약 ID/최종 runtime는 앱의 deployment job/후속 사용자 보고에 남긴다.
