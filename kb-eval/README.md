# KB-E0 오프라인 평가 하네스

현재 범위는 **KB-E0 착수 조건부 승인, 제품검색 미승인**이다. 실행 adapter는 고정 prediction fixture이며 인덱스·검색 알고리즘·모델 서버를 만들거나 실행하지 않는다. `exit=0`은 fixture 실행을 끝냈다는 뜻이다. `final_quality=not_authorized`를 제품 통과·사용자 승인으로 바꾸지 않는다.

```sh
uv sync --frozen
.venv/bin/python -m ttakkari.kb_eval schema --out /tmp/kb-e0-schema.json
.venv/bin/python -m ttakkari.kb_eval run kb-eval/fixtures/oracle.json --out /tmp/kb-e0-run --limit 1
# 위 명령은 의도한 중단으로 exit=3. 같은 입력과 --out으로 다시 실행한다.
.venv/bin/python -m ttakkari.kb_eval run kb-eval/fixtures/oracle.json --out /tmp/kb-e0-run
.venv/bin/python -m ttakkari.kb_eval run kb-eval/fixtures/oracle.json --out /tmp/kb-e0-run
.venv/bin/python -m ttakkari.kb_eval compare /tmp/kb-e0-run /tmp/kb-e0-run --out /tmp/kb-e0-comparison.json
.venv/bin/python -m pytest unit_tests/kb_eval -q
```

`--limit`은 이번 호출에서 새로 생성할 cache record 수다. 기존 record는 모두 검증한다. `exit=2`는 machine-readable 실패이며 run 출력의 `failure.json`에 분류·사유가 남는다. 실패 기록은 이후 성공해도 보존한다. JSON schema는 Pydantic strict 모델에서 만든다. 알 수 없는 설정은 오류다. `core.py`는 계산·anchor·군집·타이머·캐시·재개 인터페이스를 제공한다.

## 입력 고정과 권리

fixture는 이 단계에서 직접 작성한 3질문·10문서 합성 자료다. 공개 자료를 내려받지 않았다. code/data/qrels/corpus 권리의 원문 위치·버전·확인 시각·저장/변형/재배포 허용은 별개다. 미확인 저장 권리는 loader가 거부한다. 입력의 `status=verified`, URL 문법·응답, `review_evidence`의 id/version/hash, 확인자 주장은 모두 서술 정보일 뿐 권리 권한을 만들지 않는다. 인증된 권리 검토 flow가 없으므로 `kind`가 fixture가 아닌 입력은 loader와 runner 양쪽에서 `rights_review_authentication_required`로 hold한다. fixture는 네 권리 모두 `provenance_kind=synthetic`, `fixture://` 원문, review evidence 없음일 때만 실행하며 결과에 `rights_authorization.authority=none`을 남긴다. E1a의 실제 공개 자료 loader/권리 검토를 끝낸 뜻이 아니다.

manifest는 source revision/raw·body·quote SHA-256/URI/observed time, corpus/gold/policy/config hash, candidate code SHA, build, model/analyzer hash, quantization, seed, window/role/N을 고정한다. 합성 model hash의 `000…`는 **실제 모델 없음의 fixture 표식**이다. fixture의 candidate code SHA는 기반 revision이며, 실행 하네스는 별도 `runtime.harness_source_sha256`와 Python/NumPy/Pydantic 버전으로 고정한다. 실제 전달 commit은 작업 기록/receipt에서 확인한다. config hash에는 fixture prediction byte의 정규 JSON도 들어간다.

같은 원문 hash는 자동 연결한다. 근사 중복·번역·공통 원전은 명시적 links가 필요하다. relevance passage마다 manifest에서 소유 `doc_key`를 resolve한다. positive gold 소유 문서가 선언 `documents`에 없으면 `gold_document_mismatch`로 거부하고, 소유 문서를 split graph에도 넣는다. 질문의 다중 문서, positive gold 소유 문서, 선언 group, 모든 link의 연결 component로 split을 검사한다. 미등록 근사 중복이 없다고 증명하지 않는다. `exposure.split_overlap=unverified`는 검사 통과로 승격되지 않는다. 관측 export는 event time/receipt time과 revision을 보존하지만 DB 진위·스냅샷 격리·동시성을 입증하지 않는다.

## 지표와 통계의 판정 범위

