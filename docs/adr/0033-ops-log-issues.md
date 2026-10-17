# ADR 0033. 운영 로그·오류를 백엔드가 수집·마스킹해 에이전트에게 MCP로 제공한다 (Sentry 개념 차용)

- 상태: 채택(2026-10-17)
- 결정자: 사용자 지시("Sentry 아이디어를 빌린 운영 로그·오류 처리 도구"). 보존·삭제 정책은 이 ADR 범위 밖이며 PMS 01be3dfb에서 따로 결정한다.

## 배경(실제 사고)
Run 안의 에이전트가 백엔드 배포 실패를 진단하려 했지만 배포 job 로그가 `~/.ttakkari` 안에 있어 안전 가드가 막았다. 에이전트는 로그 없이 추측했고 앞선 배포 실패 두 건의 원인은 끝내 알 수 없었다. 같은 날 MCP 응답이 너무 커서 보호 영역에 저장돼 읽지 못한 일도 반복됐다(PMS 전체 트리 조회 46만 자).
가드를 넓히거나 우회하지 않는다. 백엔드가 자기 홈의 로그를 읽어 마스킹하고 요약해서 주는 정식 경로를 만든다.

## 로그가 생기는 곳(전수 조사, 2026-10-17 코드 기준)
| 출처 | 위치 | 성격 | 수집 방식 |
|---|---|---|---|
| 배포 job 상태 | `<home>/deploy/jobs/<id>.json` | status·error·revision(SHA) | job 동기화. 실패·recovery_pending만 이벤트 |
| 배포 job 로그 | `<home>/deploy/jobs/<id>.log` | uv sync·pytest·migrate 출력 | job 이벤트의 발췌 + 전용 조회 도구(원본 줄 단위, 마스킹) |
| Run 실패 | DB `runs.status=failed`, `runs.error` | 엔진 오류·한도·내부 오류 | DB 동기화(커서 + 멱등 키) |
| 엔진 폴백·한도 | DB `run_events` (`engine.fallback`, `account.limited`) | 라우팅 실패(예: Antigravity 모델이 effort 옵션 거절) | DB 동기화, 경고 수준 |
| FastAPI 미처리 예외 | 프로세스 | 500 | `Exception` 핸들러가 즉시 기록(릴리스 SHA·요청 경로 태그) |
| 프로세스 로그 | `<home>/logs/*.log` (backend, deployer, tunnel, qwen, proxy, 슬롯별) | launchd stdout/stderr, Python logging, uvicorn | 파일 증분 인입(inode·오프셋 커서), ERROR/CRITICAL/Traceback 블록만 |
| 코드 내 예외 | `capture_exception()` | 임의 호출 지점 | 명시 호출용 API |

중복 방지: uvicorn의 `Exception in ASGI application` 블록은 FastAPI 핸들러가 이미 기록하므로 파일 인입에서 건너뛴다. deployer 로그의 `deployment <id> failed` 블록은 job 이벤트가 더 풍부해서 건너뛴다.

## Sentry에서 빌린 것 / 안 빌린 것
Sentry 코드를 그대로 이식하지 않고 단일 Mac Studio·개인 앱 규모의 PostgreSQL/FastAPI 구현을 유지한다. 2026-10-18에는 서버·Python SDK·Relay 원본의 고정 커밋을 대조하고 실제 결함 재현 후 보완했다. 원본 파일·근거·선택·검증 범위는 [원본 대조 기록](../work-history/2026-10-18-sentry-source-audit.md)에 남긴다.

| Sentry 개념 | 여기서의 형태 |
|---|---|
| 이벤트 정규화 스키마 | `ops_events`: source, level, message, stack, excerpt, tags, breadcrumbs, release, ref, occurred_at |
| fingerprint 이슈 그룹핑 | `fingerprint = sha1(영역 + 정규화 키)`. 단일 예외는 타입 + 앱 프레임 3개(줄 번호 제외), cause/context 체인은 원인별 fingerprint까지 포함(`exc-chain-v1`), 메시지만 있으면 정규화 메시지. 메시지 정규화는 UUID·SHA·숫자·시각·홈/임시 경로·주소를 자리표시자로 바꾼다 |
| first_seen / last_seen / count | `ops_issues`에 그대로. count는 저장하지 않은 이벤트도 센다 |
| 회귀 감지 | resolved 이슈에 resolved_at 이후 발생한 이벤트가 오면 unresolved로 되돌리고 `regressed_count`·`last_regressed_at` 기록. 늦게 인입된 과거 로그는 회귀로 보지 않는다 |
| release / deploy SHA 태깅 | 이벤트·이슈에 `release`(전체 40자 SHA). 배포 이벤트는 배포 대상 SHA, 실행 중 프로세스 이벤트는 자기 revision |
| breadcrumbs | Run 실패는 직전 run_events 12개의 요약, 배포는 요청·단계 타임라인. 항목당 길이 상한 |
| 태그·검색 | tags JSONB(job_id, stage, engine, model, path...). 기간·키워드·레벨·출처·릴리스 필터 |
| 데이터 스크러버 | 저장 전·응답 직전 두 번 마스킹(아래) |
| 레이트 리밋/샘플링 | 이슈당 시간당 일반 상세 샘플 상한(30). 넘으면 count와 시간별 통계만 올리되, 해결 후 재발한 첫 이벤트는 진단 근거로 별도 보존한다. 처리 원본 ID 영수증은 상세 샘플링과 독립적으로 저장하여 재수집을 집계하지 않는다 |
| 시간별 통계 | `ops_issue_stats(issue_id, bucket_hour, count)`. 샘플링과 무관하게 정확한 추이 |
| 안 빌림 | 멀티 프로젝트·조직, SDK, 소스맵, 알림 규칙 엔진, Kafka/ClickHouse, 성능 트레이싱, 세션 리플레이 |

