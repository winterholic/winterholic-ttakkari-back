# Research Lab v2 카탈로그 Flash 결과 독립 검증

- 대상 구현 커밋: `978961e103d6a54e0e7f936f39bef9c89c66cdb4` (구현자 보고: `0084fc41-993b-4d8f-9d57-dceb36f91dff`, Gemini Flash)
- 검토 기준: `docs/research-lab/refactor-v2.md`, `docs/research-lab/refactor-v2-requirements.md`, 이전 `docs/research-lab/evidence/catalog-review-95bba6e.md`
- 독립 검토 체크아웃 HEAD: `87fec61787a54e604ab5a309eb5a7b1baa07c3c6`; 대상은 HEAD 조상이 아니다. 대상 커밋과 현재 트리의 catalog seed 및 `tests/test_catalog_seed_contract.py`는 동일함을 `git diff 978961e -- ...`로 확인했다. 다른 작업자의 staged API/core/test 변경은 그대로 두었다.
- 범위: 카탈로그 seed, 계약 테스트, 요구사항·기존 A~F 결함 및 정정 주장. 저장소 소스는 수정하지 않았다.

## 판정

**FAIL — 재검증 결함을 일부 반영했지만, evidence·수치·실행 가능성과 지원 호환성은 아직 의미 수준에서 닫히지 않았다.** 구조 총계는 직접 재검산해 112 methods / 185 parameters / 128 relations로 확인했다. 이는 구현자의 숫자 주장을 재사용하지 않은 직접 집계다. 필수 방법론 목록은 요구 정본과 이전 검토 대조상 누락이 보이지 않아 PASS (포함)로 유지한다. 포함은 유효성이나 실행 지원 승인이 아니다.

