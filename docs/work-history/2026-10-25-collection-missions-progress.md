# 수집 플랫폼 임무 중심 리팩토링 — 진행 중

## 현재 이어받을 자리 — 2026-10-25 14:24 KST
- 전체 C1–C7는 여전히 진행 중. 아래 `이번 코드`와 `실행 근거`는 첫 기능 묶음 f33c9be의 이력이다. 이번 추가 구현은 이 절이 최신이다. 기존 back/front dirty checkout은 계속 보존한다.
- GitHub/URL 없는 연구 임무를 내부 `mission_prepare` CollectorJob으로 영속 큐에 연결했다. API에서는 네트워크·모델을 실행하지 않는다. managed claim에 임무 관계를 포함하며 기존 attempt fence·orphan recovery·cancel·generation drain을 재사용한다. 준비 취소가 먼저 기록되면 새 자식 수집을 만들지 않는 재현 검사도 통과했다.
- 연구 준비는 DuckDuckGo HTML 검색→후보 URL/검색어/제공자/순위 보존→기존 정책-backed acquisition을 연결한다. 검색 진전은 매 쿼리마다 원자적으로 보존하고 새 후보가 두 번 연속 없으면 종료한다. 현재 쿼리는 기존 결정적 시작안이며, **하위 주제 의미 분해·문서에서 새 개념을 읽어 후속 검색하는 자율 연구는 아직 미완성**이다. 알려지지 않은 권리의 후보는 gap으로 남으므로 URL 없는 외부 임무 전체 성공을 아직 검증하지 않았다.
- 실제 Bing RSS 실험은 질의와 무관한 Google/YouTube 결과 및 RSS 표시 외 이용 제한을 확인하여 폐기했다. 현재 코드에 Bing 경로는 없다. DuckDuckGo 외부 제한 실험은 `"harness engineering" research paper` 질의에서 arXiv 논문, 원문 사이트, 논문 모음 등 5개 URL의 관련 제목을 확인했다. 이는 후보 탐색 검증이며 본문 적재·신뢰도 검증이 아니다.
- GitHub API repo/commit/tree inventory를 실제 임무 자식 수집에 연결했다. 하위 LICENSE 우선 적용, MIT/Apache 전체 표준 본문 대조, 별도 제한 문구가 앞뒤에 있으면 unknown, 여러 LICENSE가 있으면 unknown을 유지한다. 하위 LICENSE는 commit-pinned raw로 읽고 Git blob SHA와 대조하여 파일별 API 호출을 피한다. license notice와 원본 path/revision/blob SHA는 임무 provenance를 통해 KB origin_meta로 전달된다. LICENSE 자체는 지식 문서 후보에서 제외했다.
- 자동 확인한 라이선스 정책은 `allowed_path_prefixes`로 해당 commit/file에 한정한다. 빈 필드는 기존 policy hash를 바꾸지 않도록 직렬화에서 생략한다. 파일 정책이 `/robots.txt`도 막아 `robots_unreachable`이 되던 결함을 로컬 재현 후 교정: origin의 `/robots.txt` 제어 파일만 별도로 허용하며 실제 문서의 host/path 제한은 그대로다.
- Python 공식 문서의 PSF-2.0 근거를 공식 license 페이지에서 확인하고 서버 정책으로 등록한다. 수집에는 기존 robots/pacing/SSRF/권리 검사가 적용된다. 문서 본문(main/article)의 링크를 사이트 공통 메뉴보다 먼저 탐색하고, PDF 링크도 보존한다.
- 임무 문서 작업은 25건 단위로 정상 yield한다. retry 횟수를 소비하지 않고 다음 tick에서 기존 cursor를 이어간다. 전체 요청 page budget에 닿으면 frontier를 버리지 않고 partial + `mission_page_budget`/남은 URL 수로 종료한다. **한 source/job의 정책 한도를 넘는 자동 하위 작업 분할·임무 확장/재개 API는 아직 없다.**

### 이번 검증 범위
- **최종 추가 확인:** 코드 revision `02b1510610faf10cef265b4b8f796c98d78b8983`에서 `COLLECTION_LIVE=1 PYTHONPATH=src ../back/.venv/bin/python -m pytest -q -s tests/test_collection_live.py --tb=short` → **2 passed in 8.02s**. GitHub 15개 eligible 중 제한한 2개(SKill.md + references/_sections.md) 실제 적재·ASP.NET 검색 확인. Python 본문 링크 우선 탐색으로 tutorial/builtins/library **3개 모두 실제 적재·Python 검색**, 29개 frontier 보존. 두 임무는 의도한 제한 실험이므로 partial이며 B/D 전체 커버리지 완료가 아니다. 실행 stdout은 이 Run outbox `collection-live-check.txt`로 보존하고 share_file 한다. 아래 첫 외부 실험의 '최종 파일 선택/탐색 순서 재검증 필요'는 이 관측으로 해소했다. 서버/브라우저/작업 세션 잔류 없음, 시험 PG는 fixture teardown으로 정리됐다.
- Codex 명령 환경, 격리 PG/로컬 HTTP: `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_collection_missions.py tests/test_collector_jobs.py tests/test_collector_pipeline.py tests/test_collector_fetch.py unit_tests/test_collection_adapters.py unit_tests/test_collector_policy.py unit_tests/test_collector_parser.py --tb=short` → **199 passed, 66 subtests passed in 9.50s**. 그 뒤 MIT 앞부분의 추가 제한 문구 반례를 추가/교정하고 `unit_tests/test_collection_adapters.py` → **11 passed**. 같은 범위 중복 수를 합산하지 않는다. 변경 ruff와 diff check clean.
- opt-in `tests/test_collection_live.py`: 실제 외부 GitHub openai/skills(aspnet-core 하위 경로) 및 Python tutorial→격리 KB 적재/검색, **2 passed in 7.87s**. GitHub commit `49f948faa9258a0c61caceaf225e179651397431`, SKILL.md 포함 2건 적재; 당시 두 번째는 LICENSE였으며 이후 후보에서 제외했으므로 최종 파일 선택은 재검증 필요. Python 3건 budget에서 2건 적재/검색, 1건 changelog response_too_large 보류, 29개 frontier 보존. 이후 본문 링크 우선순위를 변경했으므로 최종 탐색 순서는 재검증 필요. **운영 DB/배포 검증이 아니다.** 이 두 제한 실험으로 E2E-1~5 전체 완료를 주장하지 않는다.
- 초기 외부 GitHub 시험의 `robots_unreachable`은 위 path 정책 제어 파일 결함이었다. raw host의 실제 robots는 404(no rules), 수정 후 실제 acquisition이 통과했다. 호스트/운영 전체 장애로 일반화하지 않는다.

