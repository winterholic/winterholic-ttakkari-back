# KB-E0 평가 하네스 구현 인계

상태: **KB-E0 착수 조건부 승인, 제품검색 미승인**. 구현자 검증 진행 중이며 E0 완료·독립 승인·제품 배포를 선언하지 않는다. 다음 단위 자동 착수 없음.

- 독립 clone: `/Users/winterholic/development/kb-e0-harness-a28c55e0`
- 기반 origin/main: `ba71814167d281d1d1ff31ba405692eb12f87a23` (clone 시 원격값)
- 작업 branch: `feat/kb-e0-harness`
- 가져온 설계 수정: `460237722654d6cf1d540f321eae504e63028175`, parent와의 정확한 31개 파일 patch만 적용. merge/cherry-pick으로 타 commit 계보를 동반하지 않았다. [31개 변경 manifest](../../reports/kb-e0/design-import-manifest.json)는 **상태 갱신 전 import 시점**의 byte/hash 대조다.
- [독립 검토 원문](../design/kb-v2/reviews/amendment-independent-review.md)은 `/tmp/kbv2-amend-review`에서 byte 보존. 다른 Run outbox는 접근하지 않았다. 원 evidence 18개와 역사적 수치·원판정은 보존한다.
- 공유 checkout은 최초 read-only status/log/remote만 확인. dirty·로컬 미푸시 `66c8402`·인덱스·git 설정·남의 commit을 변경하지 않았다.

## 실행 주체와 범위

개발·어려운 통계 추론은 이 Codex 세션이 직접 수행했다. 실행 하네스 계산은 Python 3.13.15 / NumPy 2.5.4 / Pydantic 2.14.0 / psutil 7.2.2. 외부 judge/Flash/경량 모델 위임·fallback·sub-agent 호출은 없다. 세션 시스템은 GPT-6 기반 Codex로 기술하지만 정확한 provider model ID는 런타임 도구에 노출되지 않아 **미확인**으로 남긴다. 검색 engine/model 선택은 하지 않았다. task_type reasoning의 범위는 통계 판정이며 검색 제품 확장은 없다. 프로덕션 backend revision/adapter 유무는 조사하지 않았고 그에 관한 주장을 하지 않는다.

[사용법·제한](../../kb-eval/README.md), [엄격 입력 schema](../../src/ttakkari/kb_eval/schema.py), [실행 CLI](../../src/ttakkari/kb_eval/__main__.py), [테스트](../../unit_tests/kb_eval/)를 참조한다. 모델 서버·인덱스·migration·공공자료 수집·collector 코드/계약 수정 없음.

## 설계 → 구현/테스트 대응

