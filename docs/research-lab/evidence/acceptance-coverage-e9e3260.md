# 원문 UX 10문 인수 대조 및 CPU 조합 실행 (2026-10-26)

제품 기준 backend `e9e32609ab5d5b8dbd5db25581658bbddcd9354a`, frontend `5a2650fde8d5c46f50c9f08ce67f14917f14da98`.
기존 실행 증거를 다시 실행하지 않고 아래 범위로 연결했다. 이 문서는 운영 배포나 전체 목표 완료 판정이 아니다.

## 새로 실행한 범위

Codex 명령 환경에서 표준 Run DB의 URL identity와 실제 SELECT 결과가 `ttakkari_test / ttk_test`임을 단언했다. 제품 ASGI endpoint, 실제 PostgreSQL, 실제 retrieval_eval CPU adapter를 사용했다. 모델 다운로드, GPU 호출, 운영 DB 쓰기는 없었다.

`document_chunking_strategy`와 `hybrid_retrieval`을 같은 실험의 주 방법/비교 방법으로 선택했다. 2×2 grid를 실제 실행하고 각 configuration의 입력과 Run 원문을 새 GET에서 대조했다. 두 방법 ID, 4개 설정, 4개 local_measured succeeded Run, 비교 API의 Pareto 후보 4개를 저장·재조회했다. 별도 repeat 정의에서 조합 설정을 두 번 실행하고 repeat_index 1/2 및 분포 표본 2개를 확인했다. 두 방법을 단순히 선택만 한 증거와 실제 개입이 적용된 증거를 구분한다.

| 검색 방식 | 청크 분할 | 실제 청크 수 | Recall@5 | nDCG@5 |
|---|---|---:|---:|---:|
| BM25 | 없음 | 32 | 0.9167 | 0.9779 |
| Hybrid RRF | 없음 | 32 | 0.9167 | 0.9804 |
| BM25 | 128자, overlap16 | 53 | 0.9167 | 0.9586 |
| Hybrid RRF | 128자, overlap16 | 53 | 0.9167 | 0.9832 |

데이터는 ko_tech_docs_v1의 고정 32문서/16질의다. HashEmbedder 기반 CPU 결과이며 신경망 임베딩이나 일반 검색 품질 향상을 검증한 것이 아니다. 조합의 nDCG 값은 이 fixture의 관측값이고, repeat 2회는 저장 계약 검증이며 통계적 확신이 아니다. 지연 수치는 원시 영수증에 보존했지만 이 짧은 실행으로 속도 우열을 결론내지 않는다.

실행 명령:

```sh
RESEARCH_COMBINATION_RECEIPT=<현재 Run outbox>/research-combination-cpu.json PYTHONPATH=src .venv/bin/python scripts/pytest_slots.py --timeout 60 -- .venv/bin/python -m pytest -q --timeout=30 --tb=short tests/test_research_v2_combination_acceptance.py
```

결과: `1 passed in 1.05s`. `ruff check tests/test_research_v2_combination_acceptance.py` → `All checks passed!`.

검사기의 부정 대조는 제품 파일을 수정하지 않고 임시 pytest plugin에서 `_chunk_text`를 항상 원문 1개를 반환하도록 대체했다. 같은 인수 검사가 `{'none': 32, 'fixed_window': 32}` / `assert 32 > 32`로 실패했다(`1 failed in 0.89s`). 따라서 옵션 이름만 저장하고 실제 분할을 무시하는 반례를 이 검사가 검출한다. plugin은 격리된 `/private/tmp/ttk-research-combination-proof-77c77aa5`에서만 적용했고 제품 코드와 정상 영수증은 바꾸지 않았다.

초기 helper는 validate endpoint에 생성 전용 title/secondary_metrics를 보내 422를 받았다. 스키마에 맞게 helper를 교정한 뒤 위 정상 실행을 얻었다. 부정 대조의 첫 기동은 `/private/tmp` 전체를 PYTHONPATH에 넣어 기존 numbers.py가 표준 numbers를 가리면서 pytest import에서 실패했다. 전용 plugin 디렉터리로 한정한 뒤 위 예상 실패를 얻었다. 두 helper 오류는 제품 결함 또는 인수 실행 성공으로 세지 않는다.

