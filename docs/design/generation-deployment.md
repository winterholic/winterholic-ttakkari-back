# 세대별 배포 구현과 운영 전환

2026-10-16. 최초 안전 전환 자동화를 추가했다. 실제 운영 활성화는 독립 배포 감독의 예약 기록과 health·version·smoke 결과로 판정한다.

## 동작

기존 앱을 세 슬롯(127.0.0.1:8791/8792/8793) 사이에서 교체한다. Caddy는 고정 주소 127.0.0.1:8787을 받고, 전용 관리 주소는 다른 Caddy 인스턴스와 분리한 127.0.0.1:20191이다. cloudflared와 에이전트 MCP의 기존 진입점은 유지한다.

- 기본 설정은 `runtime_generation=false`, `deploy_strategy=idle`이다. 이 운영 저장소에는 사용자가 승인한 `deploy/generation-bootstrap.json`이 있으므로, 해당 커밋의 기존 idle 배포 성공 뒤 최초 전환이 자동으로 이어진다. 승인 파일이 없는 설치는 기존 동작을 유지한다.
- generation 앱은 매 기동마다 UUID를 등록하고 standby로 시작한다. standby는 Run 복구·STT 재등록·예열·색인을 수행하지 않는다.
- DB control 행의 CAS로 active 세대를 바꾸면 구세대가 draining이 된다. 기존 Run의 부모·CLI·stdout·stdio MCP는 구세대에 남는다.
- 신규 Run의 claim·전역 lane 상한·세션 순서는 `runtime_control`과 `runtime_leases`로 직렬화한다. desk 세션의 메시지별 병행 실행은 유지한다.
- 구세대 API로 들어온 신규 Run도 DB 큐에 저장된다. active 세대가 LISTEN/NOTIFY와 5초 fallback 조회로 이어 처리한다.
- 취소·컨텍스트 초기화는 소유자에게 DB 명령으로 전달한다. 실행 중 계정 예약·한도·사용량도 세대 전체를 반영한다.
- 이벤트의 원본은 DB다. SSE는 기존 cursor 계약을 유지하고 구세대에서 기본 45초 이후 EOF로 재접속을 유도한다. frontend는 end 없는 EOF에서 Last-Event-ID로 재접속하고 seq 중복을 제거하는 기존 구현을 사용한다.
- Run 이외의 STT·미리보기·벡터·주기 작업은 단일 Mac의 OS 파일 잠금으로 직렬화한다. 기존 작업을 끝낸 뒤 새 세대가 다음 회차를 수행한다. 잠금 파일은 프로세스 메모리/DB를 대체하는 이벤트 큐가 아니다.

각 앱은 Uvicorn 단일 프로세스다. 등록 단계에서 동시에 살아 있는 앱을 최대 2개로 제한한다. 구세대가 배출 중이면 후속 배포는 자동 대기한다. 아직 `requested`인 예약 중 조상관계와 배포 계약이 같은 커밋만 최신 누적 커밋에 합친다. 시작된 예약은 합치지 않는다.

## 독립 executor 첫 slice(2026-10-21, 운영 비활성)

API/감독 wire protocol1을 보존하고 executor 소유권 capability2를 별도로 기록한다.
`run_executor_mode=local`이 기본이며, remote API는 기존 DB 큐·명령·SSE만 사용한다.
별도 `ttakkari executor --activate`가 RunManager·엔진 자식을 소유한다. 실행 중 lease를
이관하지 않으며, 고아가 의심되면 자동 재실행 대신 기동을 거부한다.
독립 검증·운영 전환·rollback 조건은 [실행기 설계](run-executor/first-slice.md)를 따른다.
후속 구현은 [서비스 소유권·manifest·미완료 경계](run-executor/followup.md)에 기록한다. remote API는 Preview와 local STT durable consumer 및 maintenance loop를 시작하지 않고 executor가 기존 queue/작업 목록을 맡는다. TTS는 원문이 API 메모리에 남아 worker를 API가 계속 소유한다. request-time Qwen warm server도 세대별 API 소유를 유지한다. local mode와 기존 API drain 검사는 유지한다.

## 파일별 책임

