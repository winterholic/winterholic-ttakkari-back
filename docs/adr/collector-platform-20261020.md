# ADR — 범용 수집 플랫폼과 ML 품질 평가 경계 (2026-10-20)

상태: 실행 설계 제안·schema 검증; 구현/학습/운영/배포 미실행. 기존 숫자 ADR과 병행 작업 충돌을 피한 날짜 slug다.

## 결정

지식 도서관과 독립적인 collector bounded context를 기존 모놀리스 내부에 둔다. 기존 PostgreSQL·blob 저장 port·generation periodic/consume/work·PMS·audit/ops를 재사용한다. 결과는 버전 있는 ingestion envelope로 export 또는 knowledge adapter에 전달한다. Run 전용 lease를 collector ID로 재사용하지 않고 job 행의 ownership/token과 기존 generation 작업 lock을 조합한다. 일별/실행별 절대 토큰·호출·금액을 DB 선예약하며 강제 불가능한 teacher 엔진은 비활성이다. 공개 모델은 challenger이며 운영 채택 전 독립 gold·calibration·false-negative·비용을 검증한다.

## 대안과 대가

1. 지식 원장에 수집/품질/스케줄을 모두 결합: 단기 코드량은 작지만 범용 소비자·권리/보류 경계를 지식 상태에 묶으므로 배제.
2. 독립 서비스+Redis/Celery+새 scheduler: 자원 확장은 쉬우나 현재 독립 scale 요구 실측이 없고 generation drain·DB 정본과 이중 운영이 생겨 보류. CPU/메모리 격리 측정으로 필요가 입증되면 inference worker만 분리한다.
3. 선택안 모듈형 모놀리스: DB 정합성/운영 재사용에 유리하나 bounded batch·통합 접점 단일 소유·모델 child process 자원 제한이 필요하다. 장시간 inference가 HTTP loop를 막으면 설계 위반이다.

## 근거와 영향

기존 knowledge import_text와 병행 kb_store는 소비자 계약이며 표준 envelope의 대체물이 아니다. 원본 hash 저장소를 중복 만들기 전에 kb/artifact 소유자와 port 재사용을 확정한다. 기존 EXT-C/V를 실제 WBS 정본으로 세분화하고 K/Y/5.4.7은 연결한다. 조사 오류를 정정표에 남기고 27건 스모크를 운영 성능으로 승격하지 않는다.

[실행 설계](../design/collector-platform/architecture.md) · [평가](../design/collector-platform/evaluation.md) · [첫 slice](../design/collector-platform/first-slice.md).
