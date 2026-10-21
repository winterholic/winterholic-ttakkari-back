# ADR 0037. 엔진별 실행 환경 능력 공유와 엔진 무관 호스트 GPU 경로

- 상태: 채택(2026-10-18), 같은 날 독립 검증 결함 5건 보완(아래 "보완")
- 결정자: 사용자("그걸 맥락으로 공유하게끔 장치를 걸어놔. 클로드에만 의존적인 구조는 별로야"). GPU 작업을 Claude 엔진에만 몰아주는 해법은 쓰지 않는다.

## 배경

엔진마다 작업 세션의 실행 환경 능력이 다르다. Codex 샌드박스는 GPU 장치(IOKit) 접근과 Metal 컴파일 서비스 통신을 거부했고(`llama-server --list-devices`에 Metal 없음, `MTLCreateSystemDefaultDevice`가 nil, `ps`·일부 `sysctl`도 operation not permitted), Muse에서도 Metal이 보이지 않았다. Claude 엔진에서는 `MTL0: Apple M5 Max (53084 MiB)`가 보였다. 이 차이를 모르던 세션들이 "GPU 사용 불가"로 잘못 일반화했고, 실제 운영 Qwen 서버가 CPU만 쓴 원인은 실행 인자(`--device none --gpu-layers 0`)였다.

## 결정 1. 실행 환경 능력 맥락 공유

- 엔진(실행 경로: claude·codex·antigravity·muse·qwen-local·host)별 능력을 `runs/env_probe.py` 실측으로만 기록한다. 항목은 gpu_metal, llama_list_devices, process_list, sysctl_read, spawn_process, network_loopback, network_outbound, writable_paths. 측정하지 않은 항목은 unknown이다.
- 저장소는 `data_dir/execution-capabilities.json` (세대와 무관한 파일). 세션은 `python3 env_probe.py --json` 결과를 `report_execution_environment` MCP 도구로 보고하고, 엔진은 보고 내용이 아니라 그 세션의 최근 Run으로 정한다(위조 불가). 손글·오래된 측정(24시간)·모르는 항목·상태는 거부한다. 호스트 기준선은 백엔드가 자식 프로세스로 직접 잰다(backend_probe, 1시간 캐시).
- 모든 세션 프롬프트(`runs/manager.py::_system_note`)에 짧은 안내를 넣는다. 원칙 두 가지: "샌드박스에서 막힌 결과는 그 환경 한정이며 호스트 불가로 일반화하지 않는다"와 그 엔진의 알려진 제한 목록.
- `account_usage` 응답과 worker_routing 배정 영수증에 `execution_capabilities`/`execution_environment` 필드만 추가한다. 기존 필드 의미는 그대로다. MCP 도구 `execution_capabilities`(읽기 전용)·`report_execution_environment`도 둔다.

## 결정 2. 엔진과 무관한 호스트 GPU 경로

