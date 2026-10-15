# 인프라 관제 디자인 선행 조사 — 2026-10-15

이 문서는 조건부 디자인 시안에 사실로 반영할 수 있는 운영·코드 근거와 아직 결정되지 않은 항목을 분리한다. 읽기 전용 공개 API, 알려진 SSH 별칭, 현재 저장소만 조회했다. 인증 파일·키·쿠키를 열람하거나 권한을 높이지 않았다.

## 1. 판정 요약

- **실제 확인:** 알려진 읽기 전용 SSH 별칭으로 한 Ubuntu 서버에 접속 가능했다. Prometheus와 Loki의 로컬 API가 응답했고, Prometheus scrape target 3개가 모두 `up`이었다. Grafana health endpoint는 HTTP 200, Dozzle 경로는 HTTP 307 redirect를 반환했다.
- **미확인:** 실제 컨테이너 실행 목록은 Docker API 권한 거부로 확인하지 못했다. Prometheus의 현재 target 목록은 exporter 수집 상태이며 앱의 HTTP 가용성을 뜻하지 않는다. `win-server`는 이번에 접속하지 않았다.
- **실행 설정과 저장소 차이:** Prometheus 보존기간은 런타임 `10y/100GiB`, 로컬 Compose는 `30d/8GB`다. Loki 런타임은 `90d`, 로컬 `loki-config.yml`은 `14d`다. 런타임 값이 화면의 사실 기준이며, 저장소 설정 불일치는 별도 정리가 필요하다.
- **상태 임계값:** `3회 연속 실패`, `5분 오래됨`, `30분 지속`은 코드/관측 기준에서 확인하지 못했다. 디자인 시안의 예시값으로만 남겨야 한다. 기존 리소스 게이지와 일일 보고서에는 다른 범위의 기준이 있으나, 이를 전체 서비스 상태에 전용하면 안 된다.
- **정식 WBS 완료:** I1.4만 완료 기준을 충족한다. I1.1~I1.3은 부분 조사, I2.1~I2.3은 선행/판단 미충족으로 완료 처리하지 않는다.

## 2. 원문 5개 질문 확인 결과

계획 원본 `2026-10-14-plan-infra-control-wbs.csv`와 HTML은 I3.1의 완료 기준으로 “5개 핵심 질문”을 요구하지만 질문 목록을 싣지 않는다. 이전 작업 기록 `docs/work-history/2026-10-15-infra-control-design-prototype.md`와 지정된 세션 `b200943e-e22c-4f1e-b63d-3dac50251447`에서도 사용자 원문 목록은 확인되지 않았다. 따라서 아래 문장은 **사용자 원문이 아니라 기존 시안 spec이 도출한 제안**이다.

| # | 시안의 제안 질문 | 사실 반영에 필요한 최소 데이터 |
|---|---|---|
| 1 | 지금 전체가 괜찮은가 | 관측 가능한 호스트/서비스 범위, 각 출처의 확인 시각, 수집 실패와 서비스 실패의 구분 |
| 2 | 어디가 문제인가 | 안정적인 host/service 식별자와 실제 서비스 health 출처 |
| 3 | 얼마나 심각하고 언제부터인가 | 근거 있는 영향·심각도·지속시간 기준. 현재 전역 기준 없음 |
| 4 | 무엇을 먼저 확인하나 | 원천 데이터가 뒷받침하는 판정 근거와 다음 확인 절차. 자동 조치 아님 |
| 5 | 더 보려면 어디로 가나 | 상세에 연결할 수 있는 확인된 Grafana/Dozzle 경로와 접근 조건 |

시안 파일의 이 목록은 spec.md §2.1에 있다. 기획 원문에 질문이 있었다고 인용하지 말 것.

## 3. 실제 운영 인벤토리 표본

확인 시각은 KST 기준 2026-10-15 21:59~22:00이다. 기존 `homeserver` 기록은 후보 식별에 사용하고, 변동 가능한 값은 런타임 조회로 대조했다.

