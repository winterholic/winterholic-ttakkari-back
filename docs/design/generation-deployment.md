# 세대별 배포 구현과 운영 전환

2026-10-16. 코드 구현·격리 검증 단계이며 운영 활성화는 하지 않았다.

## 동작

기존 앱을 두 슬롯(127.0.0.1:8791/8792) 사이에서 교체한다. Caddy는 고정 주소 127.0.0.1:8787을 받고, 전용 관리 주소는 다른 Caddy 인스턴스와 분리한 127.0.0.1:20191이다. cloudflared와 에이전트 MCP의 기존 진입점은 유지한다.

- 기본 설정은 `runtime_generation=false`, `deploy_strategy=idle`이다. 코드 배포만으로 기존 서비스 구성이 바뀌지 않는다.
- generation 앱은 매 기동마다 UUID를 등록하고 standby로 시작한다. standby는 Run 복구·STT 재등록·예열·색인을 수행하지 않는다.
- DB control 행의 CAS로 active 세대를 바꾸면 구세대가 draining이 된다. 기존 Run의 부모·CLI·stdout·stdio MCP는 구세대에 남는다.
- 신규 Run의 claim·전역 lane 상한·세션 순서는 `runtime_control`과 `runtime_leases`로 직렬화한다. desk 세션의 메시지별 병행 실행은 유지한다.
- 구세대 API로 들어온 신규 Run도 DB 큐에 저장된다. active 세대가 LISTEN/NOTIFY와 5초 fallback 조회로 이어 처리한다.
- 취소·컨텍스트 초기화는 소유자에게 DB 명령으로 전달한다. 실행 중 계정 예약·한도·사용량도 세대 전체를 반영한다.
- 이벤트의 원본은 DB다. SSE는 기존 cursor 계약을 유지하고 구세대에서 기본 45초 이후 EOF로 재접속을 유도한다. frontend는 end 없는 EOF에서 Last-Event-ID로 재접속하고 seq 중복을 제거하는 기존 구현을 사용한다.
- Run 이외의 STT·미리보기·벡터·주기 작업은 단일 Mac의 OS 파일 잠금으로 직렬화한다. 기존 작업을 끝낸 뒤 새 세대가 다음 회차를 수행한다. 잠금 파일은 프로세스 메모리/DB를 대체하는 이벤트 큐가 아니다.

각 앱은 Uvicorn 단일 프로세스다. 등록 단계에서 동시에 살아 있는 앱을 최대 2개로 제한한다. 구세대가 배출 중이면 후속 배포는 자동 대기한다. 아직 `requested`인 예약 중 조상관계와 배포 계약이 같은 커밋만 최신 누적 커밋에 합친다. 시작된 예약은 합치지 않는다.

## 파일별 책임

| 파일 | 책임 |
|---|---|
| `generation.py` | 세대 등록·CAS·claim·fencing·전역 계정 예약·제어 명령·LISTEN·OS 작업 잠금 |
| `generation_service.py` | standby/active/draining 수명, 명령 소비, 주기 작업, readiness·유한 HTTP/후처리 집계 |
| `generation_deployment.py` | 커밋 archive·전용 venv/Node 런타임·격리 DB 검사·두 슬롯 launchd·Caddy 전환·rollback·재조정 |
| `deployment.py` | 기존 외부 예약/감독 유지, 예약 claim·병합, strategy에 따른 처리 분기 |
| `0030_runtime_generations.py` | 기존 Run 상태값을 유지하는 additive 소유권·명령 테이블과 요청 멱등 키 |
| `deploy/generation-contract.json` | 앱/명령 protocol, 목표 schema, 명시적으로 허용한 online migration 출발점 |

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

## 최초 전환: 기존 안전 배포 이후 외부 설치 작업 1회

이 절차는 **아직 운영 실행하지 않았다**. 기존 코드에는 소유권 계약이 없으므로 최초 전환은 무중단으로 취급하지 않는다. 실제 LLM Run·전사·변환·색인·업로드가 끝났는지 확인한 뒤 진행해야 한다. 따까리 내부 Run에서 launchctl·서비스 재시작을 실행하지 않는다.