- 어떤 엔진의 작업 세션이든 백엔드 프로세스가 수행하는 MCP 도구로 샌드박스 밖 호스트에서 GPU 작업을 요청하는 정식 경로를 둔다. 첫 범위는 로컬 llama-server 시험 인스턴스 하나: 시작·상태·벤치·중지(`host_gpu_start/status/bench/stop`, `ttakkari/host_gpu.py`).
- G1 common lease: 기존 `data_dir/host-gpu/lock` flock을 호스트 단일 GPU 자원의 상호배제 지점으로 유지하고, `data_dir/host-resource/lease.json`에 owner task/attempt, runtime instance, generation token, heartbeat/expiry, cancel phase와 stop proof를 기록한다. `host_gpu`는 `resource_family=llm`으로 이 lease를 잡는다. Comfy/ACE/Three/Blender managed consumer도 같은 `ResourceLeaseManager`와 flock 경로를 써야 하며, 별도 잠금은 자원 통합으로 인정하지 않는다.
- lease expiry는 런타임 종료 증거가 아니다. 획득 경합은 flock으로 차단하고, 소유자·generation token 불일치 cancel/release는 거부한다. 재시작 대조는 소유 런타임을 확인한 정지 증거가 있어야 하며, 오래된 미확정 lease는 유지한다. 이 변경은 공통 lease의 backend 기반과 llama 연동까지이며, Comfy managed submit/observe/collect/reconcile 경로와 실제 GPU 검증을 완료했다는 뜻은 아니다.
- 임의 명령 금지(허용 모델 별칭과 `StartRequest` 고정 필드만 argv로 변환).
- qwen-local-harness와 qwen-local 어댑터·hostmon은 재사용하지 않았다. 하네스는 접수 프롬프트용, 어댑터는 운영 서버 고정 인자용이라 시험 인스턴스의 허용 목록·가드 모델과 맞지 않았다.
- 이 도구로 잰 결과는 `measured_on: host_gpu_path`로 표시하고 "호스트 GPU 경로에서 측정"으로 말한다. 회사 세션(pool=work)에서는 막는다.
- 긴 컨텍스트 실험은 ctx_size 최대 1,010,000과 cache_type_k/v의 f16·q8_0·q4_0 고정 선택을 허용한다. 슬롯당 컨텍스트 262,144 초과는 rope_scaling=yarn을 명시해야 하며 scale=4, original context=262144로 고정한다. 기존 기본 ctx8192·KV 타입 미지정·rope 미지정은 유지한다. 이는 허용 모델의 확장 실험 설정이며 1M 품질·처리 시간 보증이 아니다. 기존 단일 실행·소유권·1GB 스왑 가드·시간 상한은 유지한다.

## 검증

- `tests/test_execution_capabilities.py`: 프로브 전 항목·상태, 미측정 unknown, 손글 거부, 구 보고 덮어쓰기 금지, 프롬프트 주입, 보고 API(엔진 귀속·위조 거부), account_usage 필드 추가, 영수증 제한.
- `tests/test_host_gpu.py`: 인자 검증(허용 목록·모순 조합·미지 필드), 단일 실행, 스왑 가드, 타임아웃, 감시 실패 시 fail-closed, 고아 정리(무관 프로세스 불처치), 상태 파일 권한, API 검증·개인 범위.
- `unit_tests/test_host_resource.py`: LLM/Comfy 공통 경합, foreign/stale generation 거부, 만료 후에도 정지 증거 전까지 lease 유지, 재시작 reconcile, cancel 후 정상 stop release, 응답 유실/다운로드 실패 시 보존, legacy llama 상태 fail-closed.
- 이번 Run 환경에서는 실제 GPU 끝단 실행을 하지 못했으므로 호스트 실측은 미검증으로 둔다.

## 보완 (독립 검증 결함 수정)