| 파일 | 책임 |
|---|---|
| `generation.py` | 세대 등록·CAS·claim·fencing·전역 계정 예약·제어 명령·LISTEN·OS 작업 잠금 |
| `generation_service.py` | standby/active/draining 수명, 명령 소비, 주기 작업, readiness·유한 HTTP/후처리 집계 |
| `generation_deployment.py` | 커밋 archive·전용 venv/Node 런타임·격리 DB 검사·세 슬롯 launchd·Caddy 전환·rollback·재조정 |
| `deployment.py` | 기존 외부 예약/감독 유지, 예약 claim·병합, strategy에 따른 처리 분기 |
| `0030_runtime_generations.py` | 기존 Run 상태값을 유지하는 additive 소유권·명령 테이블과 요청 멱등 키 |
| `deploy/generation-contract.json` | 앱/명령 protocol, 목표 schema, 명시적으로 허용한 online migration 출발점, executor manifest raw-byte SHA-256 |

## 필수 설정

외부 감독과 슬롯의 설정 위치는 기존 `~/.ttakkari/config.env`를 유지한다. 실제 설정 파일은 이 작업에서 편집하지 않았다.

| 설정 | 값·용도 |
|---|---|
| `TTAKKARI_DEPLOY_SOURCE_ROOT` | clean commit을 읽을 실제 Git checkout의 절대 경로. 고정 tools 릴리스와 구분한다 |
| `TTAKKARI_DEPLOY_STRATEGY` | 최초 설치가 끝난 뒤 `generations` |
| `TTAKKARI_RUNTIME_GENERATION` | 슬롯 프로세스에서만 `true`. 기존 단일 backend에 단독으로 켜지 않는다 |
| `TTAKKARI_RUNTIME_API_URL` | 각 슬롯의 실제 loopback origin. 슬롯 launcher가 설정한다 |
| `TTAKKARI_RELEASE_REVISION` | 실행할 40자리 SHA. launcher가 설정하며 운영 generation의 필수값이다 |
| `TTAKKARI_RUNTIME_POLL_SECONDS` | 기본 5초. NOTIFY 누락·재접속 시 보완 조회 |
| `TTAKKARI_RUNTIME_SSE_DRAIN_SECONDS` | 기본 45초. 기존 SSE 조회 주기를 더한 범위에서 닫힐 수 있다 |
| DB 풀 | 앱당 기본 size 5 + overflow 3, LISTEN 전용 연결 1. 실제 PostgreSQL 연결 예산과 함께 조정 |

`deploy/install-services.sh all`은 기존 idle 설치용이다. generation 구성에서 이를 다시 실행하면 8787 점유가 충돌하므로 사용하지 않는다. 슬롯 plists는 로그인 시 자동 로드되는 LaunchAgents 폴더에 두지 않고 `<deploy>/services`에 둔다. 슬롯은 KeepAlive를 사용하지만, 배출 완료 때 `bootout`하므로 retired 슬롯이 자동으로 살아나지 않는다.

외부 감독은 각 조회 때 DB의 active 슬롯과 전용 프록시를 대조한다. 감독/로그인 세션이 다시 기동되면 active 릴리스만 복원한다. 이미 정상인 슬롯은 재시작하지 않으며, 메모리 설정과 저장된 proxy.json이 모두 맞으면 Caddy reload도 생략한다. draining/retired 슬롯과 이전 Run lease는 자동 복원·이전하지 않는다.

### 3슬롯과 연속 배포

`generation.PORTS`를 슬롯 수와 실행 세대 상한의 공통 기준으로 사용한다. 신규 접수 담당은 항상 한 세대이며, 나머지 최대 두 세대는 기존 작업을 마무리한다. 세대 수가 늘어도 Run lane/세션/계정 제한은 DB 전체에 걸쳐 유지한다. 동일 origin의 살아 있는 세대를 중복 등록하거나 네 번째 세대를 등록하는 것은 거절한다.

감독은 매 주기마다 이전 배포의 `draining`/`retiring` 정리를 진행하고, 별도로 전환 전 예약 한 건을 처리한다. 빈 슬롯이 없으면 `waiting_slot`로 남긴다. 오래된 배포를 정리할 때는 해당 `previous_generation` UUID만 확인하고, 이미 다음 배포로 넘어간 candidate를 다시 활성화하거나 프록시를 되돌리지 않는다. `current`/`previous`는 공개·로컬 전환 확인 때 기록하므로 늦게 끝난 이전 배포가 최신 기록을 덮어쓰지 않는다. 소유권 불명·rollback·recovery 상태에서는 후속 전환을 막는다.

