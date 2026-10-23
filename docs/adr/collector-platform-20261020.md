# ADR — 범용 수집 플랫폼과 ML 품질 평가 경계 (2026-10-20)

상태: S1 설계 + S2 수집 플랫폼 구현(2026-10-23 갱신, 아래 "S2 구현 결정"). S3(ML 학습·calibration)와 검색 엔진 최종 선정은 미착수. 운영 배포는 예약만 기록했고 성공은 별도 확인. 기존 숫자 ADR과 병행 작업 충돌을 피한 날짜 slug다.

## 결정

지식 도서관과 독립적인 collector bounded context를 기존 모놀리스 내부에 둔다. 기존 PostgreSQL·blob 저장 port·generation periodic/consume/work·PMS·audit/ops를 재사용한다. 결과는 버전 있는 ingestion envelope로 export 또는 knowledge adapter에 전달한다. Run 전용 lease를 collector ID로 재사용하지 않고 job 행의 ownership/token과 기존 generation 작업 lock을 조합한다. 일별/실행별 절대 토큰·호출·금액을 DB 선예약하며 강제 불가능한 teacher 엔진은 비활성이다. 공개 모델은 challenger이며 운영 채택 전 독립 gold·calibration·false-negative·비용을 검증한다.

## 대안과 대가

1. 지식 원장에 수집/품질/스케줄을 모두 결합: 단기 코드량은 작지만 범용 소비자·권리/보류 경계를 지식 상태에 묶으므로 배제.
2. 독립 서비스+Redis/Celery+새 scheduler: 자원 확장은 쉬우나 현재 독립 scale 요구 실측이 없고 generation drain·DB 정본과 이중 운영이 생겨 보류. CPU/메모리 격리 측정으로 필요가 입증되면 inference worker만 분리한다.
3. 선택안 모듈형 모놀리스: DB 정합성/운영 재사용에 유리하나 bounded batch·통합 접점 단일 소유·모델 child process 자원 제한이 필요하다. 장시간 inference가 HTTP loop를 막으면 설계 위반이다.

## 근거와 영향

기존 knowledge import_text와 병행 kb_store는 소비자 계약이며 표준 envelope의 대체물이 아니다. 원본 hash 저장소를 중복 만들기 전에 kb/artifact 소유자와 port 재사용을 확정한다. 기존 EXT-C/V를 실제 WBS 정본으로 세분화하고 K/Y/5.4.7은 연결한다. 조사 오류를 정정표에 남기고 27건 스모크를 운영 성능으로 승격하지 않는다.

[실행 설계](../design/collector-platform/architecture.md) · [평가](../design/collector-platform/evaluation.md) · [첫 slice](../design/collector-platform/first-slice.md).

## S2 구현 결정 (2026-10-23)

사용자 지시: "범용수집플랫폼 최대한 완성도 100에 가깝게 지난 맥락들 확인하면서 완성해서 배포까지 해줘". 자체 검증 결과이며 독립 검증은 아니다. 증거는 reports/collector/s2-final/verification-2026-10-23.txt.

1. **자동 수집 활성화 방식**: collector_consume_enabled는 managed_only가 아닌 일반 루프라 에이전트가 낸 임의 job까지 claim한다. 켜지 않는다. 소유자가 등록한 출처만 처리하는 collector-control(collector_control_enabled, 기본 True)이 같은 handler와 같은 정책·SSRF·robots 검사로 KB까지 적재한다. 설정 파일이나 비밀을 건드리지 않고 배포 + DB 출처 등록으로 활성화된다. 대안(기본값 변경)은 임의 job 접속 경로를 여는 위험이 있어 배제했다.
2. **DB 런타임 스위치**: collector_runtime_flags.paused. 재시작 없이 모든 세대에서 새 스케줄·새 claim을 멈춘다. 전달은 계속하되 전달 시 권리를 다시 검사한다.
3. **절대 예산(CP-S2-05)**: 호출 전에 최악 비용을 day→job 순서로 잠그고 commit한 뒤에만 호출한다. 실패·취소·receipt 없음은 환불하지 않고 unknown으로 전액 점유, 환불은 provider receipt가 있을 때 차액만. 날짜는 Asia/Seoul이며 정산은 예약 당시 날짜에 한다. 입력/출력 한도 강제·단가·usage 반환을 모두 증명하지 못한 엔진은 engine_unbounded로 거부한다. teacher는 기본 off이며 현재 수집 경로는 모델을 부르지 않는다(reservation 0 시험). 재생 결과: 요청 30/300/3000건 모두 20건만 허용(reports/collector/s2-05/budget-replay.json).
4. **캐시**: key에 tenant·scope·text hash·모델 revision·전처리·rubric·정책·질의를 모두 넣어 scope/모델/프롬프트를 넘나드는 hit이 없다. 음성/미확인 항목은 사유와 TTL이 필수.
5. **전달(CP-S2-08)**: export가 먼저다. 일시 오류(OSError/DB)는 savepoint 롤백 뒤 backoff 재시도 5회, 소진 시 failed로 드러내고 소유자가 "상태 점검"으로 다시 연다. 권리/해시/scope 실패는 종결이며 덮어쓰지 않는다. 로컬 sink는 수신과 영수증이 같은 DB 트랜잭션이라 delivery_unknown은 만들어지지 않는다(외부 sink가 생길 때 쓴다).
6. **파일럿(CP-S2-10)**: 권리를 원문으로 확인한 3곳(Kubernetes 블로그 피드·한국어 문서, GOV.UK 뉴스 Atom)만, train=unknown, 하루 약 26쪽. 첫 tick에 한 번 등록하고 DB 플래그로 재등록을 막는다. 실서버 스모크에서 Kubernetes 쪽이 모두 tech_docs_version_unknown으로 보류돼 이 두 출처만 그 규칙을 끈다.
7. **관리 화면**: 독립 /collect(현황·출처·실행·검토·품질/한도). 지식 도서관 수집 탭과 패널을 공유한다.

미해결/남김: hold 재처리 job(parser 버전이 바뀔 때 필요, 현재 버전 1개), 운영 DB identity와 인증된 운영 화면 확인, IPv6 실접속, 304 외 observation 확장, 외부 sink용 delivery_unknown 경로, S3 전부.
