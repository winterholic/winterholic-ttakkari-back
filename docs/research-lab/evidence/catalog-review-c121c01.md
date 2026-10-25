# Flash 카탈로그 4차 교정 독립 cross-provider 검증

**종합 판정: FAIL** — TTS LUFS 교정은 EBU 정본과 일치하고 checker 음성·양성 대조도 적절했다. 조합 판정은 등록된 비실행 상태를 fail-closed로 처리하지만, 조합 목록 누락·충돌 중복은 Full Support로 통과하고, 실제 `external_report` 결과 입력을 지원하는 방법의 설계 검증을 차단한다. 시스템 seed에 대해 `promote_method=true`인 BestResult API 호출은 결정 기록 전체를 422로 거부하여 실제 RAG e2e가 깨진다.

## 대상과 검증 경계

- 저장소: `/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2`
- 브랜치/대상/실제 HEAD: `research-v2` / `c121c01f27b16c69529cc88344d9f543190956be` / 동일 SHA. 구현자 식별자는 UUID `8730ff9d-b2eb-411b-ae3b-1b4b3c63fba4`이며 커밋 SHA가 아니다.
- 시작 시 worktree clean. 검증 내내 back/front 구현 코드와 seed 파일은 수정하지 않았다. 최종 변경은 본 보고서 한 파일뿐이다.
- 공유 worktree 경합: 보고서를 커밋하는 사이 HEAD가 `c121c01`에서 `76a22b161a261484637b4775e16ec015246f58f4`로 이동했다. `c121c01..76a22b1` diff는 `docs/research-lab/evidence/compare-series-review-c3fbf36.md` 한 파일뿐이고 readiness/planner/service/adapters/seed 코드는 바뀌지 않았다. 본 보고서 커밋의 부모는 `76a22b1`이며, `git diff-tree`로 해당 커밋이 이 보고서 한 파일만 추가했음을 다시 확인했다. Rebase나 다른 변경 포함은 없었다.
- 정본 `docs/research-lab/refactor-v2-requirements.md`, `docs/research-lab/refactor-v2.md`와 기존 `catalog-review-1c41426.md`를 확인했다. 요구사항은 미구현 아이디어도 설계·등록하고 실행기 또는 외부 결과 등록으로 연구를 이어갈 수 있게 한다. 설계 문서 §3은 `hypothesis_stage`와 `adapter_required`에 실험 설계실 및 external report 경로를 열어 둔다.
- 기존 표본 출처 대조 24개 주장·17개 고유 문서는 이전 리뷰의 한정된 확인 범위만 재사용했다. 나머지 source 전수 재수집이나 변경 없는 표면의 중복 검사는 하지 않았다. `api-contract-review-1ddb475.md`의 99 passed는 수정 배정 전 기록이므로 이번 결과로 재사용하지 않았다.
- 변경 범위는 `c121c01`에서 바뀐 readiness/planner/service, TTS seed 및 4 adapters(`llm_inference`, `retrieval_eval`, `tts_synthesis`, `external_report`)다. 지정 테스트는 이번 HEAD에서 새로 실행했다.
- `tests/conftest.py`가 프로세스별 임시 PostgreSQL cluster/DB를 생성하는 구성을 확인하고 API 검증에 사용했다. 운영 DB는 사용하지 않았다. 테스트 결과의 초기화 통계는 49 selective reset, 평균 5.9/115 tables, forced full 0, fallback 0이었다.
- 변경 후 `PYTHONPATH=src` import로 readiness/planner/service/catalog_loader와 4개 adapter의 `__file__`이 모두 이 `back-research-v2/src/ttakkari/research/...`를 가리키고, seed 경로가 같은 checkout의 `src/ttakkari/research/catalog_seed`임을 확인했다. 보고서 작성 전 HEAD도 다시 확인했다.
- `/Users/winterholic/work-history`에서 관련 기록을 검색했다. 해당 디렉터리는 Git 저장소가 아니어서 `git -C ... status`가 `fatal: not a git repository`를 반환했다. 문서 검색은 읽기 전용으로 계속했다.

## 판정표