### 다음 실행 (원래 요구에서 남은 것)
1. 위 최종 외부 검증과 현재 git 상태/영속 계약을 복원한다. GitHub 파일 선택/raw license·Python 본문 링크 순서는 재확인했으므로 같은 검사를 무조건 반복하지 않는다.
2. 임무 pause/resume/cancel/확장/주기 갱신을 API/UI에 연결하고, source/job page 한도를 넘는 자동 분할·재개를 구현한다. **현재 allocation은 예약 페이지 기준이라 먼저 만난 출처가 예산을 선점하거나 미사용 예약이 남을 수 있다.** 복수 레포/혼합/연구 임무에 실제 사용량 기반의 공정한 하위 작업 할당을 연결해야 한다. 한 출처 metadata 실패가 뒤의 독립 출처를 막지 않도록 준비 작업도 분리/기록한다.
3. personal/work 실제 데이터·참조·모델 통합(C2), 경량 ML 실제 보수적 선별·독립 평가(C4), 사용자 맥락 자체 적재와 자료 관계, 논문 metadata/브라우저 경로를 이어간다. 원래 시나리오 A–E 인수 기준은 manage_work.original_request로 복원한다.
4. C1–C7 전체 증거와 미완료를 대조한 뒤 clean main 통합·push·운영 배포 및 실제 기능 확인. 현재 frontend는 e5afcc6 clean, 이번 Run에서 변경하지 않았다. 아직 main merge/push/배포 없음. 별도 worker/외부 서버 없음.

### 계속 지킬 결정과 함정
- 최종 목표는 실제 유용한 지식의 수집·검색이며 테스트/Gate/예약은 전체 완료가 아니다. 추가 승인 없이 계속한다.
- 외부 SKILL 지시는 끝까지 데이터. source rights는 서버 검증 근거만 사용한다. client가 license/rights를 주장해 자동 허용시키지 않는다.
- collector는 KB 저장소/RAG를 중복 구현하지 않는다. 실제 delivery receipt와 검색을 확인하고 export를 적재로 세지 않는다.
- protected harness/config.env/accounts/logs는 열지 않는다. 운영 조회/배포는 지원 MCP를 사용한다.

## 원래 목표와 작업 위치
- 사용자 2026-10-25 요청: 자연어·URL·GitHub·논문/PDF·사용자 맥락을 임무 하나로 처리하고, 자율 탐색·저비용 ML 선별·통합 지식 도서관·임무 UI·A–E 실제 E2E·운영 배포까지 수행한다. 중간 구현/테스트/예약은 전체 완료가 아니다.
- backend: `back-collection-missions`, branch `feat/collection-missions-20261025`, 시작 `551c354b49b5e3a9ac40424e2361c558c857743a`.
- frontend: `front-collection-missions`, 같은 branch, 시작 `da9d98d`.
- 기존 `back`/`front`는 크게 뒤처진 dirty checkout이다. 해당 변경은 보존했다. 프로젝트에 적용되는 AGENTS.md/CLAUDE.md는 발견되지 않았다.
- 전체 계약은 기획 세션 manage_work C1–C7에 active로 보존. 아직 필수 요구 중 어느 묶음도 전체 done이 아니다.

