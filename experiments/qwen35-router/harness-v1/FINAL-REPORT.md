# Qwen3.5-4B 접수 라우터 하네스 적용 후 common30-v2 재측정

- 실행: 2026-10-16 KST, Qwen3.5-4B Q4_K_M, llama.cpp b11655 CPU-only(6 thread, ctx 4096, parallel 1), non-thinking, temperature 0, seed 20261015. 고정 30문항 x 3회 = 90건, **하네스 확정 후 한 번만** 실행.
- 결론: hard exact **84/87 = 96.6%** (기존 Qwen 48/87 = 55.2%), acceptable 87/90, 실행 오류 0, 누락·중복 0. p50 957ms, p95 1911ms(기존 2420/3450ms). 남은 오답은 한 문항(common30-11) x 3회뿐이다.
- 주의: 이 수치는 하네스 설계 단계에서 common30 문항 유형(gold_rationale 라벨)을 참고해 만든 dev 세트로 튜닝한 결과다. 평가 문항 원문은 읽지 않았고 few-shot·규칙에도 넣지 않았다. 아래 "오염 방지" 참고.

## 1. 결과 요약 (common30-v2, 90행)

| 항목 | 기존 Qwen 4B | 하네스 Qwen 4B |
|---|---:|---:|
| hard exact | 48/87 = 55.2% | **84/87 = 96.6%** |
| acceptable (90회 허용집합) | 확인 필요(원자료 읽기 차단) | 87/90 = 96.7% |
| 유효 출력 / 실행 오류 | 90/90, 0 | 90/90, 0 |
| 보류(abstain)해야 할 18건 중 답변한 수 | 18 | 3 |
| 맡겨야(delegate) 할 51건 중 오답 | 21(answer/abstain 분리 확인 필요) | 0 |
| answer 18건 중 오답 | 확인 필요 | 0 |
| p50 / p95 지연 | 2421 / 3450ms | 957 / 1911ms |
| 회차별 exact | 확인 필요 | 28/29, 28/29, 28/29 |
| 회차 간 행동이 달라진 문항 | 확인 필요 | 0 |

- 오분류 유형별(하네스): abstain→answer 3건(common30-11 x 3회). 다른 유형(abstain→delegate, delegate→answer/abstain, answer→delegate/abstain)은 0건.
- hold 문항(common30-07, 허용 answer/abstain)은 3회 모두 answer, 허용집합 안이라 acceptable로 계산(hard exact 분모 87에서는 제외).
- 오답 원인(common30-11 추정, 확인 필요): kind 분류가 `board_overview`(작업판 일반 질문)로 나갔고, 이 kind는 근거 인용 검증을 거치지 않는다. 비어 있는 작업판에서 "아직도 일하는 중이야"라고 지어낸 답이 나갔다. 최종 측정 뒤 코드는 고치지 않았다(재측정 금지 원칙). 후속 개선 후보는 6절.
- 회차 변동 0은 temperature 0, 고정 seed, grammar 제약, 프롬프트 캐시 덕에 결정적으로 동작한 결과다. 같은 입력이면 같은 출력이라는 뜻이지 일반화 신뢰구간이 아니다.

### 지연
| 경로 | 건수 | 모델 호출 | p50 | p95 |
|---|---:|---:|---:|---:|
| delegate | 51 | 1 | 924ms | 997ms |
| abstain | 15 | 1 | 966ms | 1031ms |
| answer | 24 | 2(분류+응답 생성) | 1793ms | 2115ms |
| 전체 | 90 | 평균 1.27 | 957ms | 1911ms |

- 첫 호출은 16.2초(서버 기동 직후 prefix 전체 prefill, 콜드 스타트). 전체 p95에는 영향이 작지만 최대값은 이것이다. 첫 호출을 뺀 p50/p95는 957/1890ms.
- "하네스로 늘어난 지연"은 answer 경로에서만 생긴다(호출 2회). 전체로는 오히려 줄었다. 이유: (1) delegate·abstain은 모델이 약 13토큰짜리 kind JSON만 내고, 안내 문구는 코드 템플릿이다. 기존에는 reply까지 자유 생성했다. (2) system+few-shot은 매번 같아 llama-server 프롬프트 캐시가 먹는다(prompt 토큰 205,230 중 194,241 = 94.6% 캐시).
- 측정 중 호스트 loadavg 5.5~7.1(타 프로세스 존재). 기존 측정 당시 부하는 확인 필요. 지연 우열 주장은 참고 수준이다.

## 2. 비교표 (참고: Luna high, Haiku)

