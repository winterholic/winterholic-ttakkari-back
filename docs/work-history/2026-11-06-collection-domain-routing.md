# 로보틱스·AI 하네스 수집의 UI 분야 오인 교정

## 관측과 원인

- 운영 revision: `19d99b3f4d845af8c213a20ccea8df007450eac9` (`host_operation_start(runtime_report)`, 2026-11-06 08:15Z).
- 로보틱스 임무 `ec71b61e-19fa-4d46-a5f0-3ab468447c23`: 원 지시문은 로보틱스·피지컬 AI이나 저장된 분야는 interaction/components/enterprise이고 검색어 모두 디자인시스템이다. 적재 1,493건. 실제 결과 샘플에 Carbon Design System, Design System Tactics 등이 있다.
- 하네스 임무 `64299d2f-a776-4437-921e-124a7b859ebd`: hierarchy/responsive/interaction/components/tokens/ia로 오인. 적재 1,467건.
- 이전 `topics_in`은 부분 문자열만 비교한다. 실제 로보틱스 지시문은 interaction/motion/feedback, form(Transformer 등), erp(Hyperparameter)로 세 UI 분야에 매치된다. 세 분야 이상이면 UI 맥락 없이 `catalog.plan_parts`를 선택해 원 주제를 버린다. 하네스도 hierarchy/adaptive/feedback/form/토큰/navigation/taxonomy로 같은 오류가 발생한다.
- 실제 지시문 두 건을 fixture로 보존하고 수정 전 회귀 시험에서 오인 4건을 확인했다. 추가로 Bootstrap 통계·네이버/토스 금융 요청의 오인도 재현했다.

## 변경

- 분야 단어의 영문 경계를 확인하고 단순 복수형·accessibility를 보존한다.
- UI 분야 수만으로 카탈로그를 선택하지 않는다. UI 명시 맥락 또는 분명한 디자인시스템 이름이 있어야 한다. 통계 용어·기업 이름만으로 UI 대상을 선택하지 않는다.
- 긴 연구 지시문의 제목에서 검색 주제를 추출하고 백과사전 구축 지시서 같은 작업 문구를 제거한다. 원 지시문 전체와 요청한 평가·안전 등의 조사 축은 유지한다.
- 저장된 옛 오인 계획의 `rerun`은 부작용 전에 422로 거부한다. 원 지시문으로 새 임무를 시작하도록 안내하며 원래 계획·회차는 보존한다.

## 검증

- `PYTHONPATH=src:. <기존 개발 venv>/bin/python -m pytest -q unit_tests/test_collection_domain_routing.py unit_tests/test_collection_plan.py unit_tests/test_collection_research.py tests/test_collection_catalog_plan.py tests/test_collection_research_flow.py -k 'not script_shell_page'`: **55 passed, 1 deselected**, 임시 PostgreSQL DB 사용. 원 지시문 두 건·오인 반례·기존 디자인 임무·오인 계획 재실행 차단·비브라우저 연구 흐름 포함.
- 최초 통합 검사에서 기존 SPA 실브라우저 검사 1건은 `counts.imported == 1`에 실제 0으로 실패했다. 이 Codex 명령 환경의 수집 renderer 단계 관측이며 원인 미확인이다. 브라우저 실행을 반복하지 않았고 관련 없는 검사는 분리해 수행했다. 운영 브라우저 실패로 일반화하지 않는다.
- `git diff --check` 통과. 배포는 독립 컨트롤러의 검사·결과를 별도로 확인한다.

## 운영 조치와 남은 작업

- 위 오인 임무 두 건에 인증된 `POST /api/assistant/collect/missions/{id}/cancel {}` 수행. 응답은 모두 `cancelled`, 적재 수는 각각 1,493·1,467로 보존됨. KB 문서 삭제·재분류는 하지 않았다. 개별 문서가 모두 주제 밖인지는 미검증이다.
- 수정 revision 배포 후 원 지시문 그대로, `request_key=scope-repair:<원 임무 ID>`를 지정해 새 임무를 생성한다. 기존 이력과 새 커버리지를 섞지 않기 위한 별도 회차다. 입력은 fixtures/collect_robotics_goal.md 및 collect_harness_goal.md, 기본 예산 2,000쪽·깊이 3을 유지한다.
- 운영 계획 preview가 UI 질의를 생성하지 않는지 확인하고, 실제 수집 출처·적재·도서관 검색까지 검증해야 전체 완료다. 배포 예약만으로 완료 처리하지 않는다.