| 항목 | 판정 | 요약 |
|---|---|---|
| 1. TTS EBU R128 / LUFS | **PASS** | EBU R 128 V5 방송 프로그램 목표는 -23 LUFS. seed의 -16 LUFS default와 -24~-10 범위는 별도 사용자 선택 파라미터로 설명되고 서로 모순되지 않는다. checker는 잘못된 -16 EBU 문장을 실패시키고 정상 seed 및 올바른 -23 문장을 통과시켰다. |
| 2. 조합·readiness·planner | **FAIL** | 실제 adapter identity를 대조했으며 비실행 status 및 빠진 구체 튜플은 차단됐다. 그러나 combinations 전체가 비거나 충돌 중복이면 Full Support가 남는다. Mac 실행가능 표기도 실제 Metal 오프로드 증거 없이 `Darwin arm64` 및 실행 파일/모델 파일 존재로 성립한다. `external_report` 전용 LoRA/QLoRA는 `/validate`에서 실행 불가로 설계가 막힌다. |
| 3. seed 승격·채택 | **FAIL** | system seed 직접 promote 422, 중간 행 없음, `promote_method=false` 채택과 rejected 판단은 정상. 그러나 BestResult의 `promote_method=true`는 BestResult 자체를 422로 거부한다. 지정 e2e가 실제로 실패했다. 사용자 정의 방법의 external-report→승격→sync 보존 경로는 API/격리 DB에서 확인했다. |

## 1. TTS LUFS 문구와 checker