| 모델 | hard exact | acceptable | p50 | p95 | 비고 |
|---|---:|---:|---:|---:|---|
| Qwen 4B 하네스(이번) | 84/87 = 96.6% | 87/90 | 957ms | 1911ms | 오류 0 |
| Qwen 4B 기존 | 48/87 = 55.2% | 확인 필요 | 2421ms | 3450ms | abstain 18건 전부 answer |
| Luna high | 76/87 = 87.4% | 79/90 = 87.8% | 4263.5ms | 5573ms | 변동 문항 6개 |
| Haiku | 56/87 = 64.4% | 확인 필요 | 8750ms | 23980ms | 77 응답 + CLI 오류 13건, 필요한 작업 미위임 25/51 |

Luna·Haiku·기존 Qwen 값은 이번 Run에서 다시 계산하지 않았다. 기억 저장소(recall)와 2026-10-16 work-history 문서의 기록값이다. 결과 폴더(outbox)의 원자료 읽기는 가드가 차단해 재검산하지 못했다(`확인 필요`). Luna/Haiku는 호출 경로, effort, 토큰 상한, 오류 처리 조건이 달라 엄밀한 동일 조건 비교가 아니다. 하네스 Qwen은 모델 판단 + 코드 정책 매핑의 조합이므로 "모델 단독" 성능과도 다르다.

## 3. 실험 조건: 바뀐 것과 안 바뀐 것

고정 확인(SHA256, 이번 Run에서 직접 계산):

| 대상 | SHA256 | 기존 기록과 |
|---|---|---|
| common30-v2.jsonl | 37b607c9679bba193f18578256a33f8477a3f36cf30e05283ae3ce32ac95b2b0 | 일치 |
| 기존 system prompt v2 | 850eb694411d25fe0d51c3960fe778a63503a74c022becd8ef14701816b5c1bd | 일치 |
| luna-output-schema.json | d451347a2f19835d61a27fb62667f2dd92164ff181a490481f70d7d2ff7d8e7d | 기록값 없음, origin/main과 동일 |
| 최종 출력 schema(build_response_schema, 정렬 JSON) | a55497f115755d306227f5f71497138b3f04cbed37b03600c6457ffe2c75f1bf | 코드 불변(qwen35_shadow.py 8473488b…, origin/main과 동일) |
| 채점 코드(run_v2.py 기준 exact/acceptable 정의) | a4190ca03c32… | 동일 정의로 재현 |

- 모델 `Qwen3.5-4B-Q4_K_M.gguf`(SHA256 25082a7d…), 서버 `llama-server b11655`(1bf5602a…), 인자(`--device none --gpu-layers 0 --threads 6 --threads-batch 6 --ctx-size 4096 --parallel 1 --jinja`), temperature 0, seed 20261015, `enable_thinking=false`, JSON schema 제약 출력이 기존 4B 측정과 같다.
- 새 system prompt SHA256(하네스): kind 분류용 `d8326292d57c61eddf59caf5ca3ac7805de578c3e3780c59971772d4123b155c`, kind 분류 + few-shot + schema 묶음 `0e5f40714ef72ef6ceb12203ab1aa78dcb0686ffe08dc27690ee7e0cacbaf51a`, 응답 생성용 `f3987845b414c20fe08ad407ad9ce3c8bc3bf4f1a92542b7edb670f479c53e90`, 전체 파일 `harness-prompts.json` `377ba72460e1cccd059d5106c446fe2e7a1ed1eb4c0207420af54afaffbe5f2e`.
- 토큰 상한: 기존은 단일 호출 max_tokens 1024. 하네스는 kind 호출 120, 응답 생성 호출 200. 이유: 출력이 짧은 enum JSON이고 제약 디코딩이라 길게 열 필요가 없다. 실제 최대 completion 토큰은 kind 15, reply 30으로 상한에 닿은 적 없다. prompt 토큰 최대 2245(ctx 4096 이내).
- 최종 출력은 기존 schema(`action, reply, lead_id, model_id, tool_ids, permission_ids, abstain_reason`) 그대로이며 `validate_shadow_output`으로 90건 전부 검증했다. 하네스 내부의 kind/evidence JSON은 모델 중간 산출물이며 최종 schema는 아니다.
- 운영 Luna/Haiku 라우팅, 운영 desk 동작, 모델·양자화·CPU 조건은 건드리지 않았다. 파인튜닝 없음. 운영 백엔드 코드 변경 없음(벤치용 스크립트만), 배포 요청 불필요.

## 4. 하네스 구성 요소