| 검토 항목 | 결과 | 관찰과 영향 | 조치 |
|---|---|---|---|
| A. 50개 빈 evidence 분류 | **FAIL** | 직접 집계는 40 `widely_practiced`, 10 `unverified`. 새 테스트는 상태가 허용 목록인지, 빈 evidence가 `source_reported`가 아닌지, `unverified_count > 0`인지만 검사한다. 각 방법의 원리 자체가 관행인지 확인하지 않는다. 10개가 있다는 이유로 나머지 40개가 정당화되지 않는다. | 40건을 방법별 검토하고, 근거가 확인되지 않는 원리/주장은 `unverified`; 관행으로 분류할 때는 제한된 중립 원리만 남긴다. |
| B. 28개 정량 주장 정리 | **FAIL** | 기존 전수목록 28개에 대해 새 계약은 `expected_effects`만 `%`와 `배`를 검색한다. `notes`, `side_effects`, `compatibility`, parameter 설명, 관계 note 전역은 검사하지 않는다. 의도적으로 만든 반례: `expected_effects=[]`, 빈 evidence + widely_practiced, `compatibility.notes="KV 50~75% 절감"` 또는 `side_effects="0.37비트 추가 절감"`는 이 테스트의 정량 위반 목록에 들지 않는다. 실제 seed에도 `kv_cache_quantization.expected_effects`의 50~75%, `qlora` parameter description의 0.37 bit 등이 존재하며, 테스트 대상 필드 밖이다. 넓은 숫자 정규식 자체도 %/배 외의 용량·시간·범위 단위를 전수 검출하지 못한다. | 전역 문자열 leaf를 훑되 숫자 설명/권장 파라미터/수치 효과를 구별하고, 각 효과 수치의 source claim 및 조건 결합을 점검하도록 보완한다. 28개 각 항목을 직접 정리한다. |
| C. Kokoro와 주변 참조 | **FAIL** | `tts_kokoro_pipeline_tuning`은 `deprecated`, `adapter_hints=[]`가 맞다. 그러나 `compatibility`에는 Kokoro/PyTorch 및 CPU/CUDA/Metal, `requires`와 선택 가능한 backend parameter가 그대로 남는다. 계약은 자신의 `deprecated`와 adapter 배열만 확인하며 이 runtime/parameter 조합이 loader에서 배제되는지 확인하지 않는다. 따라서 **선언된 deprecated 표식과 빈 adapter는 확인했지만, 실제 loader/readiness에서 승격 불가인지는 미검증**이다. 주변 TTS relation/tag/runtime, STT prompt 기본값 및 시스템 라우팅을 확인했고 일부 역사적 Kokoro 언급이 여전히 런타임/운영 의미와 섞여 있다. | catalog 값만으로 비실행이 보장된다고 승인하지 않는다. loader/readiness의 소비 경로 및 adapter 등록 규칙을 검사해야 한다. historical 표기와 실행 설정/태그를 분리하고 Supertonic 3 운영 엔진과의 참조를 정리한다. |
| D. FlashAttention·QLoRA 논문 수치 및 조건 | **부분 PASS / FAIL** | FlashAttention seed 수치는 원문 초록의 서로 다른 비교 조건(BERT-large seq 512 15% end-to-end wall-clock, GPT-2 seq 1K 3x, LRA seq 1K–4K 2.4x)과 맞게 분리되어 있고 Metal 별도 실측 필요도 적혀 있다. 다만 `compatibility.hardware`와 `runtimes`가 독립 배열이며 유효 조합을 정의하지 않는다. `apple_silicon_metal + vllm` 등 조합을 허용하는 것으로 오독 가능하다. QLoRA의 65B 단일 48GB GPU claim은 논문과 일치하나 seed compatibility는 CUDA/bitsandbytes와 MLX 경로를 교차 매핑하지 않고 혼합 목록으로 둔다. source claim은 QLoRA CUDA kernels를 언급하므로 MLX에 동일 논문 조건이 성립한다는 뜻은 아니다. | 원 논문 결과는 유지 가능하나 특정 paper condition으로 한정한다. 런타임·하드웨어·모델의 허용 tuple을 명시하고 그 외는 미검증으로 둔다. |
| E. KV quantization 및 speculative decoding 조합 | **FAIL** | KV 항목은 `llama.cpp` 및 `vllm`, CPU/Metal/CUDA를 함께 나열하고 FlashAttention을 요구한다. Value cache 제약이 notes에 있지만 backend별 지원 관계를 정의하지 않아 CPU/vLLM 등 지원으로 오독할 수 있다. speculative decoding도 llama.cpp/vLLM 및 CPU/Metal/CUDA를 교차 조합으로 나열하며 실제 draft 경로/버전별 지원과 ngram 대체 경로를 구분하지 않는다. adapter가 받아들이는 설정과의 대조는 수행되지 않았다. | 각 항목에서 모델·runtime·hardware 허용 조합과 버전/필수 기능을 직접 표현한다. 미확인 tuple은 유효 조합으로 노출하지 않는다. |
| F. 계약 테스트의 의미 검증 능력 | **FAIL** | 형식·slug·taxonomy·관계·parameter 계약은 통과한다. 새 테스트도 의미 검사를 표방하지만 위 반례를 놓친다. Kokoro loader/readiness, 수치-근거의 결합, compatibility tuple을 검사하지 않는다. 계약 테스트 green은 콘텐츠 정확성 또는 실행 불가를 입증하지 않는다. | 음성 대조 fixture를 추가해 허용된 좋은 예는 pass, 각 반례는 해당 method/field를 지목해 fail 하는지 확인한다. 콘텐츠 검증과 구조 계약을 별도 결과로 보고한다. |

### 수치 및 원문 대조