## 비밀값 마스킹 (불변 조건)
`oplog/scrub.py` 한 곳에서 처리한다. 이벤트의 모든 문자열(메시지·스택·발췌·태그·breadcrumb)을 저장 전에 마스킹하고, API/MCP 응답을 만들 때 한 번 더 마스킹한다(방어 중첩).
- 값 일치: 설정 객체와 환경변수 중 이름에 secret·token·password·key·cookie 등이 들어간 값(8자 이상)은 위치와 무관하게 `[REDACTED]`.
- 헤더: Authorization·Proxy-Authorization·Cookie·Set-Cookie·X-Api-Key 줄은 값 전체.
- 스킴: `Bearer|Basic|Token <값>`, URL의 `user:pass@`.
- 키-값: 이름에 password·passwd·pwd·secret·token·api key·access key·private key·credential·cookie·session id·jwt·signature·auth 가 들어간 `k=v`, `k: v`, JSON `"k": "v"`, CLI `--token v`. `tokens`, `token_count`, `author` 같은 오탐 후보는 제외한다.
- 알려진 형식: sk-…, sk-ant-…, ghp_/github_pat_, xox*-, AKIA…, AIza…, JWT, npm_, glpat-, PEM 블록.
- 일부러 가리지 않는 것: 40자 hex(커밋 SHA는 진단의 핵심). 따라서 키 이름 없이 40자 hex 형태로 박힌 비밀은 못 막는다. 알려진 한계.

## 응답 크기 상한 (불변 조건)
- 모든 조회는 요약 우선, 페이지네이션(`cursor`/`next_cursor`), 항목 수 상한, 문자열 길이 상한을 둔다.
- 목록은 상한 적용 후 실제 반환한 행 수로 `next_cursor`를 다시 계산한다. 제한 때문에 빠진 행을 건너뛰지 않는다.
- 응답 전체 문자 상한: 기본 6,000, 호출자가 `max_chars`로 올려도 16,000을 넘기지 못한다. 넘으면 문자열을 줄이고 마지막 항목부터 덜어 내며 `truncated: true`와 `hint`(다음 호출 방법)를 붙인다.
- MCP 서버가 같은 상한으로 한 번 더 막는다(백엔드 응답이 어떻든 도구 결과는 16,000자 이하).

## MCP 도구 (모든 Run, 개인 세션 한정)
읽기: `ops_list_issues`, `ops_get_issue`, `ops_search_logs`, `ops_list_deploys`, `ops_deploy_log`. 쓰기: `ops_update_issue`(상태 변경·메모).
회사 세션은 개인 서버 운영 로그를 보지 못한다(`request_backend_deploy`와 같은 규칙).
`ops_deploy_log`는 job id·SHA 접두사·`latest`·`latest_failed`로 지정하고 `summary`(원인 요약·오류 구간)·`errors`·`tail`·`range` 모드를 가진다. 경로는 입력으로 받지 않는다(job id→파일 경로를 서버가 계산).
조회 직전에 20초 이상 지났으면 증분 인입을 한 번 돌려 최신 상태를 보장한다. 주기 작업도 60초마다 돌린다.

## 저장 구조와 나중의 보존 정책
이슈 단위로 `ops_events`가 CASCADE로 묶이고 모든 행에 시각이 있다. 처리 원본 ID 영수증(`ops_event_receipts`)은 원문 없이 ID·처리 시각만 가지며 상세 보존과 별도로 삭제 정책을 정해야 한다. 이슈·이벤트·통계·영수증의 시각 인덱스로 "N일 지난 이벤트 삭제", "해결 후 N일 지난 이슈 삭제"를 쿼리 한 줄로 붙일 수 있다. 이번에는 삭제를 구현하지 않는다.

## 알려진 한계
- 파일 인입의 타임스탬프 없는 줄(uvicorn)은 인입 시각(백필은 파일 수정 시각)으로 추정하고 `time_estimated` 태그를 단다.
- 과거 배포 실패의 job error는 `release command failed; see <id>.log` 뿐이라 단계 판별을 로그 내용 휴리스틱으로 한다.
- 스키마 계약: 0040은 영수증 테이블만 추가하고 기존 저장 이벤트 ID를 채운다. 0039에서 세대별 online 전환을 허용하며 기존 테이블·프로토콜을 바꾸지 않는다.
- DB 인입은 14일 백필 범위에서 미처리 원본 ID를 선택한다. 시각/sequence watermark를 처리 완료의 증거로 사용하지 않아 같은 시각의 배치 경계와 늦은 commit을 빠뜨리지 않는다. 소스별 savepoint로 다른 소스의 수집 성공은 보존한다.
- 기존 버전이 상세를 샘플링으로 생략한 원본에는 ID 기록이 없다. 그 원본의 과거 처리 여부와 기존 집계 오차는 복원할 수 없으며 이번에 과거 집계치를 임의 보정하지 않는다. 첫 재수집 시 해당 원본은 한 번 집계될 수 있고 이후에는 영수증으로 중복을 막는다.
- 단일 예외의 fingerprint는 유지한다. 체인 예외만 새 키를 사용하므로 기존에 합쳐진 이슈를 사후 재분할하지 않으며, 새 발생은 원인별 새 이슈로 갈릴 수 있다.