전체 pytest는 보안 회귀 묶음(`-m security`)을 먼저 `security_gate` 단계에서 실행하고, 전체 실행은 `-m "not security" --sw`(stepwise)로 겹치지 않게 돈다. 첫 실패에서 멈추고, 재시도는 그 시험부터 다시 돌아 통과하면 뒤의 시험까지 끝까지 이어 간다. 같은 시험이 다시 실패하면 게이트가 실패하고, 뒤에서 처음 실패한 서로 다른 시험은 시험마다 한 번씩 재시도한다(최대 4회 실행). `--sw`는 시험 실패를 exit 2로 끝내므로 stepwise 캐시에 실패 시험이 기록됐을 때만 시험 실패로 보고, 수집 오류·단계 시간 초과·내부 오류는 재시도하지 않는다. stepwise 캐시는 배포 작업별 폴더(`jobs/<id>.pytest-cache`)에 두고 시작 때 지워 다른 후보·이전 실행과 섞이지 않는다. 직전 게이트가 실패한 시험은 `gate-last-failed.json`에 남겨 다음 게이트가 전체 실행 앞에서 먼저 돌린다(전체 실행에도 그대로 포함). 예전의 `-x` 첫 실행과 `--lf` 재시도는 재시도가 통과하면 중단 지점 뒤 시험이 한 번도 실행되지 않는 구멍이 있었다.

### 고정된 배포 감독 갱신

앱 배포만으로 `supervisor.json`의 고정 tools 버전은 바뀌지 않는다. 구버전 감독은 여전히 2슬롯과 순차 배출을 사용한다. 먼저 새 앱 SHA를 기존 `request_backend_deploy`로 배포한 뒤 **따까리 Run 밖의 호스트 터미널**에서 아래 명령을 한 번 실행한다. 실행 중인 앱 세대·Run·프록시는 재시작하지 않는다.

```sh
cd ~/.ttakkari/deploy/releases/<새 SHA>
.venv/bin/python -m ttakkari.deployment upgrade-supervisor --revision <새 SHA>
```

이 명령은 Run 환경에서는 거절한다. 테스트를 생략하지 않은 `draining`/`succeeded` 배포 기록, immutable 릴리스, 현재 active UUID와 readiness, 로컬·공개 버전 일치를 확인하고 배포 상태 변경 잠금 아래 감독 plist와 `supervisor.json`만 갱신한다. 이후 외부 `launchctl`로 감독만 다시 기동한다. 기존 launchd가 예전 plist를 메모리에 갖고 있어도 구 진입점이 새 marker를 읽어 새 릴리스로 exec한다. 동시 배포 작업이 잠금을 쓰고 있으면 변경 없이 거절한다.

명령의 `requested`는 실행 접수다. 새 감독이 실제 기동해 `controller.lock`을 획득하면 `supervisor-runtime.json`에 자신의 SHA·실행 경로·PID·8791/8792/8793 포트를 기록한다. 이 파일과 실제 세 번째 슬롯 전환까지 확인하기 전에는 운영 3슬롯 검증 완료로 보고하지 않는다. 이 최초 감독 교체는 Run 안에서 수행하지 않으며, 파일 덮어쓰기나 서비스 제어로 해당 제약을 우회하지 않는다.

## 최초 전환: 기존 안전 배포 이후 외부 설치 작업 1회

최초 전환은 무중단으로 취급하지 않는다. 기존 서버에는 소유권 계약이 없으므로 기존 외부 감독이 모든 Run 종료를 기다린다. migration 직전 전사·문서 변환이 실행 중이면 재시작을 진행하지 않고 배포를 실패 처리한다. 기존 버전에는 HTTP 요청·색인 전수 계측이 없으므로 최초 구버전 재시작까지를 새 세대 drain 보장에 포함하지 않는다. 따까리 내부 Run에서 launchctl·서비스 재시작을 실행하지 않는다.

### 2026-10-16 사용자 승인에 따른 자동 실행

`request_backend_deploy`로 clean main SHA를 예약하면 기존 감독이 검사·idle migration·재시작·공개 버전을 확인한다. 새 단일 앱은 consumer·복구·예열을 시작하지 않는 holding 상태로 기동하며, health/version 외 요청은 일시적으로 503과 Retry-After를 받는다. 감독은 작업 경계에서 새 코드를 읽고 같은 예약의 상태를 `bootstrapping`으로 바꾼다.

