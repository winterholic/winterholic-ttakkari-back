# 실행 어댑터 독립 검증 — 87fec61 + 476521d

## 판정

**FAILED — `LaunchMismatch`를 종료 성공으로 기록하는 데 종료 증거가 없고, 요청 테스트 묶음도 전부 통과하지 않았다.**

검증 checkout은 `research-v2`, HEAD `476521d`였다. `87fec61`과 `476521d`의 변경 및 기존 `adapter-review-439aa1da85283f721a49e52829892285558bb48a.md`, `adapter-review-2eef880.md`의 잔여 결함·결함 1~9를 대조했다. 기능 코드는 수정하지 않았다.

## 항목별 판정

| 항목 | 판정 | 근거 |
|---|---|---|
| HostGpu 무인자 계약·예외 매핑 | **PASS (정적 경로)** | `HostGpu.stop(expected_launch_id=None)`은 `GpuError` 하위 예외 `LaunchMismatch`(409)를 추가한다 (`src/ttakkari/host_gpu.py:60-75,836-862`). API의 `_call`은 `GpuError.status_code`를 HTTP 상태로 매핑하고 `/host-gpu/stop`은 기존처럼 무인자로 호출한다 (`src/ttakkari/api/host_env.py:45-49,117-122`). MCP 도구는 기존 API 경로를 호출한다 (`src/ttakkari/orchestrator/mcp_server.py:725-729`). 저장소 검색에서 직접 `HostGpu.stop` 호출은 연구 어댑터와 API, 테스트였고, 기존 API/MCP 호출 형식은 유지된다. `tests/test_host_gpu.py:701-708`은 무인자 계약을 확인한다. |
| HostGpu 소유권 검사와 TOCTOU | **PASS (구현), 일부 미검증** | `stop`은 `_current()`를 한 번 읽어 launch ID를 확인한 뒤 같은 state의 lease token·supervisor PID로 취소/신호를 보낸다 (`host_gpu.py:836-862`). 별도 사전 조회 후 재조회하는 check-then-stop 간격은 제거됐다. 테스트는 다른 launch 상태일 때 lease cancel/SIGTERM이 호출되지 않는지 확인하지만, 실제 동시 교체 경쟁을 재현하지는 않는다 (`tests/test_host_gpu.py:652-708`). `tests/test_host_gpu.py` 프로세스 통합 경로는 아래 실행 실패로 동적 확인이 제한됐다. |
| **LaunchMismatch → cleanup 성공** | **FAIL** | adapter는 `LaunchMismatch`만 받으면 현재 상태, lock, 이전 launch의 생존 여부를 확인하지 않고 `success=True, skipped_reason="launch_replaced"`를 기록한다 (`src/ttakkari/research/adapters/llm_inference.py:453-484`). 예외 조건은 “현재 단일 state의 launch ID가 다름”뿐이다 (`host_gpu.py:842-846`); state 부재/읽기 실패도 mismatch가 될 수 있다 (`host_gpu.py:154-159,615-618`). 따라서 이전 launch가 여전히 살아 있고 lease lock도 남은 상태에서 state가 없거나 읽히지 않으면 성공으로 기록될 수 있다. `tests/test_research_adapters.py:419-430`은 `stop`이 `LaunchMismatch`를 던지도록 mock으로 미리 정한 다음 `cleanup.success is True`를 확인하며, 종료 증거를 검증하지 않는다. 결함 재현: `_cleanup_owned_launch`에 `stop(expected_launch_id=...)`가 `LaunchMismatch`를 던지게 하고 `_lock_held(...) == True` 또는 실제 이전 supervisor가 생존한 상황을 설정하면, 반환 cleanup은 계속 `success=True`다. 이는 “다른 launch를 건드리지 않음”은 보장해도 “내 launch가 정리됐거나 이미 종료됨”을 입증하지 않는다. |
| STT 부분 실패 | **PASS** | 한 chunk 성공·다음 chunk 예외인 경우 `status="failed"`, `failed_chunks`, `stt_worker_failed`를 반환한다 (`tts_synthesis.py:547-608`). 회귀 테스트는 2개 chunk 중 한 호출만 실패시키고 실패 chunk를 확인한다 (`tests/test_research_adapters.py:701-724`). |
| LLM quant_type·모델 충돌·지원 파라미터 | **PASS** | 등록된 `Q4_K_M`, `Q2_K` exact 값 외에는 거절하고, `quant_type`과 명시 모델이 충돌하면 거절한다 (`llm_inference.py:189-207`). `main_gpu`, `quant_method`는 지원 목록에서 제외됐다. 해당 테스트는 허용값·비정규값·충돌 및 거절 파라미터를 확인한다 (`tests/test_research_adapters.py:548-610`). |
| TTS 취소 | **PASS** | 실행 락 대기와 `SupertonicProvider.synthesize` await에 취소 이벤트를 전달해 task를 취소하고, 취소 시 결과를 `cancelled`로 반환한다 (`tts_synthesis.py:44-61,476-493`). 취소 대기·합성 중 테스트가 각각 합성 미호출 및 provider task 취소를 확인한다 (`tests/test_research_adapters.py:726-774`). provider는 worker subprocess를 사용하고 cancellation cleanup에서 자식 프로세스를 kill/wait한다 (`tts/supertonic.py:86-115`, `tts/runner.py:7-14`, `model_process.py:63-84`). |
| TTS 지원 파라미터·설계 결정 | **PASS** | `split_on_comma`, `total_steps`가 지원 목록에서 제거됐고 구현되지 않은 method는 whitelist 밖이다 (`tts_synthesis.py:126-154`). Kokoro 요청은 폐기 경고와 함께 Supertonic 3로 매핑된다 (`tts_synthesis.py:162-164,254-255,339-373`). 관련 거절·목록 테스트는 `tests/test_research_adapters.py:775-801`에 있다. LLM TTFT는 proxy로 표기한다 (`llm_inference.py:533`). |
| readiness 실제 seed 경로 | **PASS** | 임시 테스트 DB에서 `catalog_loader.load_catalog_seeds`로 112 methods를 적재하고 실제 ORM method를 `compute_execution_status` 및 builtin registry에 전달했다. `weight_quantization(quant_method)`, `cpu_gpu_offloading(main_gpu)`, `embedding_model_benchmark(normalize_embeddings)`, `tts_sentence_segmentation(split_on_comma)` 모두 `partially_supported`였고 각 key가 `unsupported_params`에 들어갔다. 실행 명령/출력은 아래와 같다. 이는 seed를 수정하지 않은 경로 확인이다. |
| 결함 1~9 회귀 | **1 FAIL, 나머지 2~9 PASS** | 1은 위 `LaunchMismatch` 성공 판정 문제. 2 STT 실패·3 dense_weight 실순위 변경 및 normalize_embeddings=False 거절·4 정규 quant 및 과장 지원 제거·5 swap 추출·6 취소·7 TTS 범위·8 readiness·9 qrels 테스트가 관련 테스트 파일에서 확인됐다 (`tests/test_research_adapters.py:403-610,701-930`; qrels `:942-960`). 다만 HostGpu subprocess 통합 테스트 실패로 실제 프로세스 경로에 대한 보증은 제한된다. |

