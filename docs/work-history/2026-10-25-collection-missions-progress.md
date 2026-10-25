# 수집 플랫폼 임무 중심 리팩토링 — 진행 중

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