| 대상 | 현재 확인 | 한계 |
|---|---|---|
| SSH 연결 호스트 | `ssh hs-llm-read` 성공. 원격 `hostname` 출력은 `winterholic` | 문서/Prometheus에서는 `ubuntu-01`이라 표준 ID가 일치하지 않음. SSH 별칭의 네트워크 경로는 조사하지 않음 |
| 호스트 서비스 | `systemctl --type=service --state=running`에서 37개 unit | Docker 컨테이너와 앱 역할·중요도는 포함하지 않음 |
| Ttakkari Mac Studio | MCP `server_resources`로 현재 스냅샷 확인: `winterholicui-MacStudio.local`, macOS 27.0, M5 Max, CPU 10.4%, 메모리 38.2%, 디스크 62.4%, pressure `normal` | Mac의 순간 리소스 스냅샷이며 다른 호스트/서비스 인벤토리가 아님 |
| `win-server` | 기존 문서의 후보 호스트 이름 | 이번 실행에서 연결 경로·실행 서비스 확인 안 함 |
| 중요도 | 확인된 통합 중요도 라벨 없음 | 서비스 중요도/업무 영향은 결정되지 않음 |

### Ubuntu 호스트에서 확인된 systemd running unit

```text
cloudflared, containerd, cron, dbus, docker, fail2ban, fwupd, getty@tty1,
mariadb, ModemManager, mongod, multipathd, netdata, nginx, pm2-winterholic,
polkit, postgresql@16-main, redis-server, rsyslog, smartmontools, snapd, ssh,
systemd-journald, systemd-journald@netdata, systemd-logind, systemd-networkd,
systemd-resolved, systemd-timesyncd, systemd-udevd, tailscaled, thermald,
udisks2, unattended-upgrades, upower, user@1000, user@1003, user@1004
```

Docker API 요청은 다음처럼 거부됐다. `sudo` 등으로 우회하지 않았다.

```text
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
DOCKER_EXIT:1
```

Prometheus에서 실행한 읽기 전용 `count by (name) (container_last_seen)` 쿼리는 이름이 붙은 40개 label group과 이름 없는 group(값 69)을 반환했다. 이는 Docker의 authoritative `docker ps` 결과가 아니며, 실행 중·정상 컨테이너 수로 표시하면 안 된다. 이름 label 표본:

```text
ariari-redis, facereview-front-app, infisical-redis-1, monitoring-prometheus,
monitoring-dozzle, nextcloud-app, ariari-backoffice, infisical-backend-1,
memoir-frontend, monitoring-loki, monitoring-cadvisor, stock-redis,
ariari-backend, monitoring-grafana, infisical-db-1, nextcloud-redis,
stock-front, stock-timescaledb, coffee-loser-react, mbtid-db-1, nextcloud-cron,
monitoring-promtail, mbtid-backend-1, monitoring-node-exporter, nextcloud-db,
stock-collector, stock-movers, stock-discord-bot, stock-api,
monitoring-discord-bot, facereview-celery-beat, facereview-app,
facereview-celery-worker, tour-gacha-api, tour-gacha-scheduler,
cctv-bridge, cctv-mqtt, cctv-front, cctv-frigate, memoir-backend
```

## 4. 관측 스택과 현재 응답