1. **결정 기준 명시화 + 체크리스트**: 기존의 한 줄짜리 정책을 5개 kind(social / board_overview / status / work / unclear)와 판정 순서로 풀었다. work를 가장 먼저 검사해 "일을 시키는 말"이 사교·상태로 새는 것을 막는다.
2. **단계 분해 + 코드 정책 매핑**: 모델은 kind만 고르고 action은 코드가 정한다(work→delegate, social/board_overview→answer, unclear→abstain, status→근거 검증 결과로 answer/abstain).
3. **출력 schema 강제**: kind enum + evidence 문자열을 grammar 제약(JSON schema)으로 강제. 필드 순서가 kind→evidence라 근거를 쓰기 전에 분류부터 한다.
4. **few-shot**: 대화 턴 형식으로 18개(사교, 일, 상태-근거 있음/없음, 지시 주입, 축약어/영어). dev/common30과 별개로 새로 썼다.
5. **근거 인용 검증(grounding)**: status 질문은 모델이 [작업판]/[최근 대화]에서 글자 그대로 인용한 구절이 실제 컨텍스트에 있어야 answer, 없으면 abstain(insufficient_context). 인용이 여러 줄이면 조각마다 원문에 있어야 한다. 모델이 근거를 지어내도 코드가 막는다.
6. **주제어 겹침 검사**: 인용이 있어도 질문과 인용이 속한 줄이 같은 일을 다루는지(2-gram 겹침, 상태 줄의 보일러플레이트 제외)를 본다. 엉뚱한 줄을 인용해 통과하는 경우를 막는다.
7. **응답 문구 분리**: delegate·abstain 안내문은 코드 템플릿, answer만 별도 호출(JSON 제약, 300자)로 생성. 지어낸 약속·상태 보고가 delegate/abstain 응답에 들어갈 수 없다.
8. **프롬프트 캐시 친화 구조**: system+few-shot 접두를 고정해 지연을 줄였다.
- 적용 후 폐기: 다수결(self-consistency)은 구현만 했다가 쓰지 않았다(접두 캐시를 깨 지연이 늘고, dev에서 필요한 오답 유형이 없었음). 사교 사전 정규식 규칙은 코드에 옵션으로만 있고 최종에서 꺼져 있다.

## 5. dev 세트 기준 구성 요소별 효과

dev 세트는 common30 원문과 무관하게 새로 쓴 합성 문항이다(실패 유형 라벨만 참고). 각 문항은 common30 메시지와 문자열 유사도(SequenceMatcher, 3-gram Jaccard)를 자동 검사해, 겹침이 큰 문항은 다시 썼다. few-shot 18개의 최대 유사도도 0.55/0.12 이하.

| 세트 | 성격 | 문항 | 튜닝에 사용 |
|---|---|---:|---|
| dev | common30 실패 유형 재현(사교/상태/위임/주입) | 125(A63/B62) | 예, A로 조정하고 B도 오류를 열람(낙관적) |
| dev2 | 혼합 의도·축약 명령·영어·오타·히스토리 근거·주입 등 어려운 변형 | 81(hold 4 포함, hard 77) | 예, 같은 방식 |
| dev3 | 확정 전 신규 보류 세트(앞선 출력을 보지 않고 작성) | 61(hard 60) | **아니오.** 최종 구성은 dev3 결과로 바꾸지 않았다 |

hard exact (맞은 수/분모):

| 구성 | dev | dev2 | dev3(보류) |
|---|---:|---:|---:|
| H0 기존 프롬프트 그대로 | 77/125 | 36/77 | 42/60 |
| +단계 분해(kind, 코드 매핑) | 94/125 | 46/77 | 48/60 |
| +few-shot 12 | 93/125 | 50/77 | 47/60 |
| +근거 인용 검증 | 121/125 | 71/77 | 57/60 |
| +few-shot 18·evidence 문구 강화(겹침 없음) | 121/125 | 70/77 | 57/60 |
| +줄 단위 겹침 검사 | 123/125 | 70/77 | 미측정 |
| 최종 s7(+분할 인용) | 123/125 | 71/77 | 57/60 |
| 최종에서 few-shot 제거 | 미측정 | 미측정 | 57/60 |

(구성 명칭은 run 요약 파일 이름 기준이다. `dev-runs/*.summary.json`. 일부 구성은 겹침 검사 구현이 중간에 바뀐 버전이라 같은 이름이 아니다.)

효과가 난 곳(dev 세트 기준, 모두 원인은 확인 필요 표시가 없는 직접 관측치):
- **단계 분해**: 위임해야 할 요청이 직접 답변으로 새는 오류가 dev 54건 중 16→0, dev3에서 delegate 오답 5→0. 설명·의견·교정 같은 "모델이 직접 답하고 싶어 하는" 요청을 kind 분류가 일로 인식한다. 지시 주입(규칙 무시 + 일 요청)도 대부분 여기서 정리됐다.
- **근거 인용 검증**: 가장 큰 효과. 근거 없는 상태 질문의 abstain 처리가 dev 0/28 → 26/28, dev2 0/9(상태 다양) → 9/9, dev3 abstain 오답 11→2. 4B 모델은 "모르면 abstain"을 프롬프트로는 거의 따르지 않았다(few-shot만 넣은 구성도 0). 모델 판단을 믿는 대신 인용이 컨텍스트에 있는지 코드가 확인하는 방식이 효과를 냈다.
- **주제어 겹침**: dev에서 +2(엉뚱한 줄 인용 통과 차단), dev2 B에서는 정상 근거 질문을 abstain으로 돌리는 부작용 1~2건이 있었다. 순효과는 작다.
- **few-shot**: 단독으로는 dev3에서 효과 없음(47 vs 48), few-shot 제거 구성도 dev3에서 57로 같았다. 효과는 dev2의 축약 명령·영어·오타 유형에서만 보였다(46→50). 즉 few-shot은 dev3 기준 필수 요소가 아니다. 최종에는 안정성 목적으로 유지했다(확인 필요: 다른 분포에서의 기여).
- **응답 문구 분리**: 정확도가 아니라 안전성·지연 효과. delegate/abstain 응답 문구가 항상 안전한 고정 문장이다.