EBU의 공식 publication page는 현재 EBU R 128 Version 5.0 (2023년 11월)을 표시하고, 방송 프로그램 평균 라우드니스 목표를 -23 LUFS로 권고한다. 정본 PDF도 Target Level을 -23.0 LUFS로 적는다. [EBU R 128 공식 페이지](https://tech.ebu.ch/publications/r128), [EBU R 128 V5 공식 PDF](https://tech.ebu.ch/files/live/sites/tech/files/shared/r/r128v5_0.pdf)

`tts_audio_postprocessing`의 현 seed는 다음 의미를 분리한다.

- `summary`: 파형의 목표 음량 정규화 등 기능을 설명하며 특정 표준 목표값을 선언하지 않는다.
- `principle`: EBU R128 원리와 일반 선택 파라미터 목표값을 구별한다고 설명하고 -16을 방송 표준값이라고 말하지 않는다.
- `target_lufs`: 기본값 -16.0, 범위 -24.0~-10.0, 0.5 step. 설명은 EBU 방송 표준 권고와 구분되는 선택 파라미터라고 적는다.
- `evidence`: EBU R128의 방송 프로그램 목표 -23 LUFS와 조건을 밝히고, 사용자 선택 목표값은 공식 표준과 별개라고 구분한다.

**Checker 대조** — `check_numeric_claims`를 실제 seed와 복사한 대조 입력에 실행했다. 기존 checker 테스트의 정상 방법론 통과와 LUFS negative control도 지정 테스트에 포함되어 있다.

```text
$ PYTHONPATH=src:tests .venv/bin/python - <<'PY'  # 실제 실행, 출력 요약
real_seed target_lufs=(-16.0, -24.0, -10.0); real_seed_numeric_violations=[]
negative_control principle="EBU R128 ... -16 LUFS" -> "effect number '-16 LUFS' is not backed by a conditioned evidence claim"
positive_control 공식 EBU R128 ... -23 LUFS (조건: EBU R 128 표준 방송 프로그램 오디오) -> []
PY
```

정상 기본값·범위만 있는 seed가 통과하는 것과 잘못된 표준 귀속을 실패시키는 양방향 판정을 확인했다. seed evidence의 `year: 2020`은 최신 EBU page의 V5.0 발행연도(2023)와 다르다. -23 LUFS 핵심 권고는 V5.0에도 같지만, 이 연도 메타데이터는 최신 개정판을 가리키지 않는다.

## 2. Compatibility 조합과 실제 adapter 환경

### 실제 반환값 및 소비 경로

격리 pytest 설정을 적용한 Codex command 환경에서 built-in registry의 실제 `check_environment(scope="personal")`를 호출했다. 반환 계약의 `ready`, `execution_mode`, `reason`, `details`를 사용했으며 성공을 위해 fixture에 hardware/runtime 상수를 주입하지 않았다.

| Adapter | 실제 readiness 반환 요약 |
|---|---|
| `llm_inference` | `ready=True`, `host_supervisor`; details `runtime=llama.cpp`, `hardware=apple_silicon_metal`, server file 존재, allowlisted model files 2개, lease 미점유 |
| `retrieval_eval` | `ready=True`, `in_process`; `scikit-learn` / `cpu`, BM25/hash 준비, E5 미설치 |
| `tts_synthesis` | `ready=True`, `subprocess_worker`; `onnxruntime` / `cpu`, Supertonic 준비, STT 준비 |
| `external_report` | `ready=True`, `manual`; `external_report` / `external`, 지원 provenance 목록 |

실제 `flash_attention`은 registry가 `llm_inference`를 선택했고 `compute_execution_status`가 `tool_integrated / is_executable=True`를 반환했다. 이 readiness는 HostGpu 실행기를 시작하지 않고 파일 존재 여부·아키텍처로 추정한 준비 상태다. `llm_inference.check_environment`는 `platform.system()==Darwin` 및 `platform.machine()==arm64`이면 `apple_silicon_metal`로 이름 붙이고, 실행 가능한 `llama-server` 파일과 모델 파일이 있으면 ready라고 한다. Metal 빌드 기능이나 실제 오프로드 로그는 이 체크에서 확인하지 않는다.

Host GPU MCP 상태를 읽기 전용으로 조회했을 때 인스턴스는 `stopped`, `ready=false`, `device_evidence=[]`였다. 설정은 GPU layer 999였으나 실제 실행·Metal 오프로드를 증명하는 로그는 없었다. 이는 Codex command sandbox의 플랫폼 식별과 별도인 host GPU path 상태다. 사용자가 금지한 새 GPU 서버 시작은 하지 않았다. **현재 Mac에서 Metal 수직 실행은 미검증이며 통합 단계로 넘긴다.** d8fc1f4의 HostGpu 프로세스 회귀 검토 범위는 기존 근거로만 재사용했으며 이번 조합 판정이나 Metal 실측으로 간주하지 않았다.

### 실제 환경 identity에 대한 조합 음성 대조

실제 `llm_inference.check_environment`에서 얻은 hardware/runtime identity와 실제 seed 조합을 사용하고, 복사한 seed 조합 하나씩의 분류만 바꿔 `compute_execution_status`를 호출했다. 아래 조작은 성공값을 주입한 것이 아니라 checker가 거부해야 할 음성 대조다.

```text
actual current flash_attention: tool_integrated, executable=True
apple_silicon_metal × llama.cpp status controls:
  unsupported / upstream_supported / unverified / different_mechanism / external_only -> environment_unavailable, False
matching pair removed while other combinations remain -> environment_unavailable, False
```

위 status가 **정확히 일치하는 tuple**에 설정된 경우 fail-closed였다. 그러나 등록 자체가 빠지거나 모호하게 중복되면 다르다.

```text
$ PYTHONPATH=src:tests .venv/bin/python - <<'PY'  # 실제 실행, 출력 요약
combination_list_empty -> tool_integrated, True
conflicting_duplicate_tuple (local_executable + upstream_supported) -> tool_integrated, True
PY
```

`readiness.py`는 `if isinstance(combos, list) and combos:`일 때만 조합을 검사하므로 빈 목록은 검사를 건너뛰고, 같은 hardware/runtime이 두 번 있으면 첫 항목만 취한다. 따라서 빠진 조합·충돌 중복에서도 `Full Support` 계열의 `tool_integrated`가 반환된다. 계약 checker가 seed 자체의 구조를 잡을 수 있어도 런타임 함수가 다른 입력에서 fail-closed임을 보장하지 않는다.

### External report 선택과 planner 차단

Seed의 `lora`·`qlora`는 `adapter_hints=["external_report"]`이고 조합에는 `local_executable`이 없다. 실제 registry는 `external_report`를 선택하고 그 `check_environment`는 상시 준비 상태로 반환하지만, `compute_execution_status`가 “로컬 실행 가능한 조합 없음”으로 `environment_unavailable / False`를 돌려준다.

실제 ASGI API와 임시 PostgreSQL에서 확인했다. `/api/research/experiments/validate`에 `lora`, `adapter_id=external_report`, `strategy=single`을 보내면 `valid=false`와 로컬 실행 가능 튜플이 없다는 오류가 반환됐다. `flash_attention`을 비교 방법(`compared_method_ids`)에 추가해도 같은 lora 오류가 남아 multi-method 검증에서도 한 비실행 보조 방법이 전체 설계를 막았다. 동일 연구에서 `POST /studies/{id}/experiments`로 lora 방법을 포함한 설계를 저장하고 `/experiments/{id}/external-report`에 결과를 등록하는 직접 API 경로는 각각 201이었다.

재현 명령은 `PYTHONPATH=src:tests .venv/bin/python - <<'PY'`로 시작하는 inline ASGI probe였다. probe는 `tests/conftest.py`를 불러 프로세스별 임시 PostgreSQL을 만들고 schema를 새로 만든 뒤 인증 요청을 보냈다. 결과:

```text
sync 200
validate lora (compared=0): valid=False; errors=["...현재 환경에서 실행할 수 없습니다: 로컬 실행 가능한 하드웨어/런타임 조합이 정의되지 않았습니다..."]
validate lora (compared=1, flash_attention): valid=False; same lora error
create_lora_experiment 201
lora_external_report 201
```

`planner.validate_experiment_plan`은 선택한 `method_id`와 `compared_method_ids`의 모든 `compute_execution_status(...).is_executable == False`를 `errors`에 넣어 `valid=false`로 만든다. 이는 로컬 실행 전 검증을 설계·저장 및 external report 등록과 섞는다. 등록된 미구현 가설이나 외부 검증만 가능한 방법도 계획 검증을 통과하지 못한다. 현재 직접 생성 API가 우회 저장은 허용하지만, 프론트 설계 흐름의 `/validate`는 이를 실패로 반환한다. R3/R8/R12D의 연구 설계·외부 결과 등록 허용 요구와 맞지 않는다.

## 3. System seed 채택 및 사용자 방법 승격

### 시스템 seed 경로

격리 PostgreSQL에서 catalog sync 후 system `flash_attention` 방법으로 실험을 만들고 외부 결과 Run을 등록했다. 실제 ASGI BestResult 요청 결과:

```text
system_promote_true_api: 422, decision_rows=0, best_result_rows=0
system_promote_false_api: 201 adopted
rejected_api (promote_method=true): 201 rejected
```

이 결과는 `PYTHONPATH=src:tests .venv/bin/python - <<'PY'` ASGI probe로 얻었다. probe는 앞 절과 동일하게 `tests/conftest.py`가 시작한 임시 PostgreSQL에만 접속하고, system `flash_attention` seed를 sync한 뒤 실험·external-report Run·세 BestResult 요청을 실제 API로 만들었다. system `promote_method=true` 거부 직후 DB count도 읽어 중간 행이 없음을 확인했다.

system method 직접 promote 422는 기존 결정대로다. 다만 `adopt_best_result`의 `promote_method_flag=True`는 system method를 먼저 확인해 raise하고 BestResult/Decision을 하나도 기록하지 않는다. 보존 경계는 깔끔하지만 결과 채택까지 거부한다. `tests/test_catalog_seed_readiness_path.py`의 직접 service 호출도 system promote 거부 후 중간 Decision/BestResult가 0인 것을 확인했다.

실제 프론트 코드의 BestResult payload는 읽기 전용으로 확인했다. 현재 확인 가능한 `front-research-v2` HEAD는 `c3fbf3613ee699092279bd27e363eaa9ef00a3`이며, `BestResultPanel.tsx`는 `promote` 초기값을 false로 두고 payload에 `promote_method: canPromote && promote`를 보낸다. 사용자가 체크하면 local-measured 결과의 system seed에도 true가 실릴 수 있어 API 422가 채택 자체를 막는다. 일반 미체크 payload는 false이고 통과한다. 요청에서 지목한 `d595eb1f`는 이 checkout의 Git 객체/로그에서 찾지 못했다. front 파일은 수정하지 않았다.

지정 e2e도 이를 재현했다. RAG catalog seed로 실험하고 성공한 Run을 만든 뒤 `/best-results`에 `promote_method: true`를 보냈을 때 예상 201 대신 422가 나왔다. 사용자 선택 payload 하나에서 표준 e2e 채택이 실패한다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_catalog_seed_contract.py tests/test_catalog_seed_readiness_path.py tests/test_research_v2_catalog.py tests/test_research_v2_api_contract.py tests/test_research_v2_core.py tests/test_research_v2_e2e.py tests/test_research_adapters.py -q
... test_vertical_e2e_rag_research_flow ... FAILED
E assert 422 == 201
1 failed, 131 passed in 12.14s
```

프론트의 기본 체크 상태(false) 경로와 rejected 판단은 API에서 통과했으나, 채택과 방법론 승격을 한 flag에 결합해 분리하지 못한다. 따라서 “일반 seed 기반 실험 결과 채택”은 기본 선택에서는 되지만 승격 체크가 켜진 순간 통째로 막힌다.

### 사용자 정의 개인 방법 경로

실제 API+격리 PostgreSQL에서 user-defined 개인 방법을 등록하고 external-report 실험을 저장했다. 외부 결과 보고서 등록은 201이고 `execution_origin=external_report`였다. 이어 `/methods/{id}/promote`는 200 및 `lifecycle_status=candidate`, `promoted_from_run_id=<external Run id>`를 반환했다(외부 Run의 승격은 service 규칙상 candidate). 그 뒤 catalog seed sync를 다시 한 결과:

- 개인 Method는 존재하며 `candidate`, 같은 `promoted_from_run_id`를 유지했다.
- external Run은 `succeeded`, `execution_origin=external_report`로 보존됐다.
- method promotion Decision 1개가 연구에 남았다.

이 user-scope sync 불변식은 통과했다. seed loader는 system seed만 동기화하므로 user method를 덮어쓰지 않는다.

## 실행 기록과 표면

### 지정 테스트 전체

전체 7개 파일을 아래 명령 하나로 새로 실행했다. 결과는 **131 passed / 1 failed**다. 실패는 위 BestResult system seed e2e 하나다. 따라서 `99 passed` 이전 snapshot은 재사용하지 않았고 전체 지정 테스트 성공이라고 판정하지 않는다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_catalog_seed_contract.py tests/test_catalog_seed_readiness_path.py tests/test_research_v2_catalog.py tests/test_research_v2_api_contract.py tests/test_research_v2_core.py tests/test_research_v2_e2e.py tests/test_research_adapters.py -q
... [54%]
..........F................................................. [100%]
FAILED tests/test_research_v2_e2e.py::test_vertical_e2e_rag_research_flow
1 failed, 131 passed in 12.14s
```

### 표면별 증거

| 표면 | 확인 내용 | 판정 범위 |
|---|---|---|
| seed/code | 실제 TTS seed·checker, readiness/planner/service·4 adapters 읽기; import 경로와 seed 경로 확인 | 정적 계약 및 실제 함수 호출. 광범위한 112 source 재검증 아님 |
| checker | 실제 LUFS negative control와 정상/공식 positive control | 두 방향 대조 통과 |
| ASGI + DB | 지정 pytest의 isolated PostgreSQL API/core/e2e, 별도 API 격리 재현 | user sync, external report, promote 및 BestResult 경로. 운영 DB 미사용 |
| adapter readiness | Codex command/test 환경에서 adapter 네 개의 실제 `check_environment` 반환값 호출 | file/config readiness 측정이며 HostGpu 실제 실행은 아님 |
| host GPU | host GPU MCP status read-only 조회; stopped, ready=false, device evidence 없음 | 이번 검증에서 새 서버 실행하지 않음. Metal 수직 실행 미검증 |
| 브라우저 | 미실행 | UI 렌더링/사용자 상호작용은 검증하지 않음. front 코드는 payload 확인용 read-only |

## 최종 판단

- **항목 1 PASS.** EBU 권고 -23 LUFS와 사용자 default -16 LUFS(-24~-10)의 설명이 분리됐고 checker는 음성·양성 사례를 구분했다. EBU evidence `year=2020`은 현재 V5.0의 2023 발행연도와 어긋난다.
- **항목 2 FAIL.** 실제 adapter identity에서 개별 비실행 tuple 상태는 fail-closed지만 빈/중복 충돌 tuple은 `tool_integrated`로 통과한다. Mac Metal은 실행 환경 식별의 추정일 뿐 host GPU device evidence가 없어 실증되지 않았다. planner는 비실행·external-only 방법을 설계 검증에서 차단한다.
- **항목 3 FAIL.** 직접 system promote 422 및 중간 row 없음, false flag adoption, rejected 보존, 사용자 개인 방법의 external report promotion/sync는 확인했다. `promote_method=true`인 system seed BestResult는 통째로 422가 되고 지정 RAG e2e가 실패한다.

이번 검증은 요청 범위에서 보고서만 추가한다. 코드·seed·front 변경, stash, rebase, push, deploy, GPU 서버 시작은 하지 않았다. 실제 host-GPU 수직 실행은 통합 단계에서 별도 검증할 항목이다.