- 카탈로그 직접 집계: 112 methods / 185 parameters / 128 relations; empty evidence 50 = 40 widely_practiced + 10 unverified.
- 빈 evidence 전체 문자열을 재귀 탐색했다. 발견 사례에는 단순 표준 형식(Whisper 30초/16kHz), 설정 예시(beam 1~2, prompt 128k), 성능 주장(효과·개선)이 섞여 있다. 숫자 출현만으로 모두 결함 처리하지 않는다. 핵심 결함은 주장 종류·단위·조건·출처를 분류하지 않는 테스트의 사각지대다.
- **FlashAttention**: [원 논문 초록](https://arxiv.org/abs/2205.14135)은 BERT-large seq 512의 15% end-to-end wall-clock, GPT-2 seq 1K의 3x, LRA seq 1K–4K의 2.4x를 각각 보고한다. seed의 수치 분리는 조건과 일치한다. CUDA/GPU 논문 보고는 Metal 성능을 입증하지 않는다. 호환성 pair mapping은 여전히 없다.
- **QLoRA**: [원 논문 초록](https://arxiv.org/abs/2305.14314)은 frozen 4-bit pretrained LM에 LoRA를 학습시키고 단일 48GB GPU에서 65B 모델을 fine-tune한 결과를 보고한다. seed의 이 수치는 부합한다. seed의 `double_quant` 설명 0.37 bit는 해당 논문 주장과 연결된 파라미터 설명이지만, 현재 계약은 그 위치를 검색하지 않는다. CUDA/bitsandbytes와 MLX 지원을 같은 조합으로 일반화할 근거는 별도다.
- **Speculative decoding**: [원 논문 초록](https://arxiv.org/abs/2211.17192)은 T5-XXL, 표준 T5X 구현 대비 2–3x를 보고한다. 모든 열거 runtime/hardware에서 같은 효과라는 증거가 아니다.
- 기존 검토의 나머지 12개 URL 표본 확인은 재사용했다. 이번 검토에서 재조회한 URL은 FlashAttention, QLoRA, speculative decoding 원 논문뿐이다.

## 계약 테스트와 실행 증거

저장소는 `/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2`이다. uv 기본 캐시는 사용하지 않고 기존 venv의 pytest를 사용했다.

```text
./.venv/bin/python -m pytest tests/test_catalog_seed_contract.py -q
.......                                                                  [100%]
7 passed in 0.09s
```

```text
./.venv/bin/python -m pytest tests -k catalog -q
............................                                             [100%]
28 passed, 3077 deselected in 2.91s
db reset: 18 selective (avg 3.2/115 tables), 0 forced full, 0 fell back to full
```

DB fixture가 selective reset한 테스트 하위집합에서 실행되었고 출력상 전체 reset fallback은 0이다. production DB에 연결하거나 수정하지 않았다. 보고된 이전 `uv run pytest tests -k catalog -q` 결과 `1 failed, 9 passed, 1 skipped, 17 errors`는 이번 명령으로 재현되지 않았다. 현재 venv 재실행은 28개 통과다. 실행 환경/오류 로그가 제공되지 않아 과거 17 errors의 구체 원인은 판별 불가이며, 과거 실패를 이번 커밋 회귀 또는 환경 문제 어느 쪽으로도 단정하지 않는다. 계약 테스트는 현재 체크아웃에서 대상 커밋과 같은 seed/test blob임을 확인한 뒤 실행했다.

## 수정 필요 범위와 상태

필수 잔여 수정은 (1) 40개 widely_practiced의 항목별 기술적 근거/중립화, (2) 기존 28개 수치 claim의 전역 field 및 source/조건 결합 검사, (3) Kokoro 실제 loader/readiness 비승격 확인과 주변 관계·tag·runtime 정리, (4) QLoRA/KV quant/FlashAttention/speculative decoding의 명시적 모델-runtime-hardware tuple이다. 이 중 어느 것도 검증 완료로 승격하지 않는다.

문서·검증 소스만 다룬다. 대상 구현 및 현재 HEAD 사이의 기준 분기 때문에 이 결과는 대상 커밋 직접 checkout에서 실행한 테스트가 아니라, 대상 seed/test와 blob 동등성을 확인한 현재 작업 트리 검사다. 브랜치는 origin과 분기되어 `git pull --ff-only`가 실패했으며, 사용자 지시에 따라 rebase/stash/merge하지 않았다.