## 이번 코드
- 임무 계획/영속 모델/인증 owner API: 목표·URL·사용자 맥락·예산·종료 조건·참고 자료 유형을 보존한다. URL 분류에는 모델 호출이 없다. URL 없는 요청의 현재 계획은 결정적 시작안이며 지능형 자율 연구가 구현된 상태는 아니다.
- 기존 정책-backed 수집 source/job/frontier/SSRF/robots/raw/dedupe/KB outbox를 재사용한다. 임무 provenance가 job→item envelope→KbSource origin_meta까지 이어진다.
- 결과는 실제 KB delivery와 document/revision ID를 집계한다. 단순 보관/export를 KB 적재로 세지 않는다. 의미·온톨로지 처리는 unverified로 구별한다. 목록은 본문을 읽지 않고 집계하고 상세 메타데이터는 SQL에서 100건으로 제한한다.
- additive `0072_collection_missions`와 기존 세대 계약 online migration 선언을 연결했다. 수집/KB 기존 이력을 재작성하지 않는다.
- 직접 지정한 PDF는 pypdf 6.21.2의 별도 프로세스에서 추출한다. input/page/decompression/CPU/wall 제한, 암호화·스캔 PDF 보류, 페이지 경계·실제 parser version을 남긴다. macOS RLIMIT_AS는 ValueError라 적용하지 않으며 Linux만 VM soft limit을 둔다. 추출기가 자른 본문을 완전한 문서로 표시하던 기존 분석 경로도 교정했다.
- GitHub API 어댑터는 repo→commit→tree→SKILL/문서/관련 코드 목록을 조회하고 raw URL을 commit SHA로 고정한다. 실행 경로에 연결하는 작업은 남아 있다. 미확인 라이선스를 allowed로 추정하지 않는다. 저장소/하위 파일 라이선스 자동 해석을 더 연결해야 한다.
- 한국어 임무 composer, 맥락/텍스트 파일, 선택적 계획 preview, 임무 목록/성과/결과/KB 링크. 운영 패널은 접고 열 때만 mount하여 불필요한 요청을 피한다. UI에서 범위 토글을 뺐지만 실제 scope 데이터 통합은 아직 전혀 완료되지 않았고 고급 패널은 현재 personal legacy 경로다.

## 실행 근거와 범위
- Codex 명령 환경, 격리 테스트 PostgreSQL/로컬 HTTP fixture:
  `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_collection_missions.py tests/test_collection_mission_migration.py tests/test_collector_product_flow.py tests/test_collector_pipeline.py tests/test_route_inventory.py tests/test_security_gate_inventory.py unit_tests/test_collection_plan.py unit_tests/test_collection_adapters.py unit_tests/test_collector_parser.py unit_tests/test_executor_migration_contract.py unit_tests/test_schema_head_admission.py --tb=short`
  → **100 passed, 21 subtests passed in 5.62s**. 임무→HTML/PDF 실제 추출→KB receipt→검색, provenance, 멱등·권한·export 구분, schema roundtrip, 라우트와 배포 계약 포함. 외부/운영 E2E를 뜻하지 않는다.
- frontend `vitest run src/pages/CollectPage.test.tsx src/features/collector/ --reporter=dot` → **12 passed**. `tsc -b`, 변경 파일 ESLint, Vite build 성공. 기존 큰 chunk advisory 유지. 변경 Python ruff 성공.
- 외부 GitHub 제한 실험: `openai/skills` commit `49f948faa9258a0c61caceaf225e179651397431`, eligible files 700, limit 1; `skills/.curated/aspnet-core/SKILL.md` commit-pinned raw 200, 5544 bytes, frontmatter 확인. repo root license는 unknown. 이는 inventory/raw 획득만 검증했으며 KB 적재나 B 시나리오 완료가 아니다. 첫 smoke script의 SafeFetcher user_agent 누락은 수정한 검증 명령으로 재확인했다.
- configured ttakkari_browser 개발 fixture(실제 사용자 데이터 아님): 320/390/1440px snapshot bounds와 화면 확인. screenshot `page-2026-10-25T05-02-33-626Z.png`는 시험 데이터이며 Run outbox에서 share_file 했다. 임시 UI harness와 서버는 제거/종료했고 브라우저도 닫았다. 운영 로그인/모바일 실기기 확인 아님.
- main merge/push/배포는 아직 하지 않았다. 운영 최근 성공은 MCP에서 `94a4963c`, 최신 main `551c354b`의 이전 배포는 다른 플랫폼 테스트에서 failed로 관찰했다. 현 작업 때문에 그 원인을 추정하거나 검증 완료로 바꾸지 않는다.

## 다음 실행 — 승인 추가 없이 계속
1. 현재 작업 공간의 실제 git 상태와 manage_work를 복원한다. 유효한 검증을 처음부터 반복하지 않는다.
2. GitHub 준비·검색·정책 해석을 API 프로세스 밖의 기존 durable job claim/fence/재시도 경로에 연결한다. URL 없는 임무를 단순 discovery_pending에 두는 현재 임시 상태를 해소한다. 외부 SKILL 지시문은 끝까지 데이터로 취급한다.
3. GitHub 하위 LICENSE, 공식 Python 문서 라이선스, 공개 검색/논문 provenance를 자동 검증하고, 검증 불가 권리만 정확한 gap으로 남긴다. .pdf 링크 탐색은 아직 legacy parser가 제외하므로 함께 연결한다.
4. 임무별 batch yield·자동 분할·frontier 보존·재개·추가 수집·주기 갱신과 상태 제어. 현재 기존 파이프라인은 page cap에서 frontier를 버리므로 그대로 두면 D 인수 불가다.
5. personal/work 실제 데이터/모델/참조 통합은 별도 필수 작업이다. immutable evidence와 payload hash를 무작정 덮지 않는다. 현재 프론트 변경만으로 C2가 충족됐다고 쓰지 않는다.
6. 경량 ML 후보를 최신 공식 근거로 좁히고 대표 실험/독립 평가를 통해 실제 선별에 연결한다. 현재 ML은 기존 shadow 그대로다. 입력 목적 관련성, 보수적 자동 처리, 보류/복구와 미검증 표시가 필요하다.
7. 사용자 맥락의 KB 적재와 외부 자료 관계, PDF/파일 입력, 시나리오 A–E 제한 외부 E2E·필수 회귀 후 clean main 통합·push·배포 예약을 진행하고 운영 실동작을 별도 확인한다.