## 후속 운영 검증과 추가 교정 (09:11Z~)

- 첫 수정 `3e40791c`는 08:50Z에 활성화됐다. 08:58Z runtime_report와 인증된 plan preview에서 두 원문의 주제가 유지되고 UI 검색어가 사라진 것을 확인했다. 배포 `3ce99f0a`는 당시 draining이며 이전 세대 정리는 별개다.
- 복구 임무: 로보틱스 `c0307b64-0205-48a6-a10e-595ee2dd523a`, 하네스 `202305ec-a584-4998-87dd-c7e47cbc5384`. 생성 영수증은 `reports/collector/domain-recovery-20261106.json`.
- 09:11Z 실제 검증은 불합격했다. 로보틱스 acquired 145/imported 33, 하네스 imported 0. 두 준비 작업 모두 `policy_url is not a valid https URL: scheme_not_allowed`로 3회 소진했다. 로보틱스 trace에 Ubuntu·Git·관광 프롬프톤·태그 목록 등 무관한 후속 글도 확인했다. 두 복구 임무도 공식 cancel API로 중지했으며, 적재 33/0과 모든 이력을 보존했다.
- 라이선스 원인: `paper.license_from_url`은 HTTP Creative Commons URL을 허용 라이선스로 판정하지만 HTTP 원문을 그대로 반환했다. `register_document_license`는 HTTPS 정책 URL만 허용하고 `paper_phase`는 그 등록 오류를 격리하지 않아 전체 준비 트랜잭션이 되돌아갔다. 동일 호출 경로와 오류를 임시 DB 테스트로 재현했다.
- 탐색 원인: 검색 결과로 선택한 블로그 글 한 편도 max_depth 3과 여러 페이지 예산을 받아 같은 호스트의 모든 링크를 따라갔다. 검색 결과 웹페이지는 max_pages 1/max_depth 0으로 제한했다. 직접 지정한 URL과 카탈로그 루트는 기존 탐색 범위를 유지한다. 옛 검색 출처를 재실행할 때도 넓은 요청과 과거 캐시 frontier를 되살리지 않는다.
- 라이선스 교정: 인식된 Creative Commons URL만 HTTPS 형태로 사용하고 `reported_url`에 공급자 원문을 보존한다. 임의 출판사 URL을 바꾸거나 권한을 추정하지 않는다. 개별 문서의 정책 등록이 거부되면 savepoint로 격리하고 `paper_policy_invalid` gap과 서지 정보만 남겨 다른 논문 준비를 계속한다.
- 추가 재현 검사는 수정 전 실패를 확인했다. 변경 후 국소·임시 DB 검사 **101 passed, 1 deselected**, ruff 및 diff 검사 통과. 제외한 기존 SPA 실브라우저 검사는 위 기록과 동일하게 이 환경에서 미해결이다. 직접 지정한 URL의 링크 탐색과 옛 검색 출처 재실행의 캐시 격리도 별도로 확인한다.
- 다음 실행: 추가 수정본 배포 후 원래 지시문으로 `request_key=scope-repair-v2:<최초 임무 ID>`를 사용해 새 복구 임무 두 건을 시작한다. 실제 주제에 맞는 적재 문서와 KB 검색을 확인하기 전에는 목표를 완료로 표시하지 않는다.

## 배포 검사 교착 교정 (10:28Z~)

