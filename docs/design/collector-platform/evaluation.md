# ML 학습·평가·비용 실험 프로토콜 v1

상태: 설계. 이번에 모델 학습, 사람 라벨, 운영 성능 시험은 하지 않았다. 이전 27문서 실험은 편의표본 스모크다.

## 축과 라벨

| 축 | 라벨/근거 | 혼동 금지 |
|---|---|---|
| education | 설명 깊이·배울 수 있음 0~4+unknown | 사실성·현재 질문 관련성과 다름 |
| utility | 목적별 실용성 0~4 | 짧은 API reference도 유용할 수 있음 |
| spam | 광고/조작/반복/정상/애매함 | 내비게이션·포털 자체는 악성 spam 정답이 아님 |
| factuality | supported/refuted/conflicting/insufficient/not_applicable, 주장별 원출처·시점·버전 | 교사가 잘 썼다고 사실 검증된 것은 아님 |
| relevance | query-document graded 0~3+판정불가 | BEIR/MS MARCO는 이 축의 검색 평가이며 진실 gold가 아님 |
| freshness/provenance/novelty | 발행/수집/효력 시각·원전 계보·중복/추가 주장 | 오래된 참/새 거짓·한 원전 복제·AI 재서술 구별 |

라벨 행에는 document/raw/text hash, source family, 언어, domain, 장르, 길이, 시기, 원전 family, label rubric/version, actor identity/type, blind 여부, 근거, uncertainty, dissent, adjudication, created_at를 넣는다. actor type은 human / independent_ai / teacher / synthetic / rule이고 human_confirmed는 실제 사람 확인 event 없이는 생성 불가다. proposed expected good/bad는 gold가 아니다.

## 표본과 누출 방지

초기 제안 규모는 실제 문서 1,200건(ko/en 각 600), 개발/과학/경제/역사/상식 각 언어·domain 120건을 목표로 한다. 공식문서/논문·연구/뉴스·블로그/참고·실용·포털을 출처·시간·길이(짧은/중간/긴)와 교차 층화하되 불가능 셀은 부족으로 기록한다. 이 수는 통계력 보장이나 이미 수집된 수가 아니다. n 부족 cohort의 운영 자동 탈락은 비활성이다. 고품질 짧은 실용문·비주류 장르·장문 말미 중요정보·한국어 조사/약어/코드 토큰·버전 변화·번역·원전 재인용·인젝션 사례를 포함한다.

source/domain/eTLD+1·문서/원전/근사중복 cluster·번역 pair·synthetic parent family를 group key로 묶고 train 60% / calibration 20% / frozen test 20% 목표로 배분한다. source-heldout과 future-time holdout을 별도 manifest로 보고하며 모든 층을 완벽 균형시켰다고 가정하지 않는다. 동일 원전 번역/AI 변형은 parent split을 따른다. test는 threshold/feature/prompt 튜닝과 synthetic generation의 seed에서 제외한다. source-first query gold는 검색 결과를 보며 역작성하지 않고 원문 passage ID로 고정한다. manifest hash와 중복교차 검사 결과를 남긴다.

초기 검색 제안은 5 domains×2 languages×20질의=200질의이며 도메인/질의형별 CI를 공개한다. 기존 24질의 감사는 dev regression에 남기고 신규 frozen test와 합산하지 않는다. 정본 기준은 원문 revision/hash/span·권리/scope·발화/효력 시각이다. canonical 후보와 verified canonical 관계를 분리하고 삭제/정정/되돌림을 회귀시킨다.

## 독립 판정과 gold 출처

