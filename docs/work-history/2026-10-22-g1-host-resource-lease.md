# 2026-10-22 G1 공용 호스트 자원 lease와 managed Comfy 경로

다음 검수자가 이 저장소만으로 맥락을 읽도록 남기는 기록이다(검수자와 작성자는 서로의 outbox를 읽을 수 없다).
브랜치 `codex/g1-host-resource-lease-20261022`. 상세 설계와 수동 복구 절차는 `docs/adr/0037-execution-capabilities-and-host-gpu.md`.

## 커밋 이력
| 커밋 | 내용 |
|---|---|
| `561f6f6` | 공용 `ResourceLeaseManager`(flock + `host-resource/lease.json`)와 `host_gpu` 연동. 독립 검수 FAILED(fail-open) |
| `aa61dc2` | managed Comfy 실행 경로(고정 profile, submit/observe/collect/reconcile/cancel/release) |
| `3913144` | fail-open 수정: 프로세스 상태 3분류(PRESENT/ABSENT/UNKNOWN). 독립 재검수 FAILED(영구 교착 2건) |
| `ec147b3`, `1bb39d5` | Comfy 실행 receipt 매핑, `workflow_revision_hash` 이름 |
| `0704b82` | 교착 1·2 수정, 수동 복구 `force-release`, 재현 스크립트. 독립 재검수 FAILED(잔여 결함 4건) |
| 이번 커밋 | 재검수 결함 4건 수정(setuid login 열거, 소비자별 force-release 검증, 고아 lease 열거, 시계 여유) |

## 검수 결함과 수정
1. fail-open(`561f6f6`): `_reap_lost`가 `same_process()==False`를 서버 부재로 보고 lease를 해제했다. `proc_start`는 `psutil.Error/OSError`에서 None을 돌려주므로 권한 거부와 실제 부재가 섞였다. 반례: 자체 소유 child 생존, active lease 없음, 후속 취득 성공. 수정: `host_gpu_guard.process_state` 3분류, ABSENT일 때만 해제(`3913144`).
2. 교착 1(`3913144`): state를 `stopped`로 먼저 쓴 뒤 `reconcile`을 호출해, reconcile이 OSError로 실패하면 state는 비활성인데 lease는 live로 남고 `_current`가 정리를 건너뛰어 `acquire`가 영구 `LeaseBusy`. 수정: reconcile 성공 뒤에만 `stopped` 확정. 실패하면 state live 유지(재시도). 반대 경우(reconcile 성공, state 쓰기 실패)는 lease가 이미 `released`임을 `recorded_phase`로 확인해 state만 확정.
3. 교착 2(`3913144`): 감독자가 lease 획득과 state 쓰기 사이에서 죽으면 live lease만 남고 회수 경로 없음. 수정: lease에 `owner_pid`/`owner_start` 기록, 감독자는 서버를 띄우기 전에 `server_spawn: pending` state를 먼저 쓴다. `_current`는 flock이 풀린 `owner_task=host_gpu` live lease를 소유자 ABSENT(+state의 서버 ABSENT)일 때만 회수한다. pending 상태에서 죽었으면 포트·argv로 엄격 열거해 서버를 정리하고 비어 있음을 확인한 뒤 회수한다. UNKNOWN/불완전은 보존.
4. 수동 복구: `python -m ttakkari.host_gpu force-release ...`(ADR 0037). 신호를 보내지 않고 안전 조건을 스스로 검사한다.

## 0704b82 재검수 결함 4건과 수정
1. 같은 사용자의 setuid login(터미널마다 하나, 7개)의 argv 조회 거부가 열거를 영구 불완전으로 만듦. 7시간 lease는 거부, 1시간 lease는 해제되던 현상. 수정: exe를 읽을 수 있고 우리 서버가 아닌 setuid/setgid 파일이면 제외. exe도 못 읽으면 불완전 유지.
2. force-release가 다른 소비자(comfy) lease도 별칭 검사만으로 해제. 수정: 소비자별 런타임 검증(managed Comfy는 job 기록 일치, runtime pid ABSENT, 포트 연결 거부), 검증기 없는 소비자는 거부.
3. state 없음/불일치 고아 lease를 열거 없이 회수. 수정: 별칭 서버 엄격 열거로 없음을 확인한 뒤에만 회수.
4. 시계 역행 시 pending 서버를 놓침. 수정: 매칭 하한 여유 300초(건너뛰기 기준은 1.5초 유지). 300초 넘는 역행은 한계로 ADR에 명시.
- 재검수자 재현 스크립트 원본: `scripts/repro/g1_rereview_repro_v2.py`(첫 줄 lint 지시만 추가). 새 HEAD에서 49/49, 0704b82에서는 4건 실패.

## 재현과 검증
- 재현 스크립트(저장소 안): `PYTHONPATH=src python scripts/repro/g1_host_resource_lease_repro.py`. 시나리오 A(fail-open), B(교착 1), C(교착 2), D(수동 복구). `3913144` 소스에서는 B와 C가 FAIL, 이번 커밋에서는 12/12 OK.
- 회귀 테스트: `unit_tests/test_host_resource_process_state.py`, `unit_tests/test_host_resource_recovery.py`, `unit_tests/test_host_resource.py`, `tests/test_host_gpu.py`.
- 이 검수 라운드의 원문 보고서와 검수자 repro(`g1_host_resource_repro.py`)는 작성자가 열람하지 못했다(ttakkari 보호 영역 hook). 위 반례는 요청문에 적힌 결함 설명을 근거로 재구성했으므로 검수자 원본 스크립트와 동일하다고 보증하지 않는다.

## 알려진 한계와 미검증
- 실제 GPU, 운영 llama/Qwen, 실제 sandbox 권한 거부 환경은 실행하지 않았다(CPU child와 psutil 주입).
- macOS는 같은 사용자의 일부 보호 프로세스 argv 조회를 거부한다(이 host에서 7개 관측). 엄격 열거는 서버가 뜰 수 있었던 가장 이른 시각보다 먼저 시작한 프로세스를 후보에서 제외해 이를 처리한다. 그 이후에 시작한 같은 사용자 프로세스의 argv를 못 읽으면 불완전으로 보고 보존한다.
- 소유자 신원이 없는 lease(이 변경 이전에 기록된 것)와 legacy state는 자동 회수하지 않고 `force-release`가 필요하다.
- 좀비는 ABSENT(종료)로 본다.
- managed Comfy 실제 HTTP/WS(backend venv에 `websocket-client` 없음)와 실제 GPU smoke는 미실행. G0 main과의 통합은 이 브랜치에 G0 코드가 없어 미검증.
