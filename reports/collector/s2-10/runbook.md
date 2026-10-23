# 수집 플랫폼 운영 runbook (S2, teacher off 제한 운영)

2026-10-23. 대상: 소유자 한 명이 운영하는 Winterholic Ttakkari. 이 문서는 "지금 무엇이 켜져 있고, 어떻게 멈추고, 무엇을 봐야 하는가"만 적는다. 설계 배경은 [architecture](../../../docs/design/collector-platform/architecture.md), 결정 근거는 [ADR](../../../docs/adr/collector-platform-20261020.md).

## 1. 무엇이 켜져 있나

| 구성 | 상태 | 바꾸는 법 |
|---|---|---|
| 관리형 수집 루프(`collector-control` 주기 작업: 일정 확인, job 1건 처리, 전달, 예산 고아 정리) | 기본 켜짐(`collector_control_enabled=True`). 등록된 출처만 처리 | 설정 `TTAKKARI_COLLECTOR_CONTROL_ENABLED=false`(재시작 필요) |
| 런타임 일시정지 | 기본 해제. DB 플래그 `collector_runtime_flags.paused` | **/collect 현황의 "자동 수집 일시정지"** 또는 `PUT /api/assistant/collect/runtime {"paused":true}`. 다음 tick(최소 10초)부터 모든 세대에 적용, 재시작 불필요 |
| 일반 consume 루프(`collector_consume_enabled`) | **꺼짐 유지** | 켜지 않는다. 이유는 아래 |
| 모델 호출(teacher/judge/synthetic) | **꺼짐**(`collector_teacher_enabled=False`). 규칙(L0)만 사용 | 한도를 강제하고 usage를 돌려주는 엔진이 연결되기 전에는 켜도 `engine_unbounded`로 거부됨 |
| 파일럿 출처 | 첫 tick에 한 번 자동 등록(`collector_pilot_enabled=True`), 이후 DB 플래그가 재등록을 막음 | /collect 현황의 "예시 출처 추가"(없는 것만 추가) |

### `collector_consume_enabled`를 켜지 않은 이유(위험 분석)
- 이 플래그가 켜진 루프는 **managed_only가 아니어서** `POST /api/agent/collect/jobs`로 에이전트가 낸 임의 job까지 claim한다. 소유자가 등록하지 않은 출처로 외부 접속이 열리는 경로다.
- 소유자가 등록한 출처만 도는 `collector-control` 루프가 같은 stage handler(`pipeline.handle_job`)와 같은 정책·SSRF·robots 검사를 쓰므로 KB 적재에는 이 플래그가 필요 없다. 설정 파일(.env)을 읽거나 고칠 필요도 없었다.
- 따라서 "운영에서 자동 수집을 켠다"는 설정 편집이 아니라 (1) 배포로 루프가 존재하고 (2) 소유자 출처가 DB에 등록되는 것으로 달성했다. 일시정지는 DB 스위치가 맡는다.

## 2. 파일럿 출처 3곳 (권리를 2026-10-23에 원문으로 확인)

| key | 주기 | 한도/회 | 적재 | 근거 |
|---|---|---|---|---|
| `pilot:kubernetes-blog-feed` | 매일 | 10쪽 | 자동 | kubernetes/website 저장소 LICENSE = CC BY 4.0. robots.txt는 /legacy/ /v1.0/ /v1.1/ /404만 막음 |
| `pilot:kubernetes-docs-ko` | 매주 | 6쪽(고정 URL) | 자동 | 같은 라이선스. 한국어 문서 |
| `pilot:govuk-news-atom` | 매일 | 10쪽 | **소유자 검토 후** | GOV.UK 이용 약관: Crown copyright/OGL. 쪽별 제3자 자료 예외를 자동 판별하지 못해 자동 적재 끔 |

- 세 출처 모두 `train`은 `unknown`(허용 안 함). 모델 학습에 쓰이는 경로가 없다.
- 하루 전체 상한은 `collector_daily_page_cap`(500쪽)이고 파일럿 합계는 하루 최대 약 26쪽이다. 각 host 간격은 2초 이상이고 robots.txt를 따른다.
- Kubernetes 두 출처는 `tech_docs_version_unknown` 보류 규칙을 끈다(`require_version=false`). 이 쪽들은 URL에 버전이 없는 "latest" 스냅샷이라 켜 두면 전부 보류됐다(실서버 스모크 관찰). 원문 해시와 수집 시각이 revision을 고정한다.
- 이 권리 판단은 운영자의 원문 읽기이지 법률 자문이 아니다. 의심되면 /collect → 출처 → "출처 설정"에서 권한을 거두면 이미 적재한 문서도 숨겨진다(원장은 불변, 접근만 철회).

