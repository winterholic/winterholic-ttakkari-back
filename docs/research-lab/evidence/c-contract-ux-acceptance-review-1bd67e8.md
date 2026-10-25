# Research Lab v2 C 계약·저장 복원 독립 검증

검증일 2026-10-26. 대상은 backend `1bd67e820db686846ab7775795b7f6ae2c1ed212`, frontend `5a2650fde8d5c46f50c9f08ce67f14917f14da98` 고정 revision이다. 두 worktree는 시작 시 clean이었다. 제품 코드와 기존 테스트 소스는 수정하지 않았다.

## 판정

| 범위 | 판정 | 근거·한계 |
|---|---|---|
| F1/F2 k shadow 판정, `validate`와 생성 일치 | PASS | 실제 ASGI+PostgreSQL pytest 15개 통과. fixed `top_k`와 `top_k` sweep 동시 존재여도 retrieval 전략에서 k는 계속 무시되므로 거부가 맞다. `single`/`repeat`, `external_report`의 동명 k 필드는 차단하지 않는다. |
| F3 해석 노트 계약 | API 계약 PASS / UI 왕복 미검증 | success/failure 노트 저장·resume의 조건/본문/`unverified` 보존, 클라이언트 지정 `epistemic_status` 거부를 실제 API+PG에서 확인했다. frontend→backend 실제 브라우저 입력·저장·reload는 미검증이다. |
| F4 재사용 설정 | API 경계 PASS / UI 무수정 재사용 미검증 | source adapter 상속 계약을 실제 API+PG에서 확인했다. 원본 k=5 설정의 frontend 복제 payload, 생성 뒤 fresh parameters, 실제 retrieval cutoff·metric 5 및 사용자가 k=7로 바꾼 후속 동작은 브라우저 경로에서 미검증이다. |
| 이전 source ID 계약 | PASS | 필수 configuration ID, 없는 configuration/run의 404, study/experiment mismatch의 422 경계 테스트 통과. 기존 별도 roundtrip/FK/source 4종 검증은 재실행하지 않고 선행 증거를 재사용했다. |
| BestResult 채택→reload 및 파일 보관함 artifact 링크 | 미검증 | 이번 실제 UI 세션에서 인증된 Research Lab 화면에 진입하지 못했다. 기존 C 검토도 이 경로를 실행하지 않았다고 기록한다. |
| UX 10 전체 | 부분 | 아래 대조표 참고. F5 짧은 배지·monitor 출처 미표시는 이전 합의대로 낮은 잔여 표시 개선이며 Gate 실패로 승격하지 않는다. |

## 실행 환경과 실제 실행

- `TTAKKARI_TEST_DB` URL은 출력하지 않고 identity만 검사했다: host `127.0.0.1`, database `ttakkari_test`, user `ttk_test`, port `50275`.
- 실행 환경은 Codex command tool이다. 격리 DB에 필요한 `vector`, `pg_trgm` extension 및 앱 테이블을 준비했다. 최초 schema 준비는 `vector` 미설치로 실패했고, extension을 만든 뒤 진행했다. 운영 DB를 사용하지 않았다.
- 명령: `scripts/pytest_slots.py --timeout 300 -- .venv/bin/python -m pytest -q tests/test_research_v2_api_contract.py -k 'shadowed_k_sweep or shadowed_cutoff or interpretation_node or reuse_defaults_to_source_adapter or source_reuse_requires_configuration_id or source_reuse_rejects_missing_entities or source_reuse_derives_experiment_and_study or source_reuse_inherits_methods'`
- 출력: `15 passed, 52 deselected in 1.39s`; fixture의 선택 초기화 `15 selective (avg 4.4/115 tables)`.
- 포함 반례에는 `retrieval_eval` grid의 k+top_k sweep 거부, fixed top_k+k+top_k sweep 거부, single/repeat 및 external_report의 동명 필드 허용, source configuration 필수/부재/mismatch, success/failure interpretation 저장·reload가 있다. `epistemic_status=locally_measured`를 요청으로 보내면 422이며 서버가 저장한 값은 `unverified`다.
- 위 API 시험은 실제 FastAPI ASGI와 PostgreSQL을 사용했다. 그러나 frontend 브라우저가 보내는 실제 payload 및 화면 재로드까지 연결한 증거로 확대 해석하지 않는다.

## 브라우저 경로와 미검증 범위

구성된 `ttakkari_browser`로 로컬 Vite를 열어 실제 UI 검증을 시도했다. 브라우저는 실행됐으나 초기 backend 응답에서 dev server origin `127.0.0.1:25174`가 기본 CORS 목록에 없어 API 요청이 차단됐다. 테스트용 CORS middleware를 맞춘 뒤 Vite를 다시 띄웠으나 앱 로그인 화면은 `Mac Studio 응답 중` 상태로 로그인 입력 UI를 내놓지 않았다. `5173`은 이미 사용 중이어서 해당 프로세스는 건드리지 않았다. browser tool로 스크린샷을 저장했지만 인증된 연구 화면을 보지 못했으므로 acceptance evidence로 쓰지 않는다. 검증 종료 시 browser를 닫았다.