1. 전 문서는 먼저 고정 rubric으로 teacher와 다른 identity의 independent AI가 blind(모델 점수·source 명성·선택 정책 숨김) 평가한다. teacher 생성·평가 prompt/모델 revision을 각각 기록한다. 서로 다른 AI라도 독립 human gold로 부르지 않는다.
2. 사람 2명이 sample을 blind 검토하고 이견을 별도 adjudicator가 근거와 함께 조정하는 경로를 설계한다. 실제 담당/시간 미배정이며 현재 미실행이다. raw agreement·Krippendorff alpha 또는 weighted kappa·분모·CI를 축별 보고한다. 자동 합의도 gold로 조용히 승격하지 않는다.
3. 사람이 없으면 결과 명칭은 `automatic_reference_baseline`이고 human validation=not_executed다. 자동 분류는 shadow/hold routing까지만, 고품질 영구 폐기/사실 확정 승격은 금지한다. 사람 입력을 기다리며 플랫폼 개발을 멈추지는 않는다.
4. factuality는 원출처의 정확한 주장/인용·시점·버전·상충을 확인한 사례만 gold로 삼는다. 공식 도메인이라는 이유만으로 전 문서를 참으로 라벨하지 않는다. FEVER 계열은 고정 corpus의 claim task 참고이며 일반 웹 사실성으로 직접 전이하지 않는다.

## 공개 자산 선택과 보류

2026-10-20 공식 카드/문서를 재확인했다. 이전 보고서의 전체 상용 가능 주장은 채택하지 않는다. 모델 license, 학습 데이터 license, 원문 이용권, 하위셋별 재배포/학습 조건을 각각 registry에 기록한다. 권리 불명 자산은 다운로드/학습/재배포를 해당 용도에서 중지하고 메타 조사만 한다.

| 후보 | 선택 위치·다음 시험 |
|---|---|
| FineWeb-Edu | 영어 education teacher/baseline. 한글 운영 지표로 사용하지 않음. 카드의 회귀 점수 의미만 적용 |
| FineWeb2-HQ-PlusPlus | 다국어 challenger. mfwedu raw logit 유지; mdclm prose softmax vs 단일-logit sigmoid 코드 모순을 registry에 남김. 보정 확률/사실 판정 아님 |
| DCLM fastText | 영어 속도 baseline. 재실측 단독 RSS 약 2.9GB이므로 '경량'을 메모리 의미로 쓰지 않음 |
| NVIDIA DeBERTa | 미실측 보조 challenger. 카드의 High recall .1776 및 언어 범위 명시 부재를 함께 기록. 한국어 실험 없이 배제/지원 단정하지 않음 |
| KOREAN-WEBTEXT | 한국어 filtered corpus/실제 학습 후보. 웹문서 독립 gold로 대체하지 않음 |
| Korean-Human-Judgements | 한국어 instruction response 인간 평가 자료. 웹페이지 품질 라벨과 task가 달라 auxiliary만 |
| AI Hub 71983 | 합성 한국어 모바일 스미싱/피싱 kill-chain 자료. 웹페이지 spam 직접 gold가 아님. 접근/이용 조건 별도 확인 |
| BEIR/MS MARCO | lexical/e5/rerank 관련성 평가. 데이터셋/하위셋 license·원전 시점 기록 후 사용. 사실성 결과로 보고 금지 |
| trafilatura/datatrove | pure parser와 offline dedupe 비교 후보. production 버전·commit/license pin 필요. 신규 scheduler 대체 X |

PlusPlus 카드의 101언어 표시는 확인했으나 Korean-specific 성능을 입증하지 않는다. 직접 맞는 한국어 웹문서 품질·웹스팸 gold는 이번 조사에서 확인 못했다는 범위만 유지한다. 한국어 자산 자체가 없다는 결론은 내리지 않는다.

## teacher → 저비용 student / 기준선

A 규칙 only: 길이·boilerplate·반복·정규표현·출처 메타 + deterministic decision. 목적 불일치와 security block을 별개로 둔다.
B 고전 ML: word/char TF-IDF + logistic regression 또는 linear SVM(스코어 후 별도 calibration). teacher score·source ID·gold label 누출 특징을 제외한다. seed/특징 범위와 code/environment를 고정한다. scikit-learn 연구 venv에서 시작하고 backend request process에 새 대형 패키지를 무조건 추가하지 않는다.
C 공개 모델: 고정 revision의 raw score를 동일 text/window 입력으로 평가한다. 긴 문서는 beginning/middle/end window coverage와 max chunk 수/문서당 CPU 상한을 기록한다. 평균/최저 quantile/전체문서 policy 집계의 차이를 비교하고 앞 512토큰 절단만으로 대표 성능을 주장하지 않는다.
D student: 실제 corpus의 자동 라벨과 제한 synthetic hard case를 학습하되 데이터 출처 종류를 보존한다. 처음엔 synthetic 비중 최대 30%를 실험 가설로 두고 real-only·real+synthetic ablation을 비교한다. 사람 gold와 자동 라벨의 weight는 구분해 validation에서 선택한다. teacher만의 스타일을 암기하는지 source-heldout에서 검사한다.