`generation_bootstrap.py`는 admission 배타 잠금 아래 고정 release/Python/Node 환경과 Caddy 구성을 준비하고 standby 슬롯을 시작한다. 기존 backend를 정리하고 로그인 자동 실행 plist를 `<deploy>/services/legacy-backend.plist`에 보관한다. Caddy의 로컬·공개 health/version, 인증된 조회, 기존 종료 Run의 SSE cursor를 확인한 뒤 DB 소유권을 활성화하고 다시 검증한다.

감독 plist와 재실행 경로는 별도 고정 tools 릴리스로 지정한다. 기존 launchd 프로세스도 마지막에 고정 Python/코드로 exec하며, loaded launchd 설정이 아직 checkout을 가리켜도 `supervisor.json`을 읽어 고정 경로로 들어간다. `generations-enabled.json`을 원자적으로 기록한 뒤 이후 예약을 generations 방식으로 처리한다. config.env의 비밀값을 읽어 출력하거나 해당 파일을 고치지 않는다.

활성화 전 실패는 standby·프록시를 정리하고 holding 앱을 정상 legacy 모드로 다시 기동한다. `bootstrap-failed.json`이 자동 재시도를 막는다. 활성화 시도 이후에는 새 세대가 작업을 맡았을 수 있으므로 legacy 복귀·자식 종료를 하지 않고 `recovery_pending`으로 남긴다. 완료/실패 결과는 전용 Web Push로 사용자에게 알리며 발송 결과는 예약에 남긴다.

관측 파일: `<deploy>/jobs/<ID>.json`, `bootstrap-smoke.json`, `generations-enabled.json`, `supervisor.json`, `proxy.json`. `succeeded`와 `bootstrap=true`, DB active UUID 및 실제 슬롯 readiness·공개 SHA를 함께 확인해야 운영 활성화로 판단한다. 예약 접수와 최초 idle 배포 중간 성공은 최종 운영 활성화가 아니다.

아래는 자동화가 수행하는 수동 복구용 기준 절차다.

1. 기존 외부 감독의 idle 경로로 이 코드와 0030 migration을 배포한다. 기존 감독이 새 구현을 읽었는지 확인한다. 이 단계만으로 generation 활성화가 되지는 않는다.
2. 실행할 커밋을 `Releases.archive`로 `<deploy>/releases/<SHA>`에 분리하고 `GenerationController.environment`로 고정 venv와 브라우저 MCP Node 의존성을 설치한다. lockfile을 사용하며 이미 준비한 환경은 다시 sync하지 않는다. 필요하면 같은 릴리스에서 격리 DB 검사를 재실행한다.
3. 외부 감독도 별도 검증된 tools 릴리스의 Python/코드로 고정한다. 서비스의 WorkingDirectory와 Python을 해당 릴리스로 정하고 `TTAKKARI_DEPLOY_SOURCE_ROOT`만 개발 checkout을 가리킨다. 운영 app의 파일 변경으로 감독 코드·venv가 덮어써지지 않아야 한다.
4. 공통 admission 잠금 아래 모든 기존 장기 작업·유한 요청 종료를 확인하고 기존 backend 서비스를 정리한다. 기존 backend의 로그인 자동 실행 plist도 별도 보관해 다음 로그인 때 8787을 다시 점유하지 않게 한다. 슬롯 0을 `Slots.start`로 시작하고 readiness에서 `protocol=1`, 기대 SHA·UUID·PID를 확인한다. `activate(candidate_uuid, expected=None)`로 최초 실행 담당자를 지정한다.
5. `proxy_config(0)`의 JSON을 `<deploy>/proxy.json`에 저장해 전용 Caddy launchd 서비스의 `--config`로 지정한다. 이후 검증된 라우팅을 같은 파일에 원자적으로 보존하므로 Caddy 재기동 때 마지막 슬롯을 사용한다. 다른 Caddy와 공유하는 기본 autosave는 비활성이다. 실행 파일은 `command -v caddy`로 확인하고 설정은 `caddy validate`로 먼저 검증한다. 외부 주소는 기존 tunnel이 받는 8787이다.
6. 로컬·공개 health/version, 인증된 읽기·가짜 Run 접수/완료, 이벤트 cursor와 중복 실행 여부를 확인한 뒤 `deploy_strategy=generations`로 후속 예약을 받는다. 원래 슬롯과 supervisor의 부팅 복구 동작도 확인한다.