## 테스트 mock의 강제력

- LLM adapter 테스트는 `HostGpu.start/status/bench/_current/stop`을 `autospec=True`로 대체해 호출 시그니처를 강제한다 (`tests/test_research_adapters.py:386-399`). 이는 adapter의 keyword 호출 형태를 검증하지만 실제 HostGpu 내부 동작은 검증하지 않는다.
- mismatch 테스트는 예외와 cleanup 성공 결론을 mock으로 직접 지정한다 (`:419-430`). mismatch 상황의 lock/process 상태가 빠져 있어 종료 성공 판정을 검증하지 못한다.
- TTS 테스트는 `SupertonicProvider.synthesize`와 `run_worker`를 autospec 처리한다 (`:652-663`). 부분 실패 테스트는 실패 호출을 명시적으로 주입해 adapter의 failed 결과·failed chunk를 검증한다. 합성 중 취소 테스트도 block/cancel 동작을 테스트 더블이 제공하므로 provider의 실제 subprocess 실행까지 포함하지는 않는다.
- HostGpu ownership 테스트는 `_current`, lease manager, process identity, `os.kill`을 stand-in으로 대체하고 `HostGpu.stop` 자체는 실행한다 (`tests/test_host_gpu.py:659-708`). 상태 변화 경쟁 및 OS 신호의 실제 통합은 해당 테스트만으로 보증하지 않는다.

## readiness 실행 증거