## C4 구현: 경량 ML 다축 품질·목표 관련성·보수적 자동 선별·복구·독립 평가 (2026-10-25)

### 구현 내용
- **새 모듈 (`src/ttakkari/collector/triage.py`)**:
  - 외부 유료 API나 대규모 신경망 없이 로컬 CPU에서 문서당 0.05ms 수준으로 동작하는 경량 다축 평가 엔진.
  - 1. **목표 관련성 (Goal Relevance)**: 자연어 `goal`과 문서 제목(Title), 도입부(Lead), 본문(Body) 간의 토큰 커버리지, 한글 조사 정규화, sub-linear TF 포화 가중치를 결합해 `similarity` 스케일([0.0, 1.0]) 산출. Ingestion envelope 계약에 맞춰 `query_id` (goal 해시)와 근거(evidence)를 함께 기록.
  - 2. **본문 품질 (Content Quality / Utility)**: 문자수, 기호 비율(symbol_ratio), 알파벳/한글 비율(alpha_ratio), 중복/반복 n-gram, 짧은 줄 비율, 본문 구조(헤더, 코드 블록, 목록 등), 추출 truncation 여부를 결합해 `rule` 스케일 산출.
  - 3. **중복/근접 중복 (Novelty)**: exact match(0.0) 및 MinHash 128-band LSH Jaccard 유사도 기반 novelty 점수 산출.
  - 4. **출처 신뢰도 (Source Trust / Factuality)**: 검증된 라이선스/허용 출처 여부 기반 신뢰 신호 산출.
  - 5. **보수적 자동 선별 (Conservative Auto-Triage)**:
    - 명백히 좋음: `auto_accepted` -> 자동 적재 (accept).
    - 명백히 나쁨 (완전 무관, 극단적 스팸/보일러플레이트, 완전 중복, 심각한 오류): `auto_held` -> 자동 보류 (삭제 아님, 복구 가능).
    - 애매함 (경계선 관련성, 저품질 의심, near-duplicate): **`review_needed` -> 지식 도서관에 적재하되 '검토 필요' 플래그 부착** (좋은 문서가 누락되는 False Negative 원천 방지).
- **최소 연결 지점**:
  - `src/ttakkari/collector/pipeline.py`: `_decide`에서 mission goal 및 다축 triage 평가기를 호출하여 판정 사유 및 신호 기록. `_build_envelope`에서 `envelope["extensions"]["collector:triage"]` 및 `envelope["quality"]["relevance"]` 축 규격 충족 반영.
  - `src/ttakkari/collector/missions.py`: `view` 응답에 `triage` 집계 블록 (`accepted`, `review_needed`, `held`, `reasons`, `policy`) 및 각 결과 항목의 `triage` 상세 노출. `unhold_item` 함수로 보류된 문서를 수동 복구(accepted 전환 및 CollectorOutbox export 큐 등록).
  - `src/ttakkari/api/collection_missions.py`: `POST /api/assistant/collect/missions/{mission_id}/items/{item_id}/unhold` 엔드포인트 추가.
- **독립 평가 및 재현 검증 (`scripts/eval_collection_triage.py`, `unit_tests/fixtures/triage_eval_dataset.json`, `unit_tests/test_collection_triage_eval.py`)**:
  - **정정(독립검증 반영)**: 아래 35건은 규칙을 만든 같은 작업에서 쓴 fixture이고 라벨 출처·인간 검수 기록이 없다(`triage_eval_dataset.PROVENANCE.md`). 같은 규칙을 이 데이터로 평가한 수치는 순환이라 성능 근거가 아니며 규칙 회귀 기대값으로만 쓴다. 외부 라벨 평가는 아래 "C4 후속"에 있다.
  - 35건의 fixture 데이터셋(스팸, 404, 보일러플레이트, 중복, 무관 토픽 포함).
  - 측정 결과(규칙 회귀 기준, 순환):
    - 총 샘플 수: 35건 (좋음 13, 애매 8, 나쁨 14)
    - 좋은 문서 오판 보류율 (False Negative Rate): **0.00%** (0/13 - 좋은 문서를 잘못 버리지 않는 보수적 선별 목표 100% 충족)
    - 좋은 문서 적재 재현율 (Good Recall): **100.00%** (13/13)
    - 나쁜 문서 자동 보류율 (Bad Hold Recall): **100.00%** (14/14)
    - 수집 적재 정밀도 (Ingest Precision): **100.00%**
    - 애매 문서 검토필요 식별율: **37.50%**
    - 문서당 평균 처리 시간: **0.053 ms / doc** (로컬 CPU, 외부 API 호출 0회)
- **테스트 및 코드 품질**:
  - `PYTHONPATH=src:unit_tests ../back/.venv/bin/python -m pytest -q unit_tests/test_collection_triage.py unit_tests/test_collection_triage_eval.py unit_tests/test_collection_mission_triage_unhold.py unit_tests/test_pipeline_triage_integration.py ...` -> **100 passed, 4953 subtests passed in 2.33s**.
  - `ruff check`, `ruff format` clean.

### C4 후속: 실제 경량 ML 단계(2단계)와 정밀평가 대기(3단계) (2026-10-25)

독립검증(GPT Luna)이 지적한 "규칙만 있고 ML이 아님", "shadow ML이 판정에 안 쓰임", "정밀평가 단계 없음", "평가가 순환" 네 가지를 이 절에서 다룬다.