- 감독자 SIGKILL: 감독자가 서버보다 먼저 경량 watchdog(host_gpu_guard.py, psutil만 사용, 별도 세션)을 띄우고 준비 신호를 받은 뒤에만 서버를 시작한다(준비 실패는 fail-closed). watchdog은 0.5초 주기로 감독자(pid+시작 시각)를 보다가 사라지면, 실행 파일 경로·별칭·포트가 모두 같고 감독자 이후에 뜬 프로세스그룹 리더에게만 신호를 보낸다. 서버가 정상 종료하면 조용히 끝난다. watchdog까지 함께 죽는 경우를 위해 status/start/stop 때의 고아 정리는 그대로 둔다.
- 세션 보고 위조 방지: begin_environment_probe가 세션·Run·엔진에 묶인 일회용 challenge(15분, 세션당 5개)를 준다. 프로브를 --challenge와 함께 돌리면 보고 전체에 HMAC 서명이 붙고, 백엔드는 challenge 유효성·세션 일치·서명·측정 시각(발급 이후)을 확인한 뒤에만 저장하며 성공 시 소모한다. 엔진은 challenge가 묶인 Run의 엔진이다. 손으로 쓴 JSON·수정본·재전송·다른 세션의 challenge는 거부한다. 한계: 프로브가 세션 셸에서 돌아 서명 계산을 일부러 하는 세션은 막지 못한다(환각·손편집·재전송 방어). 회사 세션(pool=work)은 challenge 발급과 보고 모두 403(호스트 GPU 도구와 동일, 조회는 허용). PROBE_VERSION은 2.
- detail 정제: 저장 때와 읽을 때 모두 공백 정리 후 허용 문자 밖은 ?로 바꾸고 120자로 자른다. 프롬프트에는 항목 이름과 상태만 넣고 detail 문자열은 넣지 않는다. 프롬프트 주입은 미측정 1,059자에서 683자, 6개 엔진 5항목 차단 1,599자에서 1,244자로 줄었다.
- 항목별 신선도: 프로브가 항목마다 measured_epoch를 찍고, 저장·병합·stale 판정이 항목 단위다(items.*.measured_at/age_seconds/stale, 엔진 stale_items). 엔진 stale은 측정된 항목 중 오래된 것이 있거나 미측정일 때 true. 오래된 차단 항목은 프롬프트에서 차단으로 단정하지 않고 stale로 표시한다. 시각이 없는 옛 저장값은 엔진 시각으로 대신한다.
- 호스트 기준선 갱신 실패(타임아웃·실행 불가·비정상 종료): 마지막 값은 유지하고 refresh_error로 기록하며 5분간 다시 돌리지 않는다. 조회는 500 없이 응답한다.
- ps 실행 자체가 EPERM/EACCES로 거부되면 process_list는 blocked다(파일 없음·타임아웃은 unknown).
- 테스트 추가: SIGKILL 감독자, 닮은 프로세스 불처치, watchdog 준비 실패, 위조·손글·재전송·타 세션·만료·회사 세션 보고 거부, 항목별 stale, detail 정제, 프롬프트 detail 비노출, 호스트 갱신 타임아웃.

## 보완 (G1 lease 독립 재검수: fail-open과 교착 수정, 수동 복구)

- 프로세스 상태 3분류: `host_gpu_guard.process_state`는 PRESENT / ABSENT(없음, 좀비, pid 재사용으로 시작 시각 불일치) / UNKNOWN(조회 실패, 권한 거부, pid·시작 시각 근거 누락)을 돌려준다. lease 해제는 ABSENT가 관찰될 때만 한다. UNKNOWN은 부재가 아니며 lease와 state를 보존한다(fail-closed).
- 회수 순서: `_reap_lost`는 lease reconcile이 성공한 뒤에만 state를 `stopped`로 확정한다. reconcile이 실패하면 state는 live로 남아 다음 조회가 다시 시도한다. reconcile은 성공했는데 state 쓰기가 실패하면 다음 시도가 lease가 이미 `released`임을 확인하고 state만 확정한다.
- state를 먼저 쓴다: 감독자는 서버를 띄우기 전에 `server_spawn: "pending"` state(포트·argv·감독자 신원·lease 식별)를 durable하게 쓰고, 서버를 띄운 뒤 pid·시작 시각과 `server_spawn: "spawned"`를 다시 쓴다. state가 없는 live lease는 서버가 없었음이 보장된다. pending 상태에서 죽었다면 `host_gpu`가 포트·argv·별칭으로 같은 사용자 프로세스를 엄격 열거(`find_our_servers_strict`)해 우리 서버를 정리하고 비어 있음을 두 번째 열거로 확인할 때만 회수한다. 같은 사용자 프로세스의 argv·시작 시각·상태를 읽지 못하면 불완전으로 보고 보존한다. 서버가 뜰 수 있었던 가장 이른 시각(감독자 시작)보다 먼저 시작한 프로세스는 후보가 아니므로 읽지 못해도 건너뛴다(macOS는 같은 사용자의 일부 보호 프로세스 argv 조회를 거부한다).
- lease 소유자 신원: `ResourceLeaseManager.acquire`가 lease.json에 `owner_pid`·`owner_start`를 기록한다. 소유자가 죽어 flock만 풀리고 state가 없거나 다른 launch의 것이거나 `stopped`인 live lease는, 소유자가 ABSENT이고 state에 기록된 서버도 ABSENT일 때만 `host_gpu`(owner_task=host_gpu)가 회수한다. 다른 소비자(Comfy 등)의 lease는 건드리지 않는다(그 소비자의 reconcile 몫). 소유자 신원이 없는 lease(이 보완 이전에 기록된 것)는 UNKNOWN으로 보존된다.