- Recall/nDCG/MRR 및 후보 Recall은 native passage 단위다. 등급 0–3, gain `2^grade-1`, 할인 `log2(rank+1)`을 쓴다. 답 없음은 검색 Recall의 0점으로 섞지 않는다. 반환된 미판정 항목은 정답으로 가산하지 않고 judged@k와 제한을 드러낸다. semantic support를 자동 판정하지 않는다.
- answer-level Recall은 `answer_recall`에서 인접 passage의 최소 cover set을 하나의 answer로 센다. native passage 수치와 별도 이름이다. 기존 native qrels는 외부 라벨 provenance다. anchor remap은 자체 gold의 정체성을 passage ID로 바꾸지 않는다. 전체 custom answer의 등급·확정 품질 gate는 E1b/KB5 이후에 실자료로 닫아야 한다.
- invalid/ambiguous/실행 실패·pending을 모두 노출한다. 모든 답 있음 질문을 분모로 한 Recall 하한/상한도 낸다. 필수 anchor 하나라도 무효면 `input_invalid`다. source revision/hash/time이 달라지면 원문에 같은 quote가 있어도 무효다. 반복 인용은 문맥으로 유일화되지 않으면 ambiguous. NFKC-only match는 좌표를 추측하지 않고 `normalization_review_required`로 보수적으로 차단한다. 정규화 좌표 mapping은 미구현이다.
- false strong 분모는 답 없음 또는 명시적으로 오답 판정된 질문, false abstain 분모는 답 있음 질문이다. correctness 미판정, 각 분모와 이항/군집 CI를 보인다. 이항 CI의 질문 독립성은 가정이며 군집 자료에 보증으로 쓰지 않는다. 언어쌍 40+40/near-miss/충분한 power를 확보하지 않았으므로 abstain gate는 inconclusive다.
- 질문 차이의 문서 연결 군집을 10,000회 bootstrap한다. 불균등 군집은 resample 내 질문 합/질문 수로 계산한다. 1차 Recall/nDCG family는 Δ=.05, family α=.05, Holm p, 보수적 Bonferroni tail=.025 CI다. 유형별 작은 셀은 보고용이다.
- dev joint residual의 nested cluster simulation으로 중립 변화의 통과 확률과 −.10 손실의 통과 확률, Monte Carlo Wilson CI를 보고한다. dev/eval 군집 크기 설계가 다르거나 group이 겹치면 power를 적용하지 않는다. 다른 planned sample-size 설계로 외삽하는 기능은 없다. 최소 200 trials/10,000 bootstrap·20 effective clusters·비영 분산은 **이번 구현의 보수적인 잠정 guard**이며 설계상의 표본 충분성 보증이 아니다. 독립 검증 대상이다. 표본·상관·대표성이 미확인인 실제 corpus의 power는 여전히 미확인이다. 재중심화 residual의 지원범위는 관측 [-1,1] 밖으로 나갈 수 있으며 clipping하지 않는 조건부 pilot 모형이다.
- `quality_family`의 `pass`는 해당 입력·통계 비교의 비열등 판정만 뜻한다. runner/compare는 사람 holdout을 실행하지 않고 최종 승인을 만들지 않는다. `compare`는 선언 treatment 밖의 변경과 corpus/gold/policy/seed 불일치를 거부한다. treatment의 사전등록·검토자 인증 자체는 외부 trust boundary이며 현재 미구현이다.

## 계측·재현·재개

AB/BA 순서는 고정 seed로 균형 무작위화한다. `timed_call`은 admission wait, service, serialize/transport, e2e를 나누고 model queue/tokenize/prefill/inference/DB pool/execute 미관측은 null+사유다. 병렬 stage p95를 더하지 않는다. `latency_report`는 paired block bootstrap p95 ratio CI와 full800/fast200ms 절대 SLO를 따로 낸다. 높은 부하에서도 절대 실패를 유지한다. 입력 block은 strict schema로 검사한다. bool·NaN·inf·음수·0 duration·결측 field·미등록 weight는 판정 전에 거부한다. 순서/부하/profile 불균형·100쌍/10분 미달, 사전등록 최소치(`kb-eval/statistical-policy.json`, 10,000회) 미만 bootstrap, 퇴화 trace는 모두 relative_unverified다. 퇴화는 정확한 값 일치가 아니라 사전등록한 하한으로 판정한다. arm별 e2e 표준편차가 `max(timer 해상도 1µs, 1e-4 × 평균 e2e)` 미만이면 `degenerate_arm_variance`다. 절대 하한은 측정 해상도이고(기준 호스트 `perf_counter` 4.17e-8초의 24배), 상대 하한은 0.2 상대 margin의 1/2000이라 평균 크기와 무관하게 적용된다. ratio CI 폭이 0.2 margin의 1/400인 5e-4 미만이면 `degenerate_ratio_ci`다. ratio 폭은 원래 스케일과 무관하다. 품질 통계도 같은 경계를 쓴다. metric delta CI 폭이 margin .05의 1/50인 1e-3 미만이면 `degenerate_bootstrap_interval`로 `inconclusive`이고, dev delta 표준편차가 1e-3 미만이면 power는 `insufficient`다. 기준값과 근거는 `statistical-policy.json`의 `degeneracy`에 있으며, 실측 pilot 전의 공학적 하한이다. 하한을 넘는 것은 정보 있는 구간의 필요조건일 뿐 충분조건이 아니다. 실제 query별 delta 표준편차는 보통 0.1 안팎이므로, 하한 근처 pilot에서 나온 `power=established`는 실측 pilot 전에는 승인 근거로 쓰지 않는다. 10,000회는 Monte Carlo 하한일 뿐 coverage·power 충분성 근거가 아니다. 실제 동시성1/4·warm/cold·워밍업/3회 반복·10분 서버 부하 pilot은 미실행이다.