최초 설치 자동화는 후속 사용자 승인으로 추가했다. launchd의 실제 등록/해제는 Run 내부 검증에서 실행하지 않으며 독립 감독이 운영에서 수행한다. 이 문서만으로 서비스가 이미 전환됐다고 판단하지 않는다.

## 이후 배포

개인 따까리 변경은 관련 검사 통과·clean commit·push 후 별도 배포 요청을 기다리지 않고 예약한다. 사용자가 명시적으로 배포를 보류한 경우는 따른다. 진행 중 Run의 존재는 세대별 예약·전환의 차단 조건이 아니며, 예약한 Run 자신도 종료할 필요가 없다. 배포 완료만 기다리며 Run을 붙잡아 두지는 않는다.

커밋·푸시 후 기존 `request_backend_deploy(revision=...)` 경로를 그대로 쓴다. API는 실제 source checkout의 clean HEAD를 확인한다. 예약 이후 source가 바뀌어도 이미 지정한 커밋의 archive는 바뀌지 않는다.

`preparing/building/testing → prepared → starting → switching → draining → retiring → succeeded`로 진행한다. `draining`은 공개 경로가 새 SHA로 확인된 상태이고, `succeeded`는 구세대 종료까지 확인한 상태다. 같은 SHA의 중복 예약은 동일 ID를 돌려주며, 병합된 예약은 `superseded_by`를 남긴다.

새 세대가 active인 동안에도 구세대의 Run·장기 작업·후처리·유한 HTTP가 남아 있으면 종료하지 않는다. 앱 세대가 둘인 동안 세 번째 앱은 띄우지 않는다. 따라서 장기 Run 동안 여러 배포를 요청하면 첫 전환 뒤 후속 전환이 지연될 수 있지만, 사용자가 재요청할 필요는 없다.

릴리스 삭제 자동 GC는 아직 넣지 않았다. 디스크가 부족하면 준비 단계가 실패하고 기존 앱을 보존한다. active·draining·rollback 대상과 그 자식/MCP/hook이 참조하는 릴리스는 지우지 않는다. 모델 파일과 STT 전용 Python은 기존 공용 위치를 사용하며 앱 코드는 릴리스 경로로 고정된다.

## 실패와 복구

- 빌드·검사·schema 계약 실패는 기존 active 세대를 바꾸지 않는다. 공개 확인/프록시 전환 실패는 직전 세대로 신규 요청·claim을 복귀시킨다. 후보가 이미 맡은 Run은 후보에서 배출한다.
- DB CAS와 프록시 reload는 원자적이지 않다. journal의 후보·직전 UUID를 먼저 저장하고 실제 DB·프록시 상태를 읽어 반복 실행해도 수렴하도록 한다. 구세대를 종료한 직후 감독이 죽은 경우도 마지막 retiring 상태와 OS 생존 잠금·DB 소유권을 대조한다.
- heartbeat 만료로 소유권을 빼앗지 않는다. 살아 있는 앱의 전역 startup recovery를 금지하며, generation control이 남아 있으면 legacy recovery도 거절한다.
- active 슬롯의 같은 릴리스가 다시 기동되면 새 UUID가 신규 접수를 맡는다. 종료된 부모의 기존 Run lease는 새 UUID로 이전하지 않는다. 불확실한 엔진 작업은 격리된 채 유지하며 실제 자식 종료 확인 후 별도 복구한다.
- 감독은 슬롯의 실제 boot UUID가 DB active와 일치할 때만 프록시 경로를 복원한다. 응답 중인 전용 admin이 예상 구성과 다르거나 다른 프로세스가 슬롯을 점유하면 강제 종료·덮어쓰기를 하지 않고 오류를 남긴다. Mac 재부팅 때는 기존 엔진 작업까지 유지된다는 의미가 아니다.
- 부모 충돌 뒤 running STT/processing preview가 남아 있으면 새 추론/변환을 시작하지 않는다. 파일 잠금만 풀렸다고 자식이 끝났다고 간주하지 않는다. 실제 PID·시작 시각·그룹과 파일 상태를 확인하기 전에는 해당 큐를 재등록하지 않는다.
- `recovery_pending`은 자동 조정으로 안전을 확인하지 못한 상태다. 원인이 해소되기 전에는 후속 예약을 활성화하지 않는다. 관측 불능을 작업 완료로 기록하지 않는다.
- 외부 부작용의 exactly-once, 실행 중 함수 메모리 이전, Mac/DB/전원 장애에 대한 무중단은 보장 범위 밖이다. DB fencing은 오래된 쓰기를 거절하는 역할이다.