### 수동 복구 절차 (`force-release`)

자동 복구가 거부하는 경우: legacy state(`lease_id` 없음), 서버 pid·start 근거 누락, 우리 서버로 확인되지 않는 프로세스가 기록된 pid로 살아 있음, 소유자 신원이 없는 lease. 거부 메시지가 이 절을 가리킨다.

1. 살아 있는 시험 서버가 없음을 직접 확인한다. 별칭 `ttakkari-gpu-test`를 argv에 가진 프로세스가 없어야 한다. 이 명령은 아무 프로세스에도 신호를 보내지 않는다. 있다면 먼저 정상 경로(`host_gpu_stop` 또는 프로세스 종료)로 멈춘다.
2. 상태 확인: `host_gpu_status` 또는 `data_dir/host-gpu/state.json`, `data_dir/host-resource/lease.json`에서 `lease_id`를 확인한다.
3. 실행(백엔드 venv, 저장소 루트):
   `python -m ttakkari.host_gpu force-release --reason "<무엇을 어떻게 확인했는지 10~300자>" --lease-id <lease_id> --confirm-no-live-process [--data-dir <dir>] [--accept-unrelated-pid]`
   - live lease가 있으면 `--lease-id`가 일치해야 한다. lease 없는 legacy state만 있으면 생략한다.
   - `--accept-unrelated-pid`는 기록된 서버 pid가 살아 있지만 우리 서버가 아님을 직접 확인했을 때만 쓴다(argv가 기록으로 남는다).
4. 명령이 스스로 거부하는 조건(우회 불가): 공용 flock을 쥔 살아 있는 소유자가 있음, 별칭 프로세스가 하나라도 있음, 프로세스 표를 완전히 열거하지 못함(서버가 뜰 수 있었던 가장 이른 시각 이후에 시작한 같은 사용자 프로세스의 argv를 읽지 못함), 기록된 supervisor·lease 소유자 pid가 아직 존재함, 기록된 서버 pid가 존재하는데 `--accept-unrelated-pid`가 없음, reason 부족, 해제할 것이 없음.
5. 성공하면 lease를 `release_proof`(operator_override, reason, 확인한 프로세스 상태)와 함께 해제한 뒤 state를 `stopped`(`stop_reason: operator_force_release`)로 확정한다. 이 증거는 `lease.json`에 남는다.

### 보완 검증
- `unit_tests/test_host_resource_process_state.py`: process_state 3분류, terminate_group 종료 관찰, `_reap_lost` UNKNOWN 보존, watchdog.
- `unit_tests/test_host_resource_recovery.py`: 교착 1(reconcile 실패, state 쓰기 실패, 불일치 lease), 교착 2(소유자 ABSENT/UNKNOWN/PRESENT/pid 재사용/신원 없음, state 없음/타 launch/stopped, 타 소비자 lease 불간섭, pending 서버 열거), state-먼저 순서, force-release 허용·거부와 CLI.
- 재현 스크립트: `scripts/repro/g1_host_resource_lease_repro.py`(`PYTHONPATH=src python ...`, FAIL이 하나라도 있으면 종료 코드 1).
- 한계: 이 검증은 CPU child와 psutil 주입이다. 실제 GPU, 운영 llama/Qwen, 실제 권한 거부 환경(sandbox ps/sysctl)은 미실행이다.