- 추가 수정 `d68947942060c1024c8ce9035e579f1751cb7612`의 배포 `ecf6536c`는 TTS의 최종 `drain_ready` 기대값 실패로 중단됐다. 같은 SHA 재검증 `d2f0f91e-8a2c-4e51-a45b-d3f4ac889a30`도 실패했으나 이번 원인은 다르다. 공식 `ops_deploy_log(errors)`에서 테스트 초기화의 `TRUNCATE credentials, ctx_sources, engine_accounts`와 다른 연결 간 락 순환을 확인했다. **703 passed, 2 skipped, 1 error / 108.40s**이며 이후 검사는 실행되지 않았다.
- 10:28Z `host_operation_start(runtime_report)`의 운영 활성 revision은 여전히 `3e40791c6a3890511ac8284c3858bc584c890089`다. 새 복구 임무는 만들지 않았다. 네 임무는 cancelled이며 첫 복구 로보틱스 적재는 당시 134건으로 관측됐다(09:11Z의 33건은 과거 시점 값). 자료는 보존됐다.
- 테스트 fixture 원인: `remote_api`가 종료 시 전역 소비자를 다시 시작하지만 다음 시험의 `reset_database`는 소비자 pump와 이미 실행 중인 일을 기다리지 않았다. 전체 테이블을 읽고 TRUNCATE하는 트랜잭션에 동시 접근이 겹칠 수 있다. 정확히 어느 pump가 위 배포의 상대 연결이었는지는 미확인이다.
- 임시 PostgreSQL에서 소비자가 credentials 읽기 락을 유지한 채 초기화가 들어가는 회귀 검사를 먼저 실행했다. 변경 전 `LockNotAvailableError: canceling statement due to lock timeout` 및 `TRUNCATE credentials, engine_accounts`에서 **1 failed**를 확인했다. 이후 회귀 검사는 고정 지연 대신 claim 중지 관측 이벤트로 락 해제를 동기화했다.
- `tests/conftest.py`에서 초기화 동안 claim을 잠시 멈추고 진행 중 pump와 작업을 기다린다. 대기 상한을 넘으면 초기화를 진행하지 않고 검사에 실패한다. 종료 시 전역 소비자와 KB 워커의 원래 pause 상태를 각각 복원한다. 네 조합 중 한 복원 오류도 검사에서 검출하고 교정했다. 제품 코드·검사 게이트·TTS 단언 조건은 변경하지 않았다.
- TTS 실패는 이 환경에서 재현되지 않았다. 기존 실패 단언에 실제 ready 상태를 출력하도록 진단만 보강했다. TTS 문제까지 해결했다고 주장하지 않는다.
- 최종 국소 검증: `PYTHONPATH=src:. .venv/bin/python -m pytest -q tests/test_fixture_reset.py tests/test_background_consumers.py tests/test_tts_queue.py tests/test_kb_v3_fixes_realdb.py tests/test_kb_v3_integration_flow.py --timeout=60 --tb=short` → **111 passed / 24.21s**. 변경 3개 Python 파일 ruff와 `git diff --check`도 통과했다.
- 다음 실행은 이 교정과 기존 수집 수정을 함께 배포하여 전체 게이트 결과를 확인하는 것이다. 활성화 후 위 `scope-repair-v2` 생성·실제 적재·KB 검색 검증을 이어간다. TTS가 다시 실패하면 새 진단 상태로 원인을 추적한다.

## 수정본 활성화와 두 번째 복구 (11:01Z~)