| 설계 | 구현 | 직접 검증 / 잔여 |
|---|---|---|
| §4.1–4.2 manifest·권리 | schema/loader, corpus/gold/policy/config/runtime hash, 개별 code/data/qrels/corpus 권리 | strict unknown field, 권리 거부, source 변조, fixture 모델 sentinel. 공개 자료 권리 검토/E1a 미실행 |
| §4.4/P2 quote+revision/time | integrity.anchor exact 원문 hash/context, ambiguous/invalid, ≥.8 cover·인접 최소 passage 집합 | 반복 quote 음성대조·source change·NFKC collision·경계 cover. 정규화 좌표 mapping 미구현(보수적 invalid). 실제 KB5 chunker/E1b 미실행 |
| §4.4/P1 group/split | doc/declared group/duplicate/translation/multidoc 연결 component, 동일 body hash 자동 link | transitive 누수·자동 exact duplicate·holdout 접근 차단. 미등록 근사 중복/실제 DB split 미확인 |
| §4.5–4.8 지표/분모 | metrics, answer_recall, failure enum·trace classifier, slices·abstain binomial/cluster CI | 3질문/10문서 수기 oracle·invalid/실패 전체 분모/Recall bounds·40개 0오류 upper .072158. 의미 지지/자체 answer graded gate는 미구현·E1b 차단 |
| P1 군집/Holm/power | statistics, 질문 가중 cluster bootstrap, family NI, nested joint residual pilot, MC CI | 불균등·상관·Holm·중립/손실·dev/eval 설계 불일치/오염. 잠정 effective cluster guard는 독립 검토 필요. 실제 representative dev power 미확인 |
| P3 AB/BA·SLO | latency balanced_orders/timed_call/latency_report | control도SLO초과·후보+30%·공통 queue·AB부하편향. 실제 100쌍/10분/동시성1·4 서버부하 미실행 |
| §4.6/P10 자원 | runner.resources, GPU collector 주입 | self RSS/CPU/artifact disk 측정. unsupported=null/unverified·실측0 구분. 모델/hostGPU/index/swap 10분 미실행 |
| P4 replay | role/model/input/window/seed 등 cache key, byte checksum, deterministic result 비교 | 1byte/모델/role/window/seed·score byte 변조·cache corruption·input 변경 resume 거부. live tolerance는 fixture 검산, 실제 추론은 미실행 |
| §9.3 중단재개·모바일 | atomic artifact/checkpoint/lock, unmounted review API·버전/cursor/history | CLI 중단/재개·재생·API 409/501/422·동시 라벨 경합. 320/390px/browser/사람 auth 미구현·미실행 |
| §9.3 DB manifest/관측 | strict observation export schema + 합성 event/receipt fixture | 실제 DB export adapter/DB fixture 실행 미실행. 알려진 initdb shmget 거부를 재시도하지 않음 |
| E1b 최종 사람 gate | human_verified 입력/쓰기 거부, one-use audit skeleton | 실제 사람 라벨/hidden holdout 없음. reviewer role 인증 미구현. 공개세트 최종 pass 생성 불가 |

## TDD·검산 기록

`reports/kb-e0/red-*.txt`는 각 구현 전 직접 실행한 pytest 원시 출력이다. 최초 metrics/anchor/통계/캐시/holdout/mobile 상태 18개 실패, loader/runner/CLI 11개 실패, 추가 timer·answer/binomial/compare 7개 실패, 경계 4개 실패, 상태 checker 2개 실패, review API/경합 2개 실패, power 설계 2개 실패를 각각 관찰했다. 이미 구현한 동작의 양성/추가 회귀는 처음부터 green일 수 있으며 이를 red 증거로 포장하지 않는다.

작은 두 군집의 −.10 손실 모의 통과율은 0.2667이었다. 최초 테스트가 이 값 ≤.05라고 가정한 단언은 잘못되어, 원 red 다음 green 시도에서 실패했다(`green-oracles.txt`에도 실패 원문 보존). 이를 감추지 않고 **>.05를 노출하고 power insufficient로 차단**하는 음성대조로 고쳤다. 40+40이나 112문항을 충분성 근거로 쓰지 않는다.

동시 review write는 최초 8개 경쟁 중 같은 version을 4번 저장했다(`red-review-api.txt`). 파일 잠금+version 검증으로 단일 저장/나머지409로 수정했다. 이 증거는 로컬 파일/ASGI 범위이며 PG 동시성 또는 브라우저 증거가 아니다.

## 검증·미실행과 게시

이 항목은 최종 receipt의 명령·종료코드·원시 출력과 함께 갱신한다. 실제 DB/브라우저/추론/GPU/실규모 성능/운영배포는 미실행이며 E0 완료를 주장하지 않는다. 알려진 Codex command sandbox의 initdb `shmget Operation not permitted`는 이 환경의 제한으로만 취급한다. 다른 binary/engine·보안설정 재시도·우회 없음. 배포 backend/host가 불가능하다는 뜻이 아니다.