## 보완 2 (0704b82 독립 재검수 결함 4건)

- 엄격 열거의 setuid 제외: 같은 사용자의 setuid `login` 프로세스(터미널 창마다 하나, 이 host에서 7개 관측)는 argv 조회가 거부된다. 이 프로세스가 lease 이후에 시작되면 열거가 영구 불완전이 되어 force-release와 pending 회수가 모두 거부되었다. 이제 argv를 못 읽어도 `exe`를 읽을 수 있고 그것이 우리 서버 실행 파일이 아닌 setuid/setgid 일반 파일이면 우리 서버일 수 없으므로 제외한다(`_setuid_foreign_binary`). `exe`도 읽지 못하거나 setuid가 아닌 프로세스는 계속 불완전으로 보고 보존한다(fail-closed). 이 host의 실제 프로세스 표로 7시간 된 lease를 해제해 확인했다. exec 도중인 프로세스의 argv가 잠깐 비는 일시적 불완전은 최대 3회(0.1초 간격) 다시 열거해 넘기고, 계속 불완전하면 거부한다.
- 다른 소비자 lease의 force-release: `owner_task`가 `host_gpu`가 아닌 lease는 별칭 검사만으로 해제하지 않는다. managed Comfy(`resource_family=comfy`, owner_task가 32자 hex task id)는 `managed_comfy.force_release_evidence`가 (1) controller job 기록의 task id·lease id·generation token·runtime instance 일치, (2) 기록된 runtime pid+시작 시각이 ABSENT, (3) 고정 포트 8199의 연결 거부를 모두 요구한다. runtime pid가 한 번도 기록되지 않았다면 `--accept-unrecorded-comfy-runtime`으로 운영자가 직접 확인했음을 밝혀야 하고 이 사실이 proof에 남는다. 검증기가 없는 소비자의 lease는 거부한다(그 소비자가 자기 lease를 reconcile해야 한다). 다른 소비자 lease 해제는 host_gpu state를 건드리지 않는다.
- state가 없거나 불일치하는 고아 lease: 감독자가 state를 먼저 쓴다는 불변식에만 기대지 않고, 소유자 ABSENT 확인에 더해 별칭 서버가 없음을 엄격 열거로 확인한 뒤에만 회수한다. 별칭 서버가 있으면(아직 watchdog이 정리하기 전일 수 있다) 보존하고 다음 조회가 다시 시도한다. 이 경로는 서버에 신호를 보내지 않는다. 열거가 불완전해도 보존한다.
- 시계 역행: pending 서버를 찾는 시작 시각 하한 여유를 `PENDING_MATCH_MARGIN`=300초로 넓혔다(서버는 포트·argv로 이미 특정되고 같은 포트의 옛 서버는 공존할 수 없다). argv를 못 읽는 프로세스를 건너뛰는 기준은 기존 `START_SKEW`(1.5초)를 유지한다. 한계: 시계가 300초 넘게 뒤로 감기면 pending 서버를 놓칠 수 있다. 이 경우는 방어되지 않는다. pending 경로 `_reap_lost`는 `_observe_pending_server`의 포트·argv 열거가 비었다고 보면 별칭 열거 없이 곧바로 ABSENT로 판정해 lease를 회수하므로, 놓친 서버가 살아 있어도 lease가 풀릴 수 있다. 별칭 열거는 고아 lease 경로(`_recover_orphan_lease`)와 force-release에만 있고 이 경로에는 없다.

### 보완 2 검증
- `unit_tests/test_host_resource_recovery_hardening.py`(25개): setuid 규칙과 열거, 실제 프로세스 표로 7시간 lease 해제, 소비자별 거부·허용, 고아 lease 열거, 시계 역행 3/120/250초.
- `scripts/repro/g1_rereview_repro_v2.py`: 독립 재검수자의 재현 스크립트 원본(49 checks). 새 HEAD에서 49/49, 0704b82 소스에서는 4건 실패.