- 배포 `a0af482c-ba0c-420a-a4cc-8cd351d2eee9`는 전체 pytest 100% 이후 활성화됐고, 공식 `ops_deploy_log`의 11:01:38Z 상태는 **succeeded**, error=null이다. `host_operation_start(runtime_report)` 영수증 `9082ea55-cf91-4a3b-a0d7-cf7ddcadd310`에서 현재 API와 active generation의 revision이 모두 `c396370f15a491b33a64805254636f33c4e1bfce`인 것을 확인했다. 이전 TTS·fixture 실패가 이번 전체 게이트에서는 재발하지 않았으나, TTS 과거 실패의 근본 원인을 확정한 것은 아니다.
- 인증된 `~/bin/ttk post /api/assistant/collect/missions/plan`에 두 fixture 전문과 2,000쪽·깊이 3을 전달했다. sources=[] 및 원 주제 검색어 존재, 디자인시스템 질의 없음 assertions가 두 건 모두 통과했다.
- 같은 원문을 공식 create API로 생성했다. 로보틱스 `aa88050a-43a6-4912-bbb8-aad55927e295`, 하네스 `5faff33d-3586-4827-b3a3-71390acb834a`. 각각 `scope-repair-v2:<최초 임무 ID>` 요청키를 사용했으며 둘 다 running/run_seq 1이다. 다시 생성하지 않는다. 생성·preview·첫 detail/trace는 `reports/collector/domain-recovery-v2-20261106.json.gz`에 보존했다.
- 첫 관측에서 로보틱스는 관련 질의 4회와 출처 후보 31개, mission_prepare running/후속 30개 queued, failed 0이다. 후보에는 Physical AI 논문과 관련 글이 있다. 하네스는 mission_prepare queued다. 둘 다 imported 0으로 실제 적재·검색 검증은 아직 실행할 문서가 없다. 후보 URL만으로 본문 관련성이나 후속 글 유입 방지를 보증하지 않는다.
- 다음 확인에서는 두 새 임무의 실제 적재 문서와 준비 작업 종료 상태를 읽고, 웹 출처 한 페이지 제한 및 문서별 KB 검색/인용 근거를 검증한다. 과거 네 임무와 자료는 보존한다.

## 두 번째 복구 준비 진행 (11:13Z)

- 인증된 `~/bin/ttk get /api/assistant/collect/missions/{id}`와 `/trace`를 두 임무에 실행했다. 로보틱스는 **mission_prepare succeeded/error=null**, 검색 10회·출처 107개·논문 서지 9개(전문 허용 2개/서지 전용 7개), 후속 intake 78개 queued다. 하네스는 mission_prepare running/error=null, 검색 10회·출처 421개·intake 351개 queued다. trace의 failed jobs는 둘 다 0이다. 영수증의 observations 배열에 이번 관측을 보존했다.
- 실제 acquired/imported는 둘 다 0이며 문서가 없어 KB 검색과 인용 검증은 아직 미실행이다. 로보틱스 라이선스 처리 오류가 이번 준비 실행에서는 재발하지 않았으나 하네스 준비 종료와 실제 적재 검증은 남았다. source_policy_required·paper_rights_unregistered gap은 권한 미확인 자료를 보수적으로 제외한 기록이며 권한을 확대하지 않았다.
- `/api/assistant/collect/runtime` 실제 응답은 loop_available=true, paused=false다. 일반 임의 작업 소비는 false지만 관리 출처 루프는 활성이다. `collector/runner.py:consume_once`는 claim 1건 뒤 run_one을 await하는 순차 처리다. 하네스 준비가 진행되는 동안 intake가 대기하는 것은 이 코드와 일치하며, 멈춤이나 운영 장애로 확정하지 않았다.
- 다음 실행은 준비 종료·후속 intake 진행과 imported 문서를 확인한 뒤 document_id별 kb_search/kb_evidence를 수행한다. 두 새 임무를 다시 생성하거나 기존 통과 검사를 반복하지 않는다.

## 로보틱스 실제 적재·검색 검증 (11:29Z~)