따라서 다음 UI 행위는 이 revision에서 독립 재현되지 않았다.

1. 성공 및 실패 결과를 화면에서 해석 입력 → 201 저장 → reload 뒤 조건/본문/미검증 배지 보존.
2. source k=5 설정 재사용 후 원본 복제 및 설정 확인, retrieval 실제 cutoff/metric 5. 이어서 명시적 k=7 입력이 payload·metric에 반영되는지.
3. BestResult를 채택하고 reload 후 채택 설정 유지, 연결 artifact 링크가 파일 보관함 artifact를 여는지.
4. 실제 CPU retrieval 1회 및 원본 설정 불변·기존 source 4종 보존·reload 통합 확인.

잘못된 source ID 계약은 이번 실행에서 해당 경계를 가진 API 테스트를 재검증했다. 구체적으로 configuration이 필수이며 빠지면 422, configuration이 없으면 404, derived study/experiment mismatch는 422, 명시 run이 없으면 404라는 교정 정본을 유지한다.

## UX 10 대조

기준 문서는 `refactor-v2-requirements.md` §5의 UX 10문이다. 이전 독립 검토 `c-reuse-review-763dc03-e1158f0.md`의 실제 브라우저 관측은 출처 설정 복제 화면, 성공/실패 run 출처, 실패 원인 표시, 실패 run 재실행, 후속 실험 출처 reload까지였다. 그 보고서는 해석 저장 F3와 원본 설정 충실도 F4를 실패로 남겼고, BestResult/artifact는 미실행이었다. 이번에는 F3/F4 계약 교정 후 API 경계를 실제 실행해 확인했으나 UI 인수 행위는 확인하지 못했다.

| UX 질문 | 현재 증거 판정 |
|---|---|
| 시도 가능한 방법 / 현재 실행 가능한 방법 | 기존 catalog/adapter 증거의 범위만 유효; 이번 재검증 범위 밖 |
| 이전 시도 / 적용 설정 | 기존 설정·run 출처 UI 증거는 존재; 이번 재사용 fidelity UI는 미검증 |
| 조합 / 개선·악화 폭 | 기존 비교 화면·데이터 증거의 범위만 유효; 이번 재검증 범위 밖 |
| 성공 이유·실패 원인 | 실패 원인 표시와 interpretation 표시의 기존 UI 증거는 있음; 입력 후 실제 저장/reload는 미검증 |
| 재실행 가능한 실험 | 기존 검토에서 실패 run 재실행 경로 PASS; 이번에는 재실행하지 않음 |
| 현재 최고 결과물 | 기존 보고서에 화면 캡처는 기록돼 있으나 BestResult 채택/reload + artifact 파일 링크 관계는 미검증 |
| 이전 연구 재사용 | 기존 출처 badge/reload evidence 및 API 계약 PASS; 새 UI에서 k=5 설정·실제 cutoff 재현은 미검증 |

F5(짧은 출처 배지와 monitor의 출처 미표시)는 기존 합의대로 낮은 잔여 표시 개선이다. 이 결과는 C 계약 교정 및 저장 복원 인수의 UI 완료, 메인 통합, 배포, Metal/A 전체 완료를 뜻하지 않는다.

## 재사용한 기존 증거

- `c-reuse-review-763dc03-e1158f0.md`에서 0072↔0073 migration, source 4종 roundtrip/FK, 11개 reuse 반례, 원본 불변, UX8, 그리고 검증된 UI 출처 배지 경로를 재사용했다. migration을 다시 돌리지 않았다.
- `c-reuse-contract-fix.md`의 frontend 75 pass 및 backend 135 pass 기록은 변경 구현자 기록으로만 참고했다. 본 보고서의 PASS에는 위에서 직접 실행한 15개 ASGI+PG 케이스만 반영했다.
- 실제 Supertonic/Whisper 재추론은 하지 않았다. 기존 `b670fbbd` WAV/실측 증거의 범위는 이 검증이 독립 재실행하지 않은 선행 범위로 유지한다.
- actual Metal 경로는 지시대로 실행하지 않았다.

## 정리

실제 browser test에서 만든 UI 작업물은 없고, 사용자가 소유한 앱·브라우저 프로세스에는 손대지 않았다. 임시 backend와 Vite 프로세스는 검증 종료 후 정리했다. 테스트 격리 DB의 fixture credential은 conftest reset이 생성한 표준 test credential이다.
