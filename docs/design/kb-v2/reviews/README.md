# v2 독립 검토 근거와 설계 반영

현재: **KB-E0 로직 완료 승인, 제품검색 미승인**([E0 완료 기록](kb-e0-completion.md)). 이전 상태는 **KB-E0 착수 조건부 승인, 제품검색 미승인**였다. [반영 독립 확인 원문](amendment-independent-review.md)을 바이트 보존한다. E0 구현 추적은 [작업 기록](../../../work-history/2026-10-22-kb-e0-harness.md). 아래 검증 로그는 설계 수정안 당시 기록이다.

- 검토 대상: `8e3dc2c11f25098bc2fd1fee3886b6c235b5651f`, Run `78470bf8-41e6-4721-aa1c-20a853841cb3`.
- [원 보고서](v2-design-review.md): 바이트 보존, 판정 approve-with-changes. 검토자의 측정·미실행 범위를 그대로 읽는다.
- [전체 대응표](v2-review-response.md) → [활성 설계 v2](../knowledge-library-design-v2.md) §12 P1–13.
- [출처 manifest](source-manifest.json): `/tmp` 사본의 원 경로·파일명·mtime·sha256·size. 스크립트는 `.py.txt`(실행 시 별도 임시 디렉터리에 원 이름으로 복원), 내용은 무편집. raw output은 원 이름. 데이터/모델 바이너리 없음.
- [collector 소유 변경 요청](collector-contract-followups.md).
- [현재 문서 검증 기록](amendment-validation.txt), `check_design_delivery.py`는 문서/작은 계산·모델 확인 전용이며 제품 평가 하네스가 아니다.

별도 clone에 접근해 보존했으며 가드가 막힌 원 outbox 접근은 시도하지 않았다. 원 report가 적은 리뷰일 외에 정확 측정 시각은 미제공이다. manifest mtime은 사본 파일 시각이며 측정 시각을 증명하지 않는다.

Main 진입 gate는 통과하지 못했다. [초기 uv 캐시 오류](pre-push-initial.txt)와 [허용 캐시 재실행/PG initdb 실패](pre-push-contract-startup.txt)를 별도로 보존한다. 원인은 inner stderr 미출력으로 미확인이다. 독립 검토용 문서 branch 게시와 main 계약 통과를 구분한다.