```text
$ PYTHONPATH=src:tests ./.venv/bin/python - <<'PY'  # catalog_loader → ORM Method → compute_execution_status
cpu_gpu_offloading: partially_supported; flagged=['main_gpu']
embedding_model_benchmark: partially_supported; flagged=['normalize_embeddings']
tts_sentence_segmentation: partially_supported; flagged=['split_on_comma']
weight_quantization: partially_supported; flagged=['quant_method']
seed load=112 methods
```

실행은 repo test 설정의 격리 PostgreSQL을 썼고 프로세스 종료 시 테스트 DB 클러스터가 제거된다. 시작 시 dev 설정 경고가 출력됐으나 test DB만 초기화·사용했다.

## 테스트 실행

요청한 테스트와 `host_gpu`를 참조하는 권한·실행능력·MCP·라우트 인벤토리 테스트를 `.venv` Python으로 한 번에 실행했다.

```text
$ ./.venv/bin/python -m pytest tests/test_research_adapters.py tests/test_host_gpu.py tests/test_research_v2_core.py tests/test_research_v2_catalog.py tests/test_catalog_seed_contract.py tests/test_authz03_host_scope.py tests/test_execution_capabilities.py tests/test_orchestrator.py tests/test_route_inventory.py
collected 203 items
================== 14 failed, 189 passed in 129.38s ==================
```

실패 14건 중 13건은 `tests/test_host_gpu.py` 프로세스 통합 경로다. 관측된 내부 상태는 여러 테스트에서 `supervisor_error: OSError: ` (예외 메시지 비어 있음) 및 ready 대기 timeout이었고, supervisor 로그/traceback으로 실패 지점을 더 좁힐 evidence는 남지 않았다. 따라서 원인을 OS 권한이나 구현 결함으로 단정하지 않는다. 실패 테스트 이름: `test_supervisor_runs_until_stopped_and_cleans_up`, `test_only_one_supervisor_can_own_an_instance`, `test_instance_stops_at_the_max_runtime`, `test_server_that_never_gets_healthy_is_stopped_at_the_start_timeout`, `test_unconfirmed_server_stop_keeps_the_lease_until_hostgpu_observes_absence`, `test_server_exit_is_recorded`, `test_start_status_bench_stop_through_a_detached_supervisor`, `test_status_reveals_total_and_per_slot_context`, `test_state_file_is_private_and_holds_no_secrets`, `test_sigkilled_supervisor_leaves_no_unguarded_server`, `test_watchdog_exits_quietly_when_the_supervisor_stops_normally`, `test_watchdog_only_matches_our_server_by_path_alias_port_and_start_time`, `test_supervisor_refuses_to_run_the_server_without_a_working_watchdog`.

나머지 실패는 별도 `tests/test_route_inventory.py::test_route_inventory_matches_manifest`로, 미분류 Research API 라우트 21개를 보고했다. 이 인벤토리 파일은 대상 두 커밋에서 수정되지 않았다. 그 외 직접 지정한 Research adapter/core/catalog/seed contract 테스트와 HostGpu API 권한·capabilities·MCP 테스트는 통과했다.

## 구현 중점 판정 및 한계

- API의 `host_gpu_stop`은 기존 무인자 계약대로 `HostGpu.stop()`을 호출하고 `LaunchMismatch`도 `GpuError` 매핑상 HTTP 409가 된다. 이 새 예외를 API stop route가 직접 발생시키는 경로는 현재 없다(그 route는 expected ID를 주지 않음).
- HostGpu는 mismatch면 lease cancel/SIGTERM 전에 거절한다. 같은 state snapshot 사용으로 check-then-stop 간격은 줄었지만, 실제 동시 경쟁 테스트는 없었다.
- `launch_replaced`는 stop 소유권 실패를 뜻할 뿐 old launch의 종료 확인이 아니다. 특히 `_read_state`의 OSError/JSON 오류를 `_current()`가 `None`으로 볼 수 있고, lock이 살아 있을 때도 그 `None`을 그대로 반환한다. adapter가 mismatch 예외를 성공으로 기록하는 것은 정리 성공과 소유권 보호를 혼동한다.
- mock 없이 실제 Supertonic 모델 합성이나 host GPU 모델 구동은 실행하지 않았다. TTS 결정은 지정된 Supertonic 3 설계 기준으로 검토했고, TTFT 측정은 proxy로 표시하는 것을 확인했다.

## 범위

읽기, 테스트 실행, 이 보고서 작성 및 보고서만 stage/commit했다. 기능 코드, 다른 작업자의 변경, branch checkout/reset/stash 및 push는 수행하지 않았다.