정상 pre-push hook을 독립 clone에 설치해 작업 branch 게시 시 실행한다. hook의 프로젝트 규약상 non-main branch는 main 계약 묶음을 선택하지 않는다. **main 계약 pytest/전체 DB 회귀는 미실행**: tests/conftest import가 알려진 금지 initdb를 실행하므로 사용자 재시도 금지를 우선한다. 이를 gate 통과로 보고하지 않는다. `TTK_SKIP_GATE`, `--no-verify`, main push, 배포 요청은 사용하지 않는다. 이 제약은 독립 검증자가 DB 허용 환경에서 정상 main gate를 실행할 때 해소해야 한다.

필요한 다음 행동은 다른 기존 세션의 fresh context 독립 검증이다. 위 잔여 E0 표면을 해소·검증하기 전 E0.5/KB3/제품 구현/배포로 넘어가지 않는다.

## 이번 단계 최종 실행 영수증

- [원시 명령 목록과 종료코드](../../reports/kb-e0/commands.json), [통합한 unit/관련 회귀 원시 출력](../../reports/kb-e0/tests-final.txt): **78 passed, 4887 subtests passed in 7.34s**, exit=0. 이 중 E0 하네스 54개 + 기존 관련 회귀 24개다. 전체 DB 제품 회귀가 아니다.
- ruff check/format check, test-smells(`0 finding(s)`) 모두 exit=0. 하네스 line coverage 87%(916 statements, 120 missed). CLI subprocess는 coverage 주입을 하지 않아 CLI 모듈은 0%로 표시되지만 [실제 CLI 명령/JSON 출력](../../reports/kb-e0/cli-replay-exact.txt)으로 별도 검증했다. 미실행으로 0%를 채워 보이지 않는다.
- CLI 중단 exit3→재개0→동일 입력 재생0, metric replay `exact_match`. 비교 결과는 `inconclusive: power_not_established`, 최종 품질 `not_authorized`다. rights loader 거부와 cache corruption은 각각 exit2 및 machine-readable 실패를 남겼다. [권리 실패](../../reports/kb-e0/cli-rights-denied.txt), [변조 실패](../../reports/kb-e0/cli-corrupt-rejected.txt).
- source anchor·통계·자원·모바일 skeleton 양성/음성 fixture를 모두 테스트했다. 마지막 추가 red는 JSON duplicate key/측정 null/첫 metric 기준 기록/비교의 invalid 전파다. red 원문은 덮어쓰지 않았다. 합성 positive power control에서 중립 pass=1.0, −.10 손실 pass=0.0(각200 trials)이 관측됐지만 이는 **이 작은 분산 합성분포의 조건부 통계 계산**이며 실제 corpus의 power/일반화/최종 통과가 아니다.
- [설계 원본 검산](../../reports/kb-e0/design-final.txt): 18/18 hash+size, P1–13/F1–24/F9a coverage, 현재 상태 배너 음성대조, 34 links 확인. 과거 PG/Unicode/toy 계산은 이번 제품/실DB 검증이 아니다.
- source code가 사용하는 production 의존성/uv.lock은 변경하지 않았다. coverage 확인용 pytest-cov 7.1.0/coverage 7.16.2만 독립 clone venv에 추가했다. 깨끗한 frozen install의 하네스 실행/기본 pytest에는 필요 없다.

자가 상태: **KB-E0 하네스 구현 초안과 fixture 검산을 제출할 수 있음. E0 완료/독립 승인 아님.** 실제 DB manifest/관측 fixture, normalized coordinate mapping, 사람 인증 adapter/브라우저 화면·320/390px 검증, 실제 추론·성능·자원 pilot, 권리 확인 공개세트/E1b 사람 holdout은 위 표의 잔여다. 전체 E0 승인 및 다음 단계 착수는 독립 검증자가 판정한다.

원시 stdout/evidence의 byte 보존 때문에 전체 staged `git diff --check`는 trailing whitespace를 보고했다. 신규 실행 코드·테스트·사용법·현행 설계 상태 문서는 별도 scoped check에서 exit0이다. [전체 원시 결과](../../reports/kb-e0/staged-whitespace.txt)와 [범위 검산](../../reports/kb-e0/whitespace-scope.json)을 보존하며 git whitespace 설정이나 gate를 바꾸지 않았다.