| 출처/경로 | 확인된 사실 | 화면에서 의미하는 것 |
|---|---|---|
| Prometheus `http://127.0.0.1:9090/-/healthy` (SSH 원격 명령으로 host loopback에서 조회) | `Prometheus Server is Healthy.` | Prometheus 프로세스/API 응답 상태 |
| `/api/v1/targets?state=active` | `cadvisor / ubuntu-01`, `node / ubuntu-01`, `prometheus / localhost:9090` 모두 `up`; 마지막 scrape는 2026-10-15 22:00 KST 전후 | exporter scrape 성공. 앱의 HTTP health나 사용자 영향은 아님 |
| `/api/v1/status/flags` | 런타임 `storage.tsdb.retention.time=10y`, `size=100GiB` | 실제 실행 설정 |
| 로컬 `infra/monitoring/docker-compose.yml` | Prometheus 설정 플래그는 `30d`, `8GB` | 런타임과 불일치. 로컬 파일을 현재 설정으로 오인하지 않음 |
| Loki `http://127.0.0.1:3100/ready`, `/config` | `ready`; 런타임 설정 `retention_period: 90d` | Loki API 응답과 런타임 보존기간 |
| 로컬 Loki 설정 | `loki-config.yml`은 `336h`(14일), `README.md`는 90일 | 문서/파일도 서로 다름. 런타임 조회 결과가 90일 |
| Loki `/loki/api/v1/labels` | `compose_project`, `compose_service`, `container`, `filename`, `host`, `job`, `service_name`, `stream` | 로그 라벨 이름은 확인. 로그 본문은 읽지 않음 |
| Prometheus `/api/v1/rules` | 성공, groups=0, rules=0 | 현재 이 API에서 읽힌 alert/rule 없음 |
| 알려진 Grafana `https://grafana.winterholic.net/api/health` | HTTP 200 | 기존 Grafana health endpoint가 응답 |
| 알려진 Dozzle `https://dozzle.winterholic.net/` | HTTP 307 redirect | 경로가 응답하고 로그인 경로로 이동. 인증/로그 내용은 확인하지 않음 |
| `homeserver-report.timer` | `active`; 마지막 trigger 20:00, 다음 00:00 KST | 주기 보고 타이머가 활성. 보고서 내용의 실제 발송/사용성은 이번에 검사하지 않음 |

Prometheus 설정은 `scrape_interval: 15s`, 외부 host label `ubuntu-01`, job은 `prometheus`, `node`, `cadvisor`다. 새 job으로 적힌 Blackbox exporter는 주석의 향후 계획이다. HTTP endpoint가 정상인지는 이 3개 scrape만으로 판단할 수 없다.

조회 예시와 빈 응답도 확인했다.

```text
/api/v1/query?query=count%20by%20(name)%20(container_last_seen)
→ status=success; 이름이 붙은 group 40개와 이름 없는 group 1개
/api/v1/query?query=infra_control_readonly_probe_20261015
→ status=success; series=0; error 없음
```

두 번째 응답은 “값 0”이나 “서비스 다운”이 아니라 일치하는 시계열이 없다는 뜻이다. source 장애, 미수집, 실제 0, 이름/라벨 매핑 실패를 구분할 UI 근거가 있어야 한다.

## 5. 기존 Ttakkari 리소스 UI/API/AI 경로 (I1.4)

| 층 | 기존 계약/위치 | 사실 및 한계 |
|---|---|---|
| 화면 | `front/src/App.tsx`의 `/resources`; `ResourcesPanel.tsx` | 현재 Mac 리소스 전용. 마운트 중 4초마다 요청 |
| API·DTO | `GET /api/system/resources`; `SystemResources` | 인증된 사용자가 현재 Mac의 CPU·memory·disk·network·process names를 읽음. `collected_at` 포함 |
| 수집기 | `back/src/ttakkari/sysinfo.py` | 2초 cache, 0.5초 sampling. DTO에는 수집시각이 있지만 화면에는 수집 age를 보여주지 않음 |
| 실패 화면 | `ResourcesPanel.tsx` | 최초 실패는 오류 표시. 이전 데이터가 있으면 “갱신 실패, 마지막 값”을 표시하지만 마지막 값의 나이는 표시하지 않음 |
| AI/MCP | `GET /api/agent/system/resources` → 같은 `sysinfo.snapshot()`; MCP `server_resources` | 현재 Mac 값만 제공하며 컨테이너·Ubuntu·Windows 조회 기능은 없음 |

따라서 기존 `/resources`는 재사용할 **Mac host 카드/실패 표시 패턴**이다. fleet service 상태 API로 오인하거나, MCP 도구가 다른 호스트를 이미 읽는다고 표현하면 안 된다.

## 6. 상태/심각도/신선도 근거와 확정 경계