원시 영수증: [combination-cpu-e9e3260.json](combination-cpu-e9e3260.json). 실제 GET 응답, 설정, Run 원문, 질의별 결과, 측정, 비교 응답, product/data SHA256을 포함한다. 4개 source/data 파일은 `git diff --quiet e9e3260 -- <file>` 및 현재 파일 SHA256으로 영수증과 일치함을 확인했다. pytest DB는 테스트별 초기화되는 격리 환경이므로 영수증은 당시 fresh GET의 보존본이다.

## 원문 UX 질문별 현재 증거

| 원문 질문 | 유효한 실행 근거 | 남은 확인 |
|---|---|---|
| 1. 시도 가능한 방법? | remaining-integration-review-a564c05: 실제 방법 검색/필터/분야·계층·설명 UI | 운영 배포 화면 |
| 2. 현재 실행 가능한 방법? | 같은 보고서의 실행 가능/외부 후보 표시와 실행 거절; backend-review-0c37878의 fail-closed 경계 | 새 runtime의 실제 Metal, 운영 표시 |
| 3. 이전 시도? | final-ui-adoption-review-5a2650f: 원본/후속/성공·합성 실패 표시와 reload | 운영 스모크; 합성 실패를 실제 실패로 확대 금지 |
| 4. 적용 설정? | 같은 UI 인수의 k5 충실 복제/k7 명시 변경; 이번 grid의 4설정과 실제 raw 일치 | 운영 화면 |
| 5. 여러 방법 조합? | 이번 ASGI/PG/CPU 2×2 실행·방법 ID 복원·repeat2; 기존 UI 다중 선택/조건 비교 관측 | 이번 조합을 실제 UI에서 읽고 저장·reload하는 최종 확인 |
| 6. 개선/악화 폭? | lead-final-ui-integration-564d497의 동조건 A/B delta0 UI, 이번 grid의 비교 API | 운영 화면; 일반적인 개선 결론은 없음 |
| 7. 성공 이유·실패 원인? | final-ui-adoption의 연구자 해석 저장/reload; runner-registration-atomicity-review-9f087f0의 실제 예외/취소 raw·측정 보존 | 실제 실행 실패의 원문과 해석을 한 화면에서 확인; 자동 원인 설명으로 확대 금지 |
| 8. 재실행 가능한 실험? | c-reuse-review-763dc03-e1158f0의 실패 Run rerun; 이번 repeat2 | 운영 MCP 지속 연구 resume |
| 9. 현재 최고 결과물? | final-ui-adoption의 BestResult 채택/reload + lead-final-ui-integration의 실제 CPU 리포트 UI/보관/content/download SHA 일치 | 운영 화면 |
| 10. 이전 연구 재사용 방법? | final-ui-adoption의 source configuration 연결/k5 무수정/k7 변경·실행·reload | 운영 스모크 |

남은 공통 경로: 기존 controller 예약 `d3711123-da36-43c2-b730-06c23472e036`의 활성 revision 확인 → frontend main/Vercel → 새 runtime 실제 A Metal/RuntimeLease/raw → 운영 MCP 및 위 UI 잔여 → R1~R16/PMS 최종 대조. 이번 1회 배포 조회는 testing/error=null이며 로그는 27%까지다. 진행률은 완료율이 아니다. 같은 예약을 중복 생성하거나 대기 폴링하지 않는다.

PMS 7.1~7.5의 source 재수집, 선택 반례, 장기 owner crash/drain, 500개 초과 탐색, 확대 데이터 acceptance는 관련 v2 기능이 있다는 이유로 완료 처리하지 않는다. 이 문서의 bounded 실행이 그 조건을 대체하지 않는다.

후속 2026-10-26: 위 배포 testing 및 UX5/7 미확인 표시는 당시 상태다. d3711123은 release pytest의 test-method shadow 순서 의존으로 실패했고, 기존 main 교정을 통합한 누적 배포 2f8896ee가 testing이다. 격리 실제 조합 UI 새로고침 및 실제 KeyError/연구자 해석의 UI·DB·resume 저장복원은 [deployment-recovery-ui-237f135.md](deployment-recovery-ui-237f135.md)에서 보완했다. 운영 배포/Metal/MCP 미확인은 계속 유지한다.