#### 후보 사전 평가 (공식 model card / API 확인, 2026-10-25)
| 후보 | 라이선스 | 크기 | 한국어 | 학습 라벨 | 판단 |
|---|---|---|---|---|---|
| [FineWeb-Edu classifier](https://huggingface.co/HuggingFaceFW/fineweb-edu-classifier) | Apache-2.0 | 0.1B (arctic-embed-m + 회귀 헤드) | 영어 전용 | LLM(Llama-3-70B) 주석, 자체 F1 0.71 | 탈락. 한국어 미지원, PyTorch 신규 의존성 |
| [JQL Edu Heads](https://huggingface.co/JQL-AI/JQL-Edu-Heads) | Apache-2.0 | arctic-embed-m-v2.0 + 헤드(.ckpt) | 명시된 35개 언어에 한국어 없음 | LLM 3종 주석 | 보류. 한국어 미명시, PyTorch 필요. 구조(동결 임베딩 + 경량 헤드)는 차용 |
| [FineWeb-HQ classifiers](https://huggingface.co/epfml/FineWeb-HQ-Classifiers) (FineWeb2-HQ) | Apache-2.0 | 언어별 .pt | 20개 언어(태그 기준)에 한국어 없음 | 고품질 기준 문서 대비 분류(논문 arXiv 2502.10361, 세부 확인 필요) | 탈락. 한국어 없음 |
| [DCLM fastText OH-ELI5](https://huggingface.co/mlfoundations/fasttext-oh-eli5) | MIT | 약 2.39GB(.bin, HF 헤더 x-linked-size) | 영어 학습, 한국어 미검증 | OpenHermes+ELI5 대 웹 대리 라벨 | 탈락. 품질 라벨이 아니라 출처 대리, fasttext 신규 의존성 |
| 웹스팸 분류기 | - | - | - | - | 미평가. HF에서 공식 근거를 찾지 못함(검색 'webspam' 0건). 확인 필요 |
| [multilingual-e5-large](https://huggingface.co/intfloat/multilingual-e5-large) (fastembed ONNX) | MIT | 2.24GB | 아래 실측 | 임베딩 모델(라벨 아님) | **채택.** 저장소가 이미 회의록/KB 벡터에 쓰는 모델과 같은 snapshot(ac6781cd), 격리된 자식 프로세스 경로 존재, 신규 의존성 0 |
| paraphrase-multilingual-MiniLM-L12-v2 | Apache-2.0 | 0.22GB | 아래 실측 | 임베딩 모델 | 실측 열세로 탈락 |
| 기존 따까리 TF-IDF/logistic (`learning.py`) | 저장소 | 1024차원 해시 | 언어 무관 | 소유자 리뷰(운영 데이터 필요) | **폐기하지 않음.** 같은 분할에서 기준선으로 측정, 역할은 소유자 리뷰 기반 shadow 유지 |

#### 구현
- `triage_ml.py`: e5-large 임베딩 1회로 축을 따로 계산한다. 하나의 점수로 합치지 않는다.
  - `topic_relevance_semantic`: cosine(목표 `query:`, 제목+도입부 `passage:` 1500자). 임계 band는 보정용 질의 50개에서만 정했다.
  - `information_value`: ridge 헤드(0~4). FineWeb-C 한국어 사람 주석으로 학습. **정보 축일 뿐 판정을 막지 않는다.**
  - `low_quality_likelihood`: logistic 헤드. FineWeb-C의 사람 'Problematic Content' 표시로 학습. 확률은 보정되지 않아 `probability_uncalibrated`로만 표기.
  - `explanation_quality`: 라벨 데이터가 없어 `unmeasured`. 규칙 기반 구조 신호만 존재(`content_rules`).
  - `precise_eval`(정밀평가 필요성): 점수가 아니라 불확실성 사유 목록(`lexical_semantic_disagreement`, `semantic_boundary_band`, `ml_flags_low_quality_but_rules_pass`, `stage2_unavailable_or_unvalidated`).
- 헤드 artifact(`triage_ml_artifact.json`)에는 모델 버전, 임베딩 snapshot, 검증 상태, 라벨 출처, 지표가 함께 들어 있고 digest가 맞지 않으면 쓰지 않는다. 검증 상태는 코드가 사전에 정한 기준(held-out AUC 0.75, 부트스트랩 하한 0.60, 양성 15건 이상. 관련성은 0.80/0.70)으로 결정한다. 사람이 손으로 `validated`를 쓰지 않는다.
- 판정 반영(`triage._apply_stage2`): `validated`이고 한국어이고 snapshot이 일치할 때만 결정에 영향을 준다. 영향도 한 방향이다.
  - 어휘 규칙의 '무관 보류'를 의미 유사도가 반박하면 보류에서 검토 필요로 구제한다.
  - 어휘 관련성이 낮아도 의미 유사도가 high band 이상이면 `review:low_relevance`를 걷어낸다.
  - 의미 유사도가 low 미만이거나 문제 콘텐츠 가능성이 높으면 적재하되 검토 필요로 표시한다.
  - **ML 단독으로 보류하지 않는다.** 오래됨, 짧음, AI 작성, 관심사 다름, 중복을 품질 실패로 쓰는 축이 없다(중복은 기존 novelty 규칙 그대로).
  - 영어 등 미검증 언어, snapshot 불일치, 미검증 artifact는 기록만 하고 규칙이 단독으로 결정한다.
- 실행 위치: 기존 `embedder_worker` 자식 프로세스(오프라인, 부모 감시). 임무 job의 페이지마다 목표+문서를 한 번에 임베딩하고 DB 트랜잭션 밖에서 돈다. API 프로세스에는 모델이 올라가지 않는다. 실패는 문서별 `unavailable`로 기록하고 규칙이 보수적으로 결정한다(수집 계속). `collector_triage_ml_enabled`와 `context_embedder=e5`가 모두 맞아야 켜진다.
- 노출: envelope `extensions["collector:triage"]`에 축별 값, 모델 id/revision, `validation`, 사유, `stage2`(상태, 효과, 비용), `precise_eval`. 계약 축은 `relevance`(의미 유사도, 없으면 어휘), `education`(ordinal), `spam`(raw_logit). 계약에 검증 상태 필드가 없어 `evidence`에 `validation=...`를 적었다. 임무 view는 항목별 같은 필드와 집계(`triage.ml.documents_by_status`, `triage.ml.axes_validation`, `triage.precise_eval`)를 준다.

#### 외부 라벨 평가 (순환 아님, 실제 실행 결과)
명령: `PYTHONPATH=src ../back/.venv/bin/python scripts/eval_triage_ml.py --cache-dir /tmp/c4/cache --models-dir /tmp/c4/models --write-artifact --measure-cost` (원본: `unit_tests/fixtures/triage_ml/eval_report.json`)

라벨 출처
- 교육적 가치/문제 콘텐츠: FineWeb-C `kor_Hang` 639건. **커뮤니티 사람 주석자**(12명, 문서당 1명), ODC-By 1.0. LLM 라벨이 아님. 주석자 간 일치도 알 수 없음. 분포 None 254, Minimal 216, Basic 83, Good 20, Excellent 5, Problematic 61.
- 관련성: Mr. TyDi 한국어 train(질의 150개, 질의당 정답 1 + BM25 음성 2). TyDi QA 사람 판정. 음성에 미주석 정답 가능. **질문-위키 문단 대리 평가**이며 임무 목표-웹 문서와 분포가 다르다.
- 분할: seed 20261025, 70/30 층화. 시험 분할은 lambda, 임계값, 학습에 쓰지 않음. 관련성은 질의 50개로 band를 정하고 나머지 100개로만 측정.

| 대상 | 방법 | held-out AUC (95% 부트스트랩) | 비고 |
|---|---|---|---|
| 정보 가치(Basic 이상, n=173, 양성 32) | **e5-large 헤드** | **0.777** (0.694~0.851) | Spearman 0.506. 임계(train p25)에서 정밀도 0.42, 재현율 0.50 |
| | MiniLM 헤드 | 0.670 (0.565~0.769) | |
| | 기존 해시 char TF-IDF/logistic | 0.748 (0.663~0.834) | e5와 신뢰구간 겹침. 유의한 차이 아님 |
| | 길이만 | 0.704 | 규칙 `content_rules`는 0.581 |
| 문제 콘텐츠(n=191, 양성 18) | **e5-large 헤드** | **0.848** (0.731~0.945) | 임계에서 정밀도 0.62, 재현율 0.44 |
| | MiniLM 헤드 | 0.713 (0.585~0.831) | |
| | 기존 TF-IDF/logistic | 0.724 (0.567~0.857) | |
| | 규칙 품질 축(역) | 0.524 | |
| 관련성(100 질의, 300쌍) | **e5-large 코사인** | **0.949** (0.922~0.970) | 정답이 음성 둘을 모두 이김 96% |
| | 현재 어휘 규칙 | 0.743 (0.681~0.802) | 같은 조건 65% |
| | MiniLM 코사인 | 0.788 (0.728~0.840) | 79% |

사전 기준으로 e5 관련성, 정보 가치, 문제 콘텐츠 헤드 모두 `validated`(한국어). 다만 아래 한계가 따른다.

비용(Mac Studio CPU, ONNX 2스레드, 자식 프로세스 실측)
- 5문서 페이지: wall 2.9~3.1초(로드 0.6초 + 추론 2.0초), 자식 peak RSS 약 2.1GB, 종료 후 해제. warm 문서당 약 0.34초.
- 실제 `evaluate_documents` 4문서 경로: 2.47초, 문서당 618ms(로드 1.08초 포함).
- 규칙 단계는 여전히 문서당 약 0.05ms.

#### 3단계: 정밀평가 대기 (구현 상태)
- 저장소에는 호출 예산 장부(`budget.py`)만 있고 상한을 강제하는 LLM engine이 없다(`collector_teacher_enabled=False`, 호출처 없음). **그래서 고성능 모델 호출은 구현하지 않았고, 가짜 호출도 없다.**
- 구현한 것: 2단계 불확실/불일치 문서만 `signals.triage.precise_eval.state="pending"`으로 영속하고 사유를 남긴다. job당 대기열 상한(`collector_triage_precise_queue_cap`, 기본 25)을 넘으면 `skipped_cap`으로 두되 문서는 검토 필요로 적재한다. engine 상태는 항상 `unavailable`로 기록한다. 임무 view가 pending/skipped_cap 개수와 상한, engine 상태를 보여 준다.
- 남은 일: 상한을 강제하는 engine을 연결해 pending을 소비하고 결과를 기록하는 단계.

#### 4단계: 적재/보류/복구
- 자동 적재, 검토 필요 적재(좋은 문서 누락 방지), 자동 보류(영구 삭제 아님, `unhold_item` 복구)는 그대로다. 보류는 규칙에서만 나온다.

#### 검증 (실행 결과)
- `PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_collection_missions.py tests/test_collector_jobs.py tests/test_collector_pipeline.py tests/test_collector_fetch.py tests/test_collector_product_flow.py unit_tests/test_collection_adapters.py unit_tests/test_collector_policy.py unit_tests/test_collector_parser.py unit_tests/test_collection_triage*.py unit_tests/test_pipeline_triage_integration.py unit_tests/test_collection_mission_triage_unhold.py tests/test_collection_mission_stage2.py` -> `240 passed, 66 subtests passed`.
- 새 테스트: `unit_tests/test_collection_triage_ml.py` 20건(축 분리, 언어/snapshot/artifact 미검증 처리, 구제/검토/보류 불가, 실패 격리, artifact 무결성), `tests/test_collection_mission_stage2.py` 3건(DB 임무 E2E: 구제, 대기열 상한, ML 비활성 시 규칙 단독).
- `ruff check`: 변경 파일 clean. 저장소 전체에는 이 작업과 무관한 기존 지적이 남아 있다(19건, 변경 파일 아님).

#### 한계 (정직하게)
1. 정보 가치 헤드 AUC 0.78은 약하다. 문서 수 639, 문서당 주석자 1명, 시험 양성 32건이라 신뢰구간이 넓다. 판정 근거가 아닌 정보 축으로만 쓴다. 해시 TF-IDF와 통계적으로 구분되지 않는다.
2. 문제 콘텐츠 헤드는 임계에서 정밀도 0.62/재현율 0.44이며 시험 양성이 18건이다. '검토 필요' 표시용이다. FineWeb-C의 문제 콘텐츠 정의가 따까리의 저품질 정의와 같다고 보증하지 않는다. 실제 임무 문서의 기저율은 다르다.
3. 관련성 band(low 0.820, high 0.857)는 질문-위키 문단 쌍에서 정했다. 임무 목표-웹 문서 분포에서는 이동할 수 있다. 실측 예: 주제가 가까운 '데이터베이스 락' 글이 cos 0.8196으로 low 바로 아래여서 구제되지 않고 어휘 규칙대로 보류됐다(복구 가능). 운영 리뷰 라벨이 쌓이면 재보정해야 한다.
4. `validated`는 한국어에만 붙는다. 영어 등은 기록만 하고 규칙이 단독 결정한다. e5 자체는 다국어지만 외부 라벨 검증을 하지 않았다.
5. 설명 품질 축은 라벨이 없어 미측정이다. 사실성(factuality)도 미측정이다.
6. 규칙 축(어휘 관련성, 내용 규칙, 새로움)은 여전히 외부 라벨 평가가 없는 휴리스틱이며 `validation=unvalidated`로 표기한다. 따라서 전체 상태는 `partial` 이상이 되지 않는다.
7. 운영 환경의 실제 모델 파일 존재와 지연은 이 Run에서 별도로 확인하지 않았다(보호 영역). 평가와 비용은 `/tmp`에 받은 같은 snapshot(ac6781cd)으로 측정했다. 임무 job에서 모델이 없으면 `unavailable`로 기록되고 규칙으로 계속 수집한다. 운영 배포 뒤 임무 view의 `triage.ml.documents_by_status`로 실동작을 확인해야 한다.
8. 해시 임베더 환경(테스트)에서는 2단계가 자동으로 꺼진다(`disabled`).

### C4 후속 2: 3단계 정밀평가(Bounded Precise Evaluation) 소비 경로 및 복구 E2E (2026-10-25)

2단계에서 남겨진 `signals.triage.precise_eval.state="pending"` 대기열을 실제로 안전하게 소비하는 Stage 3 정밀평가 경로를 연결했다.

#### 구현 내용
- **모듈 신설 (`src/ttakkari/collector/precise_eval.py`)**:
  - `LOCAL_QWEN_ENGINE`: `EngineBound` 규격을 엄격히 충족(`enforces_input_cap=True`, `enforces_output_cap=True`, `returns_usage=True`, `micro_usd_per_1k_input=0`, `micro_usd_per_1k_output=0`).
  - 예산 장부(`budget.py`) 연동: `budget.guarded_call`을 통해 `worst_case` 사전 예약 -> provider 호출 -> provider reported usage 기반 `settle` 절차를 준수한다. 예산 거부(`BudgetDenied`) 시 문서를 안전하게 보류하고 `state="budget_denied"`를 기록하며, 예외/타임아웃 발생 시 예약 상태를 `unknown`으로 유지해 보수성을 지킨다.
  - 로컬 모델 경로 재사용: 기존 `runs/desk_qwen.py`, `runs/desk_qwen_server.py`, `runs/qwen_slot.py` 계약을 재사용한다. loopback llama-server의 `/v1/chat/completions` 엔드포인트를 호출하며, `qwen_slot.is_process_lock_busy()`를 검사하여 Mac-wide 단일 Qwen 슬롯을 우회하거나 경쟁하지 않는다.
  - 신뢰할 수 없는 문서(Untrusted Input) 안전성:
    - 외부 웹 데이터에 포함될 수 있는 시스템 오버라이드/탈옥 시도를 방어하기 위해 시스템 프롬프트에서 도구 실행을 전면 차단하고 텍스트를 순수 데이터로만 취급한다.
    - OpenAI 호환 `response_format`의 `json_schema` strict 모드를 강제하여 `topic_relevance`, `relevance_explanation`, `information_depth`, `low_quality_signals`, `recommendation`, `confidence` 필드만 반환받는다.
    - malformed JSON 응답이나 필드 누락 시 안전하게 실패 처리하며, 원문 파기나 권리 변경을 유발하지 않는다.
  - 좋은 자료의 손실 없는 복구 (Rescue Logic):
    - 어휘 규칙의 오판으로 `held`(자동 보류)되었으나 정밀평가 결과 `recommendation="accept"`(고관련성·고정보가치·결함 없음)로 확인된 문서는 `status="accepted"`로 복구된다.
    - 복구 시 `reason_codes`에 `review:precise_eval_rescued` 및 `review_needed`가 부여되며, 지식 도서관으로 전달될 수 있도록 `CollectorOutbox` delivery row가 같은 트랜잭션에서 등록된다.
  - 투명한 엔진 상태 노출 (`engine_status`):
    - `collector_teacher_enabled=False` 또는 `desk_qwen_enabled=False`인 경우 `unavailable` 사유 명시.
    - 서버 미기동 시 `endpoint not ready`, 슬롯 점유 시 `busy`를 명확히 구분하여 노출하며, 미연결 상태를 '통과'로 위장하지 않는다.
- **파이프라인 연동 (`src/ttakkari/collector/pipeline.py`)**:
  - `_admit_precise`: `precise_eval.engine_status()`를 기록하여 대기열 진입 시점의 실제 엔진 상태를 보존.
  - `_finalize_page`: API 프로세스가 아닌 collector worker의 durable page commit 트랜잭션 내부에서 `consume_prepared_precise_eval`을 호출한다. 엔진이 `ready`일 때만 평가를 수행하고, 엔진 비가용 시 `pending`으로 안전 유지된다.
- **임무 상세 및 관측 (`src/ttakkari/collector/missions.py`)**:
  - `view`의 `precise_counts` 집계에 `pending`, `skipped_cap`, `evaluated`, `failed`, `budget_denied` 상태를 모두 포함.
  - 임무 상세 응답의 `triage.precise_eval` 블록에 상태별 카운트와 함께 현재 `engine` 상세(`status`, `detail`, `model` 등)를 노출.

#### 검증 및 실행 근거
- **신규 및 회귀 단위 테스트**:
  - `unit_tests/test_collection_precise_eval.py`: 10 passed in 0.37s.
    - `test_engine_bound_contract`: EngineBound 4대 조건 및 bounded 속성 검증.
    - `test_engine_status_reports_reasons_transparently`: 5가지 상태(teacher off, desk off, no endpoint, slot busy, ready) 투명 노출 검증.
    - `test_untrusted_prompt_escaping_and_input_cap`: 입력 토큰 상한 및 프롬프트 인젝션 텍스트 격리 검증.
    - `test_qwen_api_structured_output_success`: MockTransport를 통한 strict JSON schema 및 usage receipt 정상 반환 검증.
    - `test_qwen_api_malformed_json_raises`: malformed 응답에 대한 MalformedEvaluatorOutput 예외 검증.
    - `test_apply_precise_result_rescues_held_item`: held -> accepted 복구 및 review 사유/영수증 반영 검증.
    - `test_apply_precise_result_retains_held_on_negative_verdict`: 부정 판정 시 원문 보존 및 held 유지 검증.
    - `test_consume_prepared_precise_eval_when_engine_not_ready`: 미연결 시 0 소비 및 pending 유지 검증.
    - `test_consume_prepared_precise_eval_success_rescues_held_and_emits_outbox`: 복구 시 OutboxItem 생성 검증.
    - `test_consume_prepared_precise_eval_budget_denied_updates_state_gracefully`: 예산 거부 시 budget_denied 상태 전이 및 중단 검증.
  - `unit_tests/test_pipeline_triage_integration.py`: `test_envelope_with_precise_eval_evaluated_stays_contract_valid` 추가 및 5 passed in 0.34s.
  - 관련 전체 단위 테스트: `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_collection_precise_eval.py unit_tests/test_pipeline_triage_integration.py unit_tests/test_collection_triage_ml.py unit_tests/test_collection_triage.py unit_tests/test_collection_triage_eval.py unit_tests/test_collection_adapters.py unit_tests/test_collector_policy.py unit_tests/test_collector_parser.py unit_tests/test_collection_plan.py` -> **105 passed, 66 subtests passed in 1.05s**.
  - `ruff check`: 변경 파일 전체 clean (`All checks passed!`).
  - `ruff format`: 변경 파일 포맷팅 적용.
- **미실행/한계**:
  - `tests/test_collection_mission_stage2.py` 등 DB 통합 테스트: 미실행 (`antigravity 샌드박스 환경 내 loopback 소켓 연결 차단 PermissionError: [Errno 1] Operation not permitted`). 호스트 자체의 결함이 아니며 단위 테스트 격리 Mock으로 기능 검증 완료.
  - 운영 llama-server 실제 호출 smoke: 현재 무인 샌드박스 환경에서는 loopback 소켓 접근이 차단되어 있으므로 실제 네트워크/소켓 호출은 격리 httpx.MockTransport로 검증. 실제 모델 추론은 백엔드 배포 및 권한 환경에서 수행 필요.

