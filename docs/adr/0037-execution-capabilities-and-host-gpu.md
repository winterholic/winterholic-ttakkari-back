# ADR 0037. 엔진별 실행 환경 능력 공유와 엔진 무관 호스트 GPU 경로

- 상태: 채택(2026-10-18)
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