1. 기존 외부 감독의 idle 경로로 이 코드와 0030 migration을 배포한다. 기존 감독이 새 구현을 읽었는지 확인한다. 이 단계만으로 generation 활성화가 되지는 않는다.
2. 실행할 커밋을 `Releases.archive`로 `<deploy>/releases/<SHA>`에 분리하고 `GenerationController.environment`로 고정 venv와 브라우저 MCP Node 의존성을 설치한다. lockfile을 사용하며 이미 준비한 환경은 다시 sync하지 않는다. 필요하면 같은 릴리스에서 격리 DB 검사를 재실행한다.
3. 외부 감독도 별도 검증된 tools 릴리스의 Python/코드로 고정한다. 서비스의 WorkingDirectory와 Python을 해당 릴리스로 정하고 `TTAKKARI_DEPLOY_SOURCE_ROOT`만 개발 checkout을 가리킨다. 운영 app의 파일 변경으로 감독 코드·venv가 덮어써지지 않아야 한다.
4. 공통 admission 잠금 아래 모든 기존 장기 작업·유한 요청 종료를 확인하고 기존 backend 서비스를 정리한다. 슬롯 0을 `Slots.start`로 시작하고 readiness에서 `protocol=1`, 기대 SHA·UUID·PID를 확인한다. `activate(candidate_uuid, expected=None)`로 최초 실행 담당자를 지정한다.
5. `proxy_config(0)`의 JSON으로 전용 Caddy launchd 서비스를 설치한다. Caddy 실행 파일은 `command -v caddy`로 확인하고 설정은 `caddy validate`로 먼저 검증한다. 외부 주소는 기존 tunnel이 받는 8787이다.
6. 로컬·공개 health/version, 인증된 읽기·가짜 Run 접수/완료, 이벤트 cursor와 중복 실행 여부를 확인한 뒤 `deploy_strategy=generations`로 후속 예약을 받는다. 원래 슬롯과 supervisor의 부팅 복구 동작도 확인한다.

최초 설치 자동화 전체와 launchd의 실제 등록/해제는 이번 격리 시험 범위에 포함하지 않았다. 설치 시 현재 서비스·포트·기존 설정을 확인해야 하며, 이 문서는 서비스가 이미 전환됐다는 기록이 아니다.

## 이후 배포

커밋·푸시 후 기존 `request_backend_deploy(revision=...)` 경로를 그대로 쓴다. API는 실제 source checkout의 clean HEAD를 확인한다. 예약 이후 source가 바뀌어도 이미 지정한 커밋의 archive는 바뀌지 않는다.

`preparing/building/testing → prepared → starting → switching → draining → retiring → succeeded`로 진행한다. `draining`은 공개 경로가 새 SHA로 확인된 상태이고, `succeeded`는 구세대 종료까지 확인한 상태다. 같은 SHA의 중복 예약은 동일 ID를 돌려주며, 병합된 예약은 `superseded_by`를 남긴다.

새 세대가 active인 동안에도 구세대의 Run·장기 작업·후처리·유한 HTTP가 남아 있으면 종료하지 않는다. 앱 세대가 둘인 동안 세 번째 앱은 띄우지 않는다. 따라서 장기 Run 동안 여러 배포를 요청하면 첫 전환 뒤 후속 전환이 지연될 수 있지만, 사용자가 재요청할 필요는 없다.

릴리스 삭제 자동 GC는 아직 넣지 않았다. 디스크가 부족하면 준비 단계가 실패하고 기존 앱을 보존한다. active·draining·rollback 대상과 그 자식/MCP/hook이 참조하는 릴리스는 지우지 않는다. 모델 파일과 STT 전용 Python은 기존 공용 위치를 사용하며 앱 코드는 릴리스 경로로 고정된다.

## 실패와 복구

- 빌드·검사·schema 계약 실패는 기존 active 세대를 바꾸지 않는다. 공개 확인/프록시 전환 실패는 직전 세대로 신규 요청·claim을 복귀시킨다. 후보가 이미 맡은 Run은 후보에서 배출한다.
- DB CAS와 프록시 reload는 원자적이지 않다. journal의 후보·직전 UUID를 먼저 저장하고 실제 DB·프록시 상태를 읽어 반복 실행해도 수렴하도록 한다. 구세대를 종료한 직후 감독이 죽은 경우도 마지막 retiring 상태와 OS 생존 잠금·DB 소유권을 대조한다.
- heartbeat 만료로 소유권을 빼앗지 않는다. 살아 있는 앱의 전역 startup recovery를 금지하며, generation control이 남아 있으면 legacy recovery도 거절한다.
- active 슬롯의 같은 릴리스가 다시 기동되면 새 UUID가 신규 접수를 맡는다. 종료된 부모의 기존 Run lease는 새 UUID로 이전하지 않는다. 불확실한 엔진 작업은 격리된 채 유지하며 실제 자식 종료 확인 후 별도 복구한다.
- 부모 충돌 뒤 running STT/processing preview가 남아 있으면 새 추론/변환을 시작하지 않는다. 파일 잠금만 풀렸다고 자식이 끝났다고 간주하지 않는다. 실제 PID·시작 시각·그룹과 파일 상태를 확인하기 전에는 해당 큐를 재등록하지 않는다.
- `recovery_pending`은 자동 조정으로 안전을 확인하지 못한 상태다. 원인이 해소되기 전에는 후속 예약을 활성화하지 않는다. 관측 불능을 작업 완료로 기록하지 않는다.
- 외부 부작용의 exactly-once, 실행 중 함수 메모리 이전, Mac/DB/전원 장애에 대한 무중단은 보장 범위 밖이다. DB fencing은 오래된 쓰기를 거절하는 역할이다.

schema 계약이 다르면 명시된 `online_migration_from`에서만 online migration을 허용한다. 기본값은 빈 목록이다. online migration 연결에는 2초 lock timeout과 30초 statement timeout을 적용한다. 파괴적 변경은 기존 안전 전환 절차로 별도 처리한다. DB migration을 자동 downgrade해 rollback하지 않는다.

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
