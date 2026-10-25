# 독립 검증 — 2cea922

**판정: FAILED** — 요청된 테스트는 통과했고 시드 데이터 수치는 재현됐다. 그러나 method whitelist 선언이 없는 어댑터는 `supported_methods`가 `None`인 경우 모든 slug를 지원한다고 간주되어 Full Support로 승격될 수 있다.

## 범위와 실행 상태

- 검토 대상은 `2cea92227468747cc56de3d30a31fc1089a9ed35`의 `readiness.py`, `test_research_v2_catalog.py` 델타다. 1e35485 코어 기능은 재검증하지 않았다.
- `research-v2` worktree에서 확인했다. `origin/main` 기준 ahead 12 / behind 4였고, 조사 중 어댑터·seed·테스트 파일에 미커밋 변경이 있었다. checkout/pull 없이 보존했다. pytest 결과와 실제 어댑터 probe는 그 시점의 작업 트리에서 나온 것이므로 변경 파일의 상태에 영향을 받을 수 있다.
- 첫 요청 명령은 uv 캐시를 기본 경로에 만들려다 macOS sandbox에서 막혔다. 승인된 `/private/tmp` 캐시 경로를 지정해 같은 pytest 범위를 다시 실행했다.

## 항목별 근거

### 1. Catalog seed 및 DB 적재 — 통과

- 독립 JSON 집계: **112 methods / 112 unique slugs / 185 parameters / 128 relations / 누락 target 0**.
- 명령: `HOME=/Users/winterholic UV_CACHE_DIR=/private/tmp/uv-cache-research-review uv run python - <<'PY'` — 각 `catalog_seed/*.json`에서 `taxonomy.json` 제외, slug 집합과 모든 `relations[].target` 재계산.
- 출력: `catalog methods=112 unique_slugs=112 parameters=185 relations=128 missing_targets=0`
- DB 적재 검증: 요청 pytest에 포함된 `test_catalog_loader_full_112_matching`가 112개 system method와 관계 128개를 적재·조회했고 FK 양쪽 끝이 모두 method ID 집합에 있음을 확인했다. loader는 slug→ID map으로 관계 대상을 연결한다([catalog_loader.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/catalog_loader.py:142), [catalog_loader.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/catalog_loader.py:247)).
- 테스트 정밀도 한계: DB 테스트는 각 method parameter/target/goal의 **개수**와 관계 총수/FK 존재를 확인하지만, parameter 값 전체와 DB 관계의 `(from_slug, to_slug, type)` tuple을 seed와 직접 대조하지는 않는다([test_research_v2_catalog.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/tests/test_research_v2_catalog.py:155)). 이번 독립 재계산에서 seed target 누락은 없었고 loader 코드의 slug 연결도 확인했지만, 이 테스트만으로 동일 개수 내 값 치환까지 잡는다고 볼 수는 없다.

### 2. Readiness whitelist 및 파라미터 규칙 — 부분 통과, 결함 1건

