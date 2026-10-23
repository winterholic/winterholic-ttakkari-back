# 범용 수집 플랫폼 + ML 품질 평가 실행 설계 납품

2026-10-20 설계, 2026-10-23 S2 구현·검증 갱신. S3(ML 학습·calibration)는 미착수. 배포는 예약 기록과 별도 확인. 실행 기록은 [작업 기록](../../work-history/2026-10-23-collector-s2-completion.md), [runbook](../../../reports/collector/s2-10/runbook.md).

- [요약 HTML](summary.html): 휴대폰용 핵심 결정·3단계·PMS UUID.
- [아키텍처](architecture.md) / [ADR](../../adr/collector-platform-20261020.md): 모듈 경계·저장·상태·generation·예산·safe fetch·별도 API/UI.
- [기존 KB 계약 호환표](contract-compatibility.md): 마감 동시 커밋과 discover/fetch/parse 확장.
- [평가/학습](evaluation.md) / [정정 추적](corrections.md): 라벨·split·teacher/student·calibration·독립 평가·과거 오류 범위.
- [Intake scope 결속·멱등 계약](intake-binding.md): envelope access 비신뢰, JCS payload hash, intake fixture 10건.
- [첫 구현 패키지](first-slice.md): CP-S2-01 `48d0a2b4-09f5-47ab-a6ac-d86a92c249a5`.
- [WBS CSV](wbs.csv) / [기존 트랙 매핑](integration-map.md) / [실제 PMS readback](evidence/pms-readback.json).
- [표준 schema](contracts/ingestion-envelope.schema.json) / [예산 schema](contracts/budget-policy.schema.json) / fixtures.
- [실행 증거](evidence/verification.txt): 입력 SHA/시각·현재 로컬 revision/dirty·과거 원출력·브라우저 범위. 전체 제품 동작을 보장하는 검증이 아니다.

PMS `e99a75de-7ccf-48e6-a5d1-46e455eb79b8`의 기존 EXT-C/V 안에 CP leaf 25건: S1 설계/정정 3 done, S2 구현 11 todo, S3 ML/검증/개선 11 todo. 역할은 책임 경계이며 개인 실행자/일정은 미배정이다. 기존 EXT-C/K/V/Y 상위 전체 완료나 사용자의 별도 승인 event로 승격하지 않는다.

다음 worker는 먼저 최신 back 변경 소유권·migration graph를 확인하고, 계약 validator/멱등 intake의 RED 테스트부터 시작한다. front 발산은 UI 착수 전, 병행 kb ACL/visibility/parser와 P0 gate는 knowledge sink 연결 전 해결한다. bounded teacher port가 없으면 teacher off로 수집 자체를 진행한다.

재현(back 루트): `.venv/bin/python docs/design/collector-platform/contracts/verify_contracts.py` 및 `python3 docs/design/collector-platform/check_delivery.py --closed`.