| 관측 | 기존 기준 | 적용 범위 |
|---|---|---|
| `/resources` 게이지 | 프런트 `ResourcesPanel.tsx`: 80% warning, 90% danger; memory pressure enum은 OS `normal/warn/critical` | 현재 Mac 리소스 게이지에만 존재. 서비스/전체 상태 threshold 아님 |
| `daily-report.sh` load | 15분 load average를 코어 수로 정규화, 200/350 경계 | 별도 요약 보고서의 load 지표 |
| `daily-report.sh` CPU | 3초 delta, 85/95 경계 | 별도 요약 보고서의 순간 CPU 판정 |
| `daily-report.sh` memory | 85/93 경계 | 별도 요약 보고서의 Ubuntu 메모리 판정 |
| `daily-report.sh` 컨테이너 | down/unhealthy면 위험 표시; 재시작 수는 24시간 창에서 1/6 경계 | Docker 권한이 있는 보고 스크립트 범위. 브라우저/API 권한으로 확인하지 않음 |
| Prometheus target | `up` 및 scrape timestamp | 수집원 상태·신선도. 서비스 자체 health 아님 |
| Loki readiness | `/ready` 응답 | Loki 프로세스 readiness. 개별 로그 수집 누락 없음의 증명은 아님 |
| 조건부 시안 숫자 | 3회 실패, 5분 stale, 30분 지속 | 운영 기준/사용자 결정으로 확인되지 않음 |

첫 상태 화면에 필요한 사실 규칙은 다음과 같다.

1. **서비스 자체 health를 확인할 원천이 없으면 `미확인`**이다. exporter `up`만으로 서비스 `정상`을 만들지 않는다.
2. **관측 출처의 실패/오래된 값은 서비스 장애와 분리**한다. 수집 실패 하나로 모든 서비스에 장애 배지를 붙이지 않는다.
3. **빈 시계열은 0이 아니다.** 미수집·미지원·실제 0을 구분할 수 없으면 정상 판정에서 제외하고 그 사유를 보여준다.
4. **확인시각은 출처별**이다. 현재 설정의 scrape 주기(15초)와 UI에서 선택할 stale 허용치는 다른 항목이며 후자는 미확정이다.
5. 임계값과 유지시간은 기준선·사용자 판단 전까지 미정이다. 이번 조사로 새 숫자를 확정하지 않는다.

## 7. 기존 링크·최소 재사용 후보·미확정 선택

**확인된 기존 자산:** Prometheus·Loki API, Grafana·Dozzle 링크, Ttakkari Mac `server_resources` UI/API/MCP, Prometheus target/label/query의 read-only 조회 경로.

**최소 재사용 후보(아직 I2 결정 아님):** 기존 수집기/API를 유지하고, 우선 Mac snapshot과 이미 scrape되는 Ubuntu host/exporter 신호를 데이터 출처와 확인시각을 붙여 재사용한다. Grafana/Dozzle은 상세 확인 링크로 유지한다. 신규 수집기·저장소·Docker socket 노출은 선행 조건으로 두지 않는다.

**남은 선택:** (a) Ttakkari backend가 Ubuntu의 읽기 전용 Prometheus/Loki API에 실제로 접근할 경로와 권한, (b) app-level service health 원천이 필요한 서비스 범위, (c) `winterholic`과 `ubuntu-01`을 연결하는 canonical host ID, (d) 운영 기준선을 근거로 한 severity/staleness 지속시간. 이번 Run에서는 연결 구현이나 기준 결정을 하지 않았다.

## 8. WBS 12.1.1~12.2.3 결과