### 컷오버 완료 후 drain `recovery_pending` 진단과 명시적 복구 (2026-10-30)

- `runtime_report` 및 읽기 전용 CLI `python -m ttakkari.deployment diagnose [--no-db]`는 비밀·로컬 경로 노출 없이 다음 세 가지를 함께 보고한다.
  1. `supervisor`: `supervisor.json` 핀 SHA·릴리스 유효성, `supervisor-runtime.json` 실행 SHA·PID 생존·갱신 경과(`age_s`), 핀-실행 일치 여부, `controller.lock`·`backend-restart.lock` 보유 여부, `proxy.json` 저장 슬롯.
  2. `deploy_queue`: `RECENT_DEPLOYS`(최근 5건) 밖으로 밀려난 오래된 작업을 포함해 전체 미종료 배포에서 `draining_jobs`, `foreground_job`, `blocking_jobs`(`recovery_pending`/`rolling_back`), `blocked_jobs`(차단된 후속 `requested`/`waiting_*`)와 `cutover_blocked` 여부.
  3. `slots`: `8791/8792/8793` 슬롯별 프록시 지정 여부, active 소유 세대, 살아 있는 세대(`lock_held`, `leases`, `process_groups`).
- 이미 전환을 마친 배포(`activated_revision == revision`)가 구세대 `drain` 조회 중 일시적 미응답 등으로 `recovery_pending`이 되면, `pending_jobs()`의 단일 `foreground` 자리와 `serve()`의 `blocked` 가드를 동시에 점유해 후속 `requested` 배포를 멈춘다. 이때 가드를 풀거나 자동 supersede하지 않고, **따까리 Run 밖의 호스트 터미널**에서 명시적 fence를 지정해 복구한다.
  - 구세대 슬롯이 `/api/ready`에서 동일 `previous_generation`·`previous_revision`으로 응답하면(장기 GAME Run 등 `leases > 0` 포함), 슬롯을 정지하지 않고 배포 상태만 `draining`으로 복귀시킨다(`action="resume_draining"`). 기존 Run은 그대로 보존되고 후속 배포는 남은 빈 슬롯으로 즉시 진행된다.
  - 구세대 슬롯이 응답하지 않으면, DB `RuntimeLease == 0`, `RuntimeProcessGroup == 0`, OS `runtime/<id>.lock` 미보유, 기록된 PID 종료가 모두 증명될 때만 DB 세대 상태를 `offline`으로 정리하고 `succeeded`로 마감한다(`action="retire_verified_dead"`). 하나라도 남아 있으면 거절한다.
- **순환 의존(부트스트랩) 해소 절차**:
  - 아직 배포되지 않은 새 코드의 `runtime_report` 확장은 구배포 API에 즉시 반영되지 않으며, 구버전 감독(`e817fc19` 등)이 `recovery_pending`에 막혀 있으면 새 커밋도 곧바로 배포될 수 없다.
  - 따라서 (1) 호스트에서 새 코드 경로의 Python으로 읽기 전용 `python -m ttakkari.deployment diagnose`를 실행해 핀/런타임 SHA와 현재 `epoch`·세대·슬롯 fence를 확인하고, (2) 같은 경로에서 `python -m ttakkari.deployment recover-drain --job-id <id> --expected-revision <sha> --expected-previous-generation <uuid> --expected-previous-revision <sha> --expected-previous-slot <slot> --expected-candidate-generation <uuid> --expected-active-generation <uuid> --expected-active-revision <sha> --expected-epoch <epoch> [--apply]`를 실행한다(dry-run 선행 후 `--apply`).
  - `abcb`가 `draining`(또는 조건 충족 시 `succeeded`)으로 전이되면 구버전 감독의 `pending_jobs()`도 즉시 해당 작업을 `drains`로 옮기고 `blocked` 가드를 해제하므로, 대기 중이던 후속 배포가 정상 전환된다. 새 릴리스가 `active`가 된 뒤 `upgrade-supervisor --revision <새 SHA>`로 감독 프로세스까지 갱신한다.