## 3. 매일/문제 시 보는 곳 (/collect)
1. **현황**: 발견→가져옴→추출→통과→보류/차단/실패→적재, "내 검토 대기", 기다리는 작업(재시도 대기·예산 대기·정책 차단), 하루 한도, 자동 수집 상태.
2. **출처**: 일시정지/재개, 지금 수집, 권한 설정. 한 출처만 멈출 수 있다.
3. **실행**: job 이력, 취소/재실행, 전달 결과(실패 항목은 "상태 점검").
4. **검토**: 보류 + 자동 적재가 꺼진 출처의 통과 자료. 원문·처리 이력 확인, 유용성 검토, 적재. "보류 표본 감사"로 출처·사유별 고정 표본을 본다.
5. **품질·한도**: 하루 호출/토큰/비용/페이지, 정산 대기, 참고 품질 모델(shadow).

| 증상 | 의미 | 조치 |
|---|---|---|
| job `retry_wait` + `robots_unreachable`/`fetch_deferred` | host의 robots/응답 문제나 SSRF 거부. 접속 안 함 | 기다리면 backoff 후 재시도(최대 3회). 계속되면 출처 확인 |
| job `policy_blocked` | 정책 없음/store 불허 | 출처 설정에서 권리 확인 |
| 전달 `failed` + `transient:*` | blob 저장소 등 일시 오류 5회 소진 | 원인 해소 후 "상태 점검"(재개) |
| 전달 `failed` + 권리/해시/scope | 종결 사유. 덮어쓰지 않음 | 출처를 고친 뒤 다시 수집 |
| 한도 `정산 대기` > 0 | 호출 결과를 모르는 예약. 한도에서 빠진 채 유지 | 현재 모델 호출이 꺼져 있어 0이어야 정상 |

## 4. 멈추기/되돌리기
1. 즉시 정지: /collect 현황 → "자동 수집 일시정지"(DB 플래그, 재시작 불필요). 이미 가져온 자료의 전달은 계속된다(전달 때 권리를 다시 검사).
2. 한 출처만: 출처 → 일시정지.
3. 루프 자체 제거: `TTAKKARI_COLLECTOR_CONTROL_ENABLED=false` 후 재시작/배포.
4. 적재 문서 숨기기: 출처 권한을 `knowledge: denied`로 바꾸면 해당 출처 문서가 검색·원문 export에서 즉시 빠진다.
5. 스키마는 additive라 downgrade 대신 앞으로 고친다(roll-forward). 새 테이블(`collector_budget_*`, `collector_cache`, `collector_runtime_flags`)은 비어 있어도 무해하다.

## 5. 배포 후 확인 순서 (수락 ≠ 성공)
1. `backend_deploy_status(예약 ID)`가 `succeeded`인지. `requested/waiting_*/draining`은 성공이 아니다.
2. 운영 `ops_search_logs`에서 `collector` 관련 로그(첫 tick의 파일럿 등록, job 실행, 오류)를 본다.
3. 로그인한 상태에서 /collect 현황이 "켜짐"이고 출처 3곳·최근 job이 보이는지, 지식 도서관 검색에 문서가 생겼는지 사람이 확인한다. **이 Run은 운영 인증이 없어 3번을 하지 못했다.**

## 6. S3와의 경계 (깨지 않을 인터페이스)
- `quality.L1Policy`/`run_l1`(shadow 모드)과 `quality_service` 참고 모델은 그대로다. S3의 공개 classifier·student는 이 경로에 `mode=shadow`로 붙는다.
- 모델 호출은 반드시 `budget.guarded_call(...)`만 거친다. 엔진은 `EngineBound`로 입력/출력 한도 강제·단가·usage 반환을 모두 증명해야 한다. 하나라도 없으면 거부된다.
- 결과 캐시는 `cache.cache_key`(tenant·scope·text hash·모델 revision·전처리·rubric·정책·질의)를 쓴다.
- S3 운영 승격 조건: (1) 라벨 rubric·누출 방지 split·권리 manifest(CP-S3-01), (2) 학습 허용(`train`)이 확인된 출처 5개 이상과 사람 라벨 20건 이상, (3) 한도 강제 엔진 증명 전에는 teacher 비활성, (4) 운영 실제 적재 문서에 대한 독립 검토. 코드의 로컬 utility baseline/calibration은 shadow에만 붙으며 이 조건을 대신하지 않는다. 현재 파일럿은 `train=unknown`이라 학습 자료로 쓸 수 없다.
- source-heldout calibration이 있는 artifact만 `calibrated_probability`를 반환한다. 지원 표본/독립 출처가 부족하면 `calibration=null`이고 소비자는 확률 대신 raw logit과 미측정 상태를 사용해야 한다. Manifest에는 label review ID/actor/time, source policy hash/version, license/policy URL, train permission과 본문 해시가 포함된다. 이는 학습 출처를 재현하는 코드 근거이며 사람 검토나 권리의 실체를 독립 증명하지 않는다.
- hold 재처리는 기존 보존 원문만 읽고 네트워크를 호출하지 않는다. 요청 시점과 실행 시점의 보존 만료·현재 store/store_raw 권리를 모두 확인하고, 동일 item/source/policy/profile/model 버전 요청은 멱등하다. 보존 원문이 없거나 권리가 철회된 경우 작업을 만들지 않는다.