| WBS / UUID | 결과 | 사유·남은 부분 |
|---|---|---|
| 12.1.1 / `36ef9e71-b6c1-4a48-ba81-05a01c863218` I1.1 | 부분 · `doing/0%` | Ubuntu systemd 목록과 Mac snapshot을 확인. Docker 목록은 API 권한 거부, canonical host ID 충돌, win-server 현황 미확인. 중요도도 없음 |
| 12.1.2 / `ab933a79-1441-4845-a0f1-5a78379a1962` I1.2 | 부분 · `doing/0%` | runtime targets/API/labels/retention 확인. runtime-저장소 설정 불일치, 전체 label/query 예제·실패/빈 응답 범위 정리 및 Grafana/Dozzle 상세 접근은 완료기준 일부 미충족 |
| 12.1.3 / `833cdc99-2888-470b-ba1e-a966b22f7cce` I1.3 | 부분 · `doing/0%` | Mac에서 알려진 SSH 별칭으로 Ubuntu 접속, loopback API를 읽음. 네트워크 경로/Tailscale 연결 여부, 다른 호스트, app의 원격 API 권한 확인 안 됨. Docker는 permission denied |
| 12.1.4 / `9d22984d-77fb-488b-8e31-033e280ca4d3` I1.4 | 완료 · `done/100%` | `/resources` UI/API/DTO, agent API, MCP와 기존 실패/갱신 표시 재사용 지점을 특정. 다만 모두 현재 Mac 리소스용임 |
| 12.2.1 / `61059382-6996-44bc-a595-a89d534f233d` I2.1 | 선행 대기 · `todo/0%` | I1.1/I1.2가 완료되지 않아 host/service canonical ID, 중요도, 서비스↔container 매핑을 확정할 수 없음 |
| 12.2.2 / `a289c1b1-889a-46dd-b451-a389d27900f2` I2.2 | 선행 대기 · `todo/0%` | I1.3/I2.1 미완료. 최소 재사용 후보는 기록했지만 원격 API 권한·경로와 데이터 계약 확인 전 구조 선택은 미확정 |
| 12.2.3 / `2d635e49-3fab-40f3-8c1b-ce4d795c80ae` I2.3 | 선행 대기 · `todo/0%` | I2.1 선행 미완료. 기존 기준은 개별 게이지/주기 보고서용이며 전역 상태 정책으로 결정되지 않음 |

I3.1/I3.2 조건부 시안, 기존 선행 UUID, 원래 설명은 변경하지 않았다.

## 9. Claude 후속 반영사항

1. §2의 5개 질문은 제안으로 유지하고, “사용자 원문”으로 인용하지 않는다.
2. 화면에 `ubuntu-01`과 Mac만 노출할 때도 `winterholic`↔`ubuntu-01`은 식별자 매핑 확인 전 단정하지 않는다. `win-server`와 서비스 중요도는 미확인이다.
3. Prometheus target `up`은 scrape 수집 상태만 나타낸다. container name series 개수도 실행/건강/중요도와 같지 않다.
4. 런타임 Prometheus/Loki 보존기간(10y/100GiB, 90d)을 확인 사실로 쓰되 저장소 설정 불일치가 있음을 드러낸다.
5. 3회/5분/30분은 예시/가정으로 유지하거나 제거한다. 기존 80/90 또는 일일 보고서 threshold도 fleet severity로 확장하지 않는다.
6. “미확인”은 장애가 아니다. “수집 실패/값 오래됨/서비스 health 실패”를 출처와 시각에 따라 분리한다.
7. 기존 재사용 후보는 Mac `/resources`와 read-only Prometheus/Loki/Grafana/Dozzle뿐이다. 원격 통합 경로는 아직 디자인 선택으로 남는다.

## 10. 확인한 출처

- 계획 원본: `/Users/winterholic/.ttakkari/data/outbox/db2b5d9d-a423-4497-a67e-8e619365ab3e/2026-10-14-plan-infra-control-wbs.csv`, `.html` — I3.1/I5.2, 목표/선행 단계
- 홈서버 구성: `/Users/winterholic/development/projects/home-server/homeserver/infra/monitoring/{docker-compose.yml,prometheus.yml,loki-config.yml,README.md,daily-report.sh}`
- Ttakkari: `front/src/{App.tsx,features/system/ResourcesPanel.tsx,api/client.ts}`, `back/src/ttakkari/{api/system.py,api/agent.py,sysinfo.py,orchestrator/mcp_server.py}`
- 시안: `docs/design/infra-control/{spec.md,prototype.html}`
- 기록 검색: `~/work-history/2026-10-14.md`, 지정된 Ttakkari 세션 `b200943e-e22c-4f1e-b63d-3dac50251447`; 5개 질문의 사용자 원문은 찾지 못함
- 저장소 상태: back HEAD `1834d24`, 기존 미추적 `docs/creative-workspace/`는 읽거나 수정하지 않음. 홈서버 로컬 HEAD `4a01374` (origin/main보다 2 commit ahead); 런타임 값은 저장소 문서 대신 API에서 따로 관측
