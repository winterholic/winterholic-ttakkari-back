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
- 안전장치: 한 번에 하나만(flock 감독자 `python -m ttakkari.host_gpu supervise`, 백엔드 세대가 내려가도 유지), 스왑 증가 가드(시작값보다 1GB 이상이면 자동 종료), 최대 실행 시간, 운영 Qwen 서버(포트 18987·57170)와 포트·프로세스 분리(상태 파일의 pid+시작 시각 일치와 `--alias ttakkari-gpu-test` 확인 후에만 신호), 임의 명령 금지(허용 모델 별칭과 `StartRequest` 고정 필드만 argv로 변환).
- qwen-local-harness와 qwen-local 어댑터·hostmon은 재사용하지 않았다. 하네스는 접수 프롬프트용, 어댑터는 운영 서버 고정 인자용이라 시험 인스턴스의 허용 목록·가드 모델과 맞지 않았다.
- 이 도구로 잰 결과는 `measured_on: host_gpu_path`로 표시하고 "호스트 GPU 경로에서 측정"으로 말한다. 회사 세션(pool=work)에서는 막는다.

## 검증

- `tests/test_execution_capabilities.py`: 프로브 전 항목·상태, 미측정 unknown, 손글 거부, 구 보고 덮어쓰기 금지, 프롬프트 주입, 보고 API(엔진 귀속·위조 거부), account_usage 필드 추가, 영수증 제한.
- `tests/test_host_gpu.py`: 인자 검증(허용 목록·모순 조합·미지 필드), 단일 실행, 스왑 가드, 타임아웃, 감시 실패 시 fail-closed, 고아 정리(무관 프로세스 불처치), 상태 파일 권한, API 검증·개인 범위.
- 이번 Run 환경에서는 실제 GPU 끝단 실행을 하지 못했으므로 호스트 실측은 미검증으로 둔다.

## 보완 (독립 검증 결함 수정)

- 감독자 SIGKILL: 감독자가 서버보다 먼저 경량 watchdog(host_gpu_guard.py, psutil만 사용, 별도 세션)을 띄우고 준비 신호를 받은 뒤에만 서버를 시작한다(준비 실패는 fail-closed). watchdog은 0.5초 주기로 감독자(pid+시작 시각)를 보다가 사라지면, 실행 파일 경로·별칭·포트가 모두 같고 감독자 이후에 뜬 프로세스그룹 리더에게만 신호를 보낸다. 서버가 정상 종료하면 조용히 끝난다. watchdog까지 함께 죽는 경우를 위해 status/start/stop 때의 고아 정리는 그대로 둔다.
- 세션 보고 위조 방지: begin_environment_probe가 세션·Run·엔진에 묶인 일회용 challenge(15분, 세션당 5개)를 준다. 프로브를 --challenge와 함께 돌리면 보고 전체에 HMAC 서명이 붙고, 백엔드는 challenge 유효성·세션 일치·서명·측정 시각(발급 이후)을 확인한 뒤에만 저장하며 성공 시 소모한다. 엔진은 challenge가 묶인 Run의 엔진이다. 손으로 쓴 JSON·수정본·재전송·다른 세션의 challenge는 거부한다. 한계: 프로브가 세션 셸에서 돌아 서명 계산을 일부러 하는 세션은 막지 못한다(환각·손편집·재전송 방어). 회사 세션(pool=work)은 challenge 발급과 보고 모두 403(호스트 GPU 도구와 동일, 조회는 허용). PROBE_VERSION은 2.
- detail 정제: 저장 때와 읽을 때 모두 공백 정리 후 허용 문자 밖은 ?로 바꾸고 120자로 자른다. 프롬프트에는 항목 이름과 상태만 넣고 detail 문자열은 넣지 않는다. 프롬프트 주입은 미측정 1,059자에서 683자, 6개 엔진 5항목 차단 1,599자에서 1,244자로 줄었다.
- 항목별 신선도: 프로브가 항목마다 measured_epoch를 찍고, 저장·병합·stale 판정이 항목 단위다(items.*.measured_at/age_seconds/stale, 엔진 stale_items). 엔진 stale은 측정된 항목 중 오래된 것이 있거나 미측정일 때 true. 오래된 차단 항목은 프롬프트에서 차단으로 단정하지 않고 stale로 표시한다. 시각이 없는 옛 저장값은 엔진 시각으로 대신한다.
- 호스트 기준선 갱신 실패(타임아웃·실행 불가·비정상 종료): 마지막 값은 유지하고 refresh_error로 기록하며 5분간 다시 돌리지 않는다. 조회는 500 없이 응답한다.
- ps 실행 자체가 EPERM/EACCES로 거부되면 process_list는 blocked다(파일 없음·타임아웃은 unknown).
- 테스트 추가: SIGKILL 감독자, 닮은 프로세스 불처치, watchdog 준비 실패, 위조·손글·재전송·타 세션·만료·회사 세션 보고 거부, 항목별 stale, detail 정제, 프롬프트 detail 비노출, 호스트 갱신 타임아웃.