RSS·CPU time·run artifact 디스크는 하네스 프로세스만 측정한다. GPU/index disk/swap-in은 collector가 없으면 `value=null,status=unverified`다. GPU collector 주입 interface에서만 실제 측정을 연결할 수 있다. 이 값들을 검색 서버의 자원이나 host 전체 GPU 측정으로 부르지 않는다.

cache key는 원문 입력 hash/길이, corpus/gold/build/model/analyzer/policy/config/code/quantization/seed/role/window와 하네스 runtime을 포함한다. record checksum과 기대 key, 후보/점수 bytes/순서를 정확 비교한다. 완료한 deterministic metric/anchor artifact도 비교한다. 첫 완료는 기준 기록이며 이후 동일 run 재실행에서 metric 정확 일치를 검증한다. timestamp/checkpoint/timer/resource는 schema의 비교 제외 목록에 있다. 실제 추론의 cosine≤1e-4/logit≤1e-3·near-tie 정책은 초기 미검증 정책이다. `live_tolerance`는 합성 점수 검산만 하며 실제 추론 또는 질의 평균 overlap≥.99를 인증하지 않는다. HNSW rebuild는 미실행이다.

fsync→atomic rename→directory fsync로 cache/manifest/checkpoint를 남긴다. checkpoint가 유실돼도 cache를 다시 검증한다. run writer는 배타 잠금, 라벨 상태는 파일 잠금+낙관 버전으로 동시 저장을 막는다. 로컬 POSIX artifact 골격이며 PG transaction/외부 index crash 내구성 증거가 아니다.

## 모바일 계약과 미실행 표면

`review_api.create_router`는 제품 app에 mount하지 않은 FastAPI 골격이다. GET state, GET item(질문/원문 URI/quote/후보 pool), POST event(승인/수정/폐기, label_version, history/cursor)를 ASGI TestClient로 검증한다. agent/LLM 표시를 분리하고 **human write는 인증 adapter가 없어서 501로 거부**한다. 실제 사람 확인을 생성하지 않는다. `open_holdout`은 인증된 reviewer flow가 없으므로 actor/role 문자열과 무관하게 `reviewer_authentication_required`로 거부하고 파일·디렉터리·audit 상태를 바꾸지 않는다. 거부 호출이 향후 정당한 개봉을 막지 않는다. agent/LLM review event의 actor는 `actor_authentication=unauthenticated_claim`으로 기록한다. 실제 hidden holdout은 제공·개봉하지 않았다.

실제 DB manifest/관측 fixture 실행은 알려진 Codex command sandbox의 initdb `shmget Operation not permitted` 제한으로 미실행이다. 같은 initdb/다른 binary·engine·보안설정 재시도는 하지 않는다. 실제 DB snapshot/export adapter와 DB 검증이 남았다. 320/390px 화면·브라우저 검증은 새 UI를 만들지 않아 미실행이다. 이 두 표면을 포함한 E0 완료조건과 독립 검증이 남아 있으며 다음 KB 단위·엔진 선택·collector 계약 변경·배포는 하지 않는다.

## 테스트 보호 점검 (mutation)

`.venv/bin/python kb-eval/mutation/run_mutation.py [--rev REV]`는 판정 이유 생성 코드(`reasons.append`, `"reasons": [...]`)와 `mutants.py`에 적은 조건·가드 변이를 하나씩 넣고 E0 단위 테스트를 돌린다. 작업 트리는 바꾸지 않는다. 변이마다 subset을 임시 디렉터리에 복사하고, 비어 있는 `PYTHONPYCACHEPREFIX`로 실행하며, 실제로 그 복사본이 import됐는지 plugin으로 확인한다. 같은 크기·같은 초의 mtime을 가진 연속 변이가 이전 `.pyc`를 재사용해 결과가 틀렸던 문제를 막기 위해서다. `expect="kill"` 변이가 살아남으면 exit 1이다. `expect="equivalent"`는 동등하거나 반례를 만들 수 없는 변이로, 근거를 함께 적고 보고만 한다.

## 남은 위험 (독립 재검증 2회차 낮음 지적, 미수정)

- 로컬 artifact는 서명하지 않는다. cache/manifest/checkpoint의 checksum과 replay key는 우발적 손상과 입력 불일치를 잡는다. 하지만 같은 파일 시스템에 쓸 수 있는 행위자가 checksum까지 다시 계산한 위조는 막지 못한다.
- 인증된 권리 검토 flow가 없어서 fixture가 아닌 입력은 모두 hold한다. 따라서 E1a(실제 공개 자료)는 권리 검토 flow와 그 독립 검증이 있어야 시작할 수 있다. 그때 runner의 public 결과 제한(`public_regression_only`)도 테스트와 함께 다시 넣어야 한다. 지금은 도달할 수 없는 분기라서 제거했다.