## 6. 한계와 후속

- common30 하네스 오답: `board_overview`로 분류된 worker 상태 질문(common30-11)이 grounding을 건너뛴다. dev 세트에는 같은 유형이 없어서 못 잡았다. 개선 후보(미검증): board_overview도 답을 작업판 줄 인용으로 확인하거나, 질문에 구체 대상(에이전트·워커·서버 등)이 있으면 status로 재분류. 이 수정은 dev에 같은 유형 문항을 더한 뒤 개발해야 하며, common30에 다시 맞추면 평가 오염이다.
- 평가 일반화: common30은 작업판·대화가 모두 비어 있는 30문항이다. 실제 접수에서는 작업판에 항목이 있고 대화가 길다. dev/dev2/dev3는 비어 있지 않은 컨텍스트도 포함하지만 합성 데이터다. 실로그 분모는 아직 없다(Shadow Mode 결정과 같은 이유로 운영 교체 근거로 쓰지 않는다).
- 정책 의존성: 비어 있는 컨텍스트에서의 상태 질문 = abstain이라는 v2 gold 정책에 맞춘 하네스다. 정책이 바뀌면 코드 매핑을 바꿔야 한다. 일정·메모 조회는 delegate(도구 필요), 에이전트/배포/세션 상태는 abstain이라는 경계도 정책에서 온 것이다.
- 단계 분해의 대가: 모델 호출이 최대 2회(answer). 운영 desk에 넣는다면 시스템 프롬프트 캐시를 유지해야 지연 이득이 유지된다. 운영 반영은 이번 범위가 아니다.
- 지연 측정에 타 프로세스 부하가 섞였다(loadavg 5.5~7.1). 반복 측정 필요.

## 7. 실행·권한 이슈(기록)

- 지정 outbox `370d319d-583d-4f95-a23f-7a8bcb39b776/qwen-harness-common30-v2/` 쓰기 시도(Write 도구)는 ttakkari-guard가 "보호 영역 접근 차단"으로 거부했다. 대신 이 Run에 허용된 outbox `e0dfa47e-3358-49ae-b644-74f4fb9df0d8/qwen-harness-common30-v2/`에 산출물을 두었다. 지정 폴더로의 복사는 사용자가 해야 한다.
- `~/.ttakkari/benchmarks`, 기존 Luna 결과 outbox(`9316389f…`), 기존 4B 원자료 outbox(`2450ff68…`) 읽기도 같은 가드가 차단했다. 같은 명령을 반복하지 않았고 기록값(recall, 저장소 work-history)으로 대체했다. 그래서 기존 4B의 acceptable·오분류 세부와 Luna/Haiku 오분류 유형은 확인 필요다.
- 전체 디스크 `find`는 120초 시간 초과로 백그라운드 이동 후 30분 한도로 중지됐다(결과 무관).
- macOS에서 `sed -i`를 인자 없이 써서 첫 dev2 배치가 실행되지 않았다. 원인 확인 후 재실행했고 측정에는 영향 없다.
- 하네스 서버는 공용 원자 lock(`/private/tmp/ttakkari-apple-inference-bench.lock`) 아래에서만 띄웠고, 소유 PID만 종료했으며 lock은 모두 해제됐다.

## 8. 파일

- `qwen-harness-common30-v2-raw90.jsonl` 원시 90행(모델 호출별 raw 응답, 토큰, 지연, kind, evidence, 근거 검증 결과 포함)
- `qwen-harness-common30-v2-rows.csv` 행 단위 표
- `qwen-harness-common30-v2-summary.json` 요약(혼동행렬, 회차별, SHA256, 토큰, loadavg)
- `harness-prompts.json` 최종 프롬프트·few-shot·schema 전문
- `comparison.md` 비교표 요약
- `code/` 하네스 코드(`harness.py`, `final_run.py`, `with_server.py`), dev 세트와 생성 스크립트(`dev/`), dev 구성별 요약(`dev-runs/`)
- 저장소 사본: `back/experiments/qwen35-router/harness-v1/`