순서: A/B/C 재현→teacher/student 비교→calibration→ablation→frozen holdout→shadow. 성능·false-negative 이득이 없는 student 학습은 반복하지 않고 단순 baseline 유지. 미확정 사실은 teacher가 창작한 확신으로 채우지 않는다. 합성은 의도적으로 표기된 광고/반복/사실 변조/형식 변형, 원전 기반 정확한 짧은 문서, prompt injection에 사용하며 실제 gold test를 대체하지 않는다.

Calibration은 calibration split만으로 Platt/isotonic/temperature 등 task에 맞는 방법을 비교하고 개별 언어/도메인의 충분한 support가 없으면 pooled calibration 또는 미보정으로 표시한다. raw score·transform·calibrator ID·training split hash를 모두 보존한다. ordinal education 회귀는 MAE/순위 및 구간 reliability를 보고, 임의 sigmoid를 품질 확률로 쓰지 않는다. 이진 spam/high-quality 확률만 정답 라벨 정의·정책·calibration 기준을 명시한다.

학습 manifest는 dataset SHA·source/license snapshot·label/rubric hash·split/seed·code SHA·환경 lock·model revision/tokenizer/preprocess·hyperparameter·CPU threads/device·duration/RSS·metrics/CI·artifact hash를 포함한다. 실험 실행별 `reports/collector/<experiment-id>/`에 predictions.jsonl, exclusions.jsonl, manifest.json, metrics.json, errors.csv, calibration artifact를 둔다. 다운받은 pretrained weights 존재와 학습/운영 성공을 구분한다.

## 평가 지표와 제안 통과선

다음 수치는 운영 승인값이 아니라 최초 실험 가설/제안이다. G0 구조 안전 게이트는 fixture에서 강제하고, G1/G2 품질 수치는 충분한 독립 표본+CI가 없으면 NOT VERIFIED다.

| 게이트/지표 | 산식·증거 | 초기 제안 판단 |
|---|---|---|
| G0 accounting | eligible 입력=success+hold+skip+error+remaining | 100% 설명 가능, hash/span/ACL 오류 0 |
| G0 예산/중복/소유권 | 병렬 예약·retry/unknown·N→10N replay, cross-midnight, generation switch | 절대 예산 초과 0, stale commit/중복 delivery 0 |
| 품질 recall/FNR | high-quality retained / 독립 gold high-quality, FNR=잘못 reject/전체 high-quality | 언어/domain별 recall ≥.95와 one-sided 95% 하한 ≥.90 제안. 표본 부족은 shadow 유지 |
| spam precision/recall | class별 precision/recall/PR-AUC 및 confusion matrix | 자동 차단 precision ≥.98 제안, recall을 숨기지 않음 |
| selective risk/coverage | 보류 제외 error와 자동처리 비율/보류율/복구율 | 보류 감소만으로 개선 주장 금지, domain/길이별 보고 |
| calibration | ECE(bin 정의/n), Brier, reliability plot | ECE ≤.05 및 raw 대비 Brier 비악화 제안. calibrated subset·CI 공개 |
| factuality | claim별 supported/refuted precision/recall·citation entailment·unknown | 수치 미확정. unknown을 wrong/right로 은폐하지 않음 |
| retrieval | 질의 macro nDCG@10, Recall@5/20, MRR·indexed-gold coverage | 동일 corpus/code에서 baseline 비열등, 목표 cohort nDCG@10 +.03 제안, paired bootstrap CI |
| provenance/canonical | 원본→normalized→passage/span→receipt 조인, 오병합/분리·정정 후 상태 | fixture 100%, 독립 운영 표본 전수 대조 미실행 |
| resource | p50/p95/max wall·cold/warm CPU/RSS/device별, queue wait 별도 | local score p95 ≤1s/doc·worker RSS ≤4GiB 제안, 총 batch/time cap |
| LLM/cache | actual input/output/reasoning, reserved/settled/unknown, hit/miss·teacher calls/doc | day/job hard cap 충족, N 증가에도 calls/day 상한 고정 |