- 내장 어댑터 중 `llm_inference`, `retrieval_eval`, `tts_synthesis`는 각각 명시적인 `supported_methods`와 `supported_parameters`를 가진다([llm_inference.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/adapters/llm_inference.py:29), [retrieval_eval.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/adapters/retrieval_eval.py:122), [tts_synthesis.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/adapters/tts_synthesis.py:100)). 외부 보고서 어댑터는 의도적인 `supported_methods=["*"]`와 보고서 입력 파라미터 목록을 선언한다([external_report.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/adapters/external_report.py:27)).
- 테스트의 규칙 검증은 형식 검사만 하는 공허 테스트가 아니다. 실제 builtin registry로 unlisted `tts_kokoro_pipeline_tuning`을 넣어 `partially_supported` 및 unsupported parameter 2개를 확인하고, whitelist된 normalization 방법은 `immediately_executable`로 확인한다([test_research_v2_catalog.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/tests/test_research_v2_catalog.py:184)). 다만 신규 테스트에 whitelist 누락 선언 반례는 없다.
- **결함 F-1 — whitelist 미선언 시 Full Support 오승격.** 구현은 `supported_methods=None`일 때 `is_method_supported=True`로 시작하고 whitelist 검사 자체를 건너뛴다([readiness.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/readiness.py:145)). 재현: `check_environment()`가 ready이고 ID가 `retrieval_eval`인 간단한 어댑터 객체에서 `supported_methods`/`supported_parameters` 속성을 모두 생략하고, `parameters=[]`, `metrics=["score"]`, slug=`unlisted_demo`인 validated method를 전달한다. 출력: `missing_declarations_ready: status=immediately_executable executable=True matched=retrieval_eval`. 이는 “지원 선언이 없으면 모든 slug가 허용”되는 반례다. builtin adapter 자체에는 현재 빠진 선언이 없지만 등록 가능한 custom/plugin adapter에 대해 strict whitelist를 보장하지 않는다.
- 같은 재현에서 `check_environment()`가 `ready=False`를 반환하면 `environment_unavailable executable=False`였다. readiness는 probe 예외도 `environment_unavailable`로 반환한다([readiness.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/readiness.py:123)). 따라서 실패/미준비 probe가 Full Support를 반환하는 경로는 확인되지 않았다.
- 실제 builtin probe 출력: `llm_inference: ready=True mode=host_supervisor`, `retrieval_eval: ready=True mode=in_process`, `tts_synthesis: ready=True mode=subprocess_worker`; `host_gpu`, `qwen_local` ID는 builtin registry에서 조회되지 않았다. 이 설정/파일 가용성 probe는 정상이라 개인 범위에서 지원 slug·parameter가 맞으면 각 adapter의 분류는 각각 `tool_integrated`, `immediately_executable`, `immediately_executable` 규칙에 부합한다([readiness.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/readiness.py:186)). 실제 llama-server 프로세스가 실행 중인지 별도로 확인한 것은 아니다. `llm_inference.check_environment()`는 binary/model 파일 가용성을 기준으로 ready를 정하고 active server 상태는 확인하지 않는다([llm_inference.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/adapters/llm_inference.py:97)).
- Kokoro 폐기/Supertonic 3 채택은 설계 문서와 adapter 구현에 일치한다([local-tts.md](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/docs/design/local-tts.md:6), [tts_synthesis.py](/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/src/ttakkari/research/adapters/tts_synthesis.py:133)). Kokoro slug는 테스트에서 Full Support가 아닌 partially supported로 나왔다.

### 3. 테스트 실행 — 통과

- 명령: `HOME=/Users/winterholic UV_CACHE_DIR=/private/tmp/uv-cache-research-review uv run pytest tests/test_catalog_seed_contract.py tests/test_research_v2_catalog.py tests/test_research_v2_core.py tests/test_research_v2_migration.py`
- 출력: `collected 17 items`; `17 passed in 2.17s`.
- 명령: `HOME=/Users/winterholic UV_CACHE_DIR=/private/tmp/uv-cache-research-review uv run pytest tests/test_research_adapters.py`
- 출력: `collected 7 items`; `7 passed in 0.05s`.
- 최초 기본 캐시 경로 명령은 실행 전 실패: `Failed to initialize cache at /Users/winterholic/.cache/uv` / `Operation not permitted`; 이후 지정 worktree의 테스트는 승인된 임시 캐시 경로로 재실행해 위 결과를 받았다.

## 결함 목록 및 재현

| ID | 심각도 | 결함 | 재현 결과 |
|---|---|---|---|
| F-1 | 중간 | `supported_methods` 미선언을 허용 전체로 해석해 strict whitelist가 우회됨 | ready adapter에서 선언 속성을 생략하고 unlisted slug/parameter 없음으로 호출 → `immediately_executable` |

**권고:** whitelist가 없는 경우 Full Support를 금지하도록 기본을 fail-closed로 바꾸고, `supported_methods` 미선언/빈 선언을 별도 negative test로 고정한다. 이 검증 작업에서는 기능 코드를 수정하지 않았다.