schema 계약이 다르면 명시된 `online_migration_from`에서만 online migration을 허용한다. 기존 테이블을 바꾸지 않는 migration만 온라인 출발점을 명시한다. 현재 `0034_knowledge_ledger`에서 `0035_qwen_token_usage`로는 독립 사용량 테이블을 추가하므로 온라인 전환을 허용한다. 새 migration의 head와 배포 계약이 어긋나면 회귀 검사에서 실패한다. online migration 연결에는 2초 lock timeout과 30초 statement timeout을 적용한다. 파괴적 변경은 기존 안전 전환 절차로 별도 처리한다. DB migration을 자동 downgrade해 rollback하지 않는다.

계약의 `schema_aliases`는 이름이 바뀌어 저장소에 더는 없는 옛 리비전 ID를 알려진 리비전에 대응시킨다. 별칭은 "물리 스키마가 대상 리비전과 같다"는 보증이므로 실제 DB를 대조해 확인한 경우에만 추가한다(2026-10-21: 운영 DB가 커밋된 적 없는 `0059_kb_immutability_truncate`로 찍혀 있었고, 테이블·컬럼·인덱스·트리거 대조 결과 `0059_collector_intake` 상태였다). 정렬은 후보 릴리스의 `alembic/env.py`(`ttakkari.schema_aliases.realign`)가 migration 시작 전에 수행한다. 감독은 릴리스보다 오래된 코드로 돌 수 있어(2026-10-21 기준 `e817fc19` 고정) `schema_aliases`를 모른 채 `online_migration_from`만 본다. 그래서 별칭 키는 `online_migration_from`에도 함께 적고, 별칭 정렬을 감독 코드에 두지 않는다.

온라인 범위 밖의 스키마는 예약을 `failed`로 끝내지 않고 `waiting_idle`로 둔다. 검사는 이미 통과했으므로 릴리스·검사 바인딩을 보존해 재검사 없이 기다린다. 대기 중에는 새 Run 접수를 막지 않고, 실행 중 Run이 0이 되는 순간 접수 배타 잠금을 얻어 일반(비 online) migration과 컷오버를 마친 뒤 잠금을 놓는다. 활성 작업은 종료시키지 않는다. `backend_deploy_status`·`ops_list_deploys`·`ops_deploy_log`는 `active_runs`, `idle_since`, `idle_reason`(`wait_reason`)을 보여준다. 대기 중 더 새 예약이 같은 계약 안에서 들어오면 이전 `waiting_idle` 예약은 `superseded`된다. 유휴 이행 migration 자체가 실패하면 그때는 실제 오류와 함께 `failed`로 기록한다.

## 검증 범위

새 테스트는 두 개의 **실제 ASGI 프로세스**, 실제 PostgreSQL, 실제 Caddy와 가짜 CLI를 사용한다. 기동·전환·구세대 CLI PID 유지·신규 작업·원격 취소·SSE cursor 재개, 전역 슬롯/세션 순서, DB 명령 중복 제거, rollback/감독 중단, 변경된 사용량 공유와 stale 쓰기 거절을 다룬다.

실제 Claude/Codex/Antigravity 유료 실행, 운영 launchd/tunnel, 로컬 STT 모델 추론, 업로드 중 운영 전환, 대규모 SSE·CPU/RSS 측정은 미실행이다. 통과한 테스트의 범위를 운영 무중단 보증으로 확대하지 않는다.

실행:

```sh
uv sync --frozen
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/<새로_만든_격리DB> uv run pytest -q
uv run ruff check src tests alembic
```

0030은 독립 DB에서 upgrade → downgrade(0029) → upgrade를 검사하고 `runs`·`runtime_*` 테이블의 모델 차이가 없는지 확인한다. 전체 `alembic check`가 보고하는 맥락/회의의 raw vector·lexical 인덱스 11개는 기존 baseline 코드에도 같은 차이가 있다. 이번 변경에서 삭제하거나 비교 결과를 통과로 바꾸지 않았다.