문서 bootstrap은 source family 단위, retrieval은 질의/원전 family 단위 paired resampling한다. 자동/사람 gold 결과·도메인·언어·장르·길이·시간 cohort별 n/CI와 원시 오류 목록을 남긴다. 평균만으로 소수 domain 악화를 숨기지 않는다. threshold는 calibration split에서 고정한 뒤 frozen test는 한 번 평가하며 실패 후 재학습은 새 test 버전/교차 시점 holdout으로 구분한다.

Ablation: rule / +exact dedupe / +near-dedupe / +public score / +student / +budgeted teacher / +synthetic / +calibration / +retrieval rerank를 한 항목씩 바꾼다. 동일 manifest에서 false-negative·자동처리율·오류·cost/CPU·retrieval을 함께 비교한다. 다 바꾼 전후 수치를 각 요소 공로로 분배하지 않는다.

## 환경·registry·개선

이전 재실측은 연구 venv의 CPU 경로였고 host GPU benchmark가 아니다. codex sandbox의 Metal 차단은 host Metal 불가를 뜻하지 않는다. 다음 실험은 CPU fallback을 유지하고 GPU가 필요하면 host_gpu MCP 경로에서 시작·status·벤치 결과를 별도 evidence로 남긴다. 모델 로드/추론/학습/재개/MCP를 별개의 체크로 남긴다. back Python과 연구 Python을 섞지 않고 lock 및 device를 manifest에 기록한다. 요청별 child process/공용 자원 슬롯을 STT/E5/TTS 소유자와 조정한다.

registry는 candidate→validated→shadow→champion→retired 상태, artifact+policy/calibrator pointer를 원자적으로 전환한다. 이전 champion 즉시 복원과 재평가 queue를 갖춘다. drift는 source mix·언어·장르·길이·score histogram·hold/복구율·오류 유형 변화를 주별 고정 sample로 비교한다. drift가 감지되면 shadow로 되돌리고 source-first error sampling→추가 라벨→버전 있는 재학습을 실행한다. 자동 daily 대량 teacher labeling으로 되돌아가지 않는다. 빈 표본/수집 실패는 정상 0 drift가 아니다.

## 공식 1차 근거 (2026-10-20 열람)

- [PlusPlus 모델 카드](https://huggingface.co/epfml/FineWeb2-HQ-PlusPlus-Classifier): raw mfwedu와 mdclm prose/code 모순.
- [FineWeb-Edu 모델 카드](https://huggingface.co/HuggingFaceFW/fineweb-edu-classifier): education 학습 목적. 이번 카드 직접 재열람은 sources.json에 표시.
- [NVIDIA 모델 카드](https://huggingface.co/nvidia/quality-classifier-deberta), [KOREAN-WEBTEXT](https://huggingface.co/datasets/HAERAE-HUB/KOREAN-WEBTEXT), [Korean-Human-Judgements](https://huggingface.co/datasets/HAERAE-HUB/Korean-Human-Judgements).
- [AI Hub 71983](https://www.aihub.or.kr/aihubdata/data/view.do?aihubDataSe=extrldata&currMenu=120&dataSetSn=71983&topMenu=100): 합성 kill-chain 자료, 웹문서 직접 라벨 아님.
- [BEIR 공식 저장소](https://github.com/beir-cellar/beir), [MS MARCO 공식 사이트](https://microsoft.github.io/msmarco/): relevance 평가 경계와 이용 조건은 하위셋별 확인.
- [RFC9309](https://www.rfc-editor.org/rfc/rfc9309.html), [HTTPX transports](https://www.python-httpx.org/advanced/transports/), [PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.html), [JSON Schema](https://json-schema.org/draft/2020-12/json-schema-core): robots·transport·DB claim·교환 검증 설계 근거. HTTPX 기본 기능만으로 DNS pinning이 완료됐다는 주장은 하지 않는다.