- 인증된 detail/trace 재확인: 로보틱스 acquired 16/accepted 13/imported 11/pending_delivery 1, item failed 2/blocked 4, failed job 0이다. 두 실패 item은 네이버 블로그 URL의 HTTP 200/empty_extraction이며, 접근 제한 자료도 trace에 남는다. 로보틱스·하네스 두 준비 작업은 모두 succeeded/error=null로 끝났고 하네스는 intake 351개 queued/imported 0이다. 실제 수집은 계속 진행 중이다.
- 관측한 로보틱스 적재 11건 전부의 URL을 계획의 출처 URL과 비교하여 일치(assertions 통과)했다. 예전 Ubuntu·Git·관광·태그 목록·Carbon 제목은 이 관측 집합에서 발견하지 않았다. 현재 관측한 11건의 범위이며 향후 수집 전체나 각 문서의 사실 정확성을 보증하지 않는다.
- 새 문서 `b893055c-63c0-456d-be5d-00b354f018f2`(MSU Physical AI survey)를 `kb_search(query="physical AI", mode="hybrid", document_id=...)`로 검색했다. lexical/vector 양쪽 경로와 본문 passage를 반환했다. `kb_evidence(passage_id="1124726d-effe-42d4-bf39-5226a9a560ab", start=0, end=500)`는 hash_verified=true이며 지각·추론·행동을 수행하는 물리 시스템 내용으로 주제 관련성을 확인했다.
- 새 문서 `1823570b-92c0-4d5c-9880-b44ebc63fd48`(한국어 피지컬 AI 글)도 문서 제한 hybrid 검색 및 `kb_evidence(passage_id="3d945761-5cd6-474a-b530-41cf71d35909", start=0, end=350)`에서 hash_verified=true를 확인했다. 로봇·자율주행·물류 로보틱스 활용 본문이므로 디자인시스템 혼입이 아닌 원 주제의 적재·검색 경로를 확인했다. 해시 일치는 저장된 인용 바이트의 검증이며 본문 주장 자체의 참을 뜻하지 않는다.
- 검색·인용 영수증은 `reports/collector/domain-recovery-v2-kb-20261106.json`. 하네스의 실제 적재·검색 검증이 남아 있으므로 전체 목표는 active를 유지한다.

## 복구 운영 검증 판정 (12:01Z~)

- 인증된 detail/trace에서 로보틱스 imported **56건**, 하네스 imported **8건**을 확인했다. 두 prepare는 succeeded/error=null, 두 trace의 failed job은 0이다. 수집 자체는 running이며 백과사전 전체 구축 완료를 뜻하지 않는다. 로보틱스 retry_wait 1건은 robots_unreachable/fetch_deferred로 사유가 기록돼 있다.
- 하네스 새 문서 `67bcee59-4a87-4483-8d8b-38c0736357d7` 및 `33dad6b5-d69a-402c-a862-26dc0cafd0c1`을 document_id로 제한해 `kb_search(query="Harness Engineering", mode="hybrid")`를 실행했다. lexical/vector 경로의 본문 passage를 반환했고, `kb_evidence`의 `28bf1972-8140-47e9-85c0-1584b8a758fc`/`1504a35c-db6a-42af-b1eb-bed5ec443083` 두 인용 모두 hash_verified=true다. 실제 내용은 AI 에이전트의 실행 환경·도구·규칙·피드백 루프 설계로 원 주제와 관련된다. 영수증은 `reports/collector/domain-recovery-v2-harness-kb-20261106.json`.
- 하네스 적재 8건의 URL은 전부 선택한 plan.sources와 일치한다. 로보틱스 56건은 55건이 직접 일치하고 TechTarget 1건의 final_url만 달랐다. 최초 단순 URL 동등성 검사 1건 실패를 혼입으로 단정하지 않고 공식 item API의 requested url/final_url/HTTP 200과 실제 HTTP HEAD 최종 URL로 리디렉션을 검증했다. 리디렉션을 포함한 재검사는 robotics 56/harness 8 모두 통과했으며 unexpected follow-up URLs=0이다. item 영수증은 `reports/collector/domain-recovery-v2-url-20261106.json`. 이 판정은 현재 관측 문서 집합에 한정한다.
- 생성·계획·관측 전체 JSON은 반복 본문으로 14.8MB가 되어 `domain-recovery-v2-20261106.json.gz`로 무손실 압축했다. 압축 해제 바이트가 원본과 동일함을 assert한 뒤 압축본으로 보존했다. 과거 임무·KB 데이터는 삭제하지 않았다.
- 원인 규명, 주제 판정 교정과 회귀, 추가 운영 결함 교정 배포, 두 원문 복구 임무 생성 및 실제 적재·검색·관련성 검증이 충족됐다. **혼입 결함 교정과 운영 복구 검증은 완료**이며 새 수집 임무는 계속 실행한다. 원 지시문이 요구한 모든 기술 분야의 백과사전 완성·전문가 품질 검증은 이 복구 작업의 완료 범위가 아니다.
