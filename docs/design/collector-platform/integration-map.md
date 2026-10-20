# 기존 WBS와 신규 CP 패키지 연결

PMS 프로젝트 `e99a75de-7ccf-48e6-a5d1-46e455eb79b8` (winterholic-ttakkari) 안에서만 상세화했다. 기존 task를 삭제·이동하거나 동일 실행 항목을 다른 프로젝트에 복제하지 않았다. CP는 원래 항목의 하위 실행 패키지다. 수집 플랫폼의 3단계는 단계 라벨(S1/S2/S3)로 여러 기존 책임 트랙을 묶으며 PMS의 기존 번호는 유지한다. 정확한 현재 번호·상태는 evidence/pms-readback.json.

| 기존 항목/UUID | 책임·CP 연결 |
|---|---|
| EXT-C `a675d332-fb13-4bd2-84a1-a763c5f2b68b` | platform 공통 실행 기반 정본 |
| C01 `502bc99f-7fef-457a-bccb-8a99b24a5101` | S1-01 조사 정정과 S2-00 실제 runtime/소유권 인벤토리 분리 |
| C02 `986b7380-48f3-43f7-a6ef-cacdbdfab743` | S2-03 권리/robots 정책. 브라우저/개별 서비스 권리 전부 완료 X |
| C03 `733aa857-1819-4a88-b258-1ef31e72001d` | S1-03 설계, S2-01 계약/멱등 등록, S2-02 durable 실행 |
| C04 `48e0368e-b268-4ec8-9b27-29dc651f3e45` | 로그인 브라우저 전용 범위 유지. 첫 slice에는 브라우저 없음 |
| C05 `cc41a6f0-c3a8-4613-8cbf-28f235a305c8` | S2-05 공용 절대 예산·캐시. 다른 미디어 자원 예산 원 범위 유지 |
| C06 `2c27e24d-ca2f-4984-9609-9a2d3c5d42f1` | S2-06 비신뢰 정규화/lineage/보류 보존 |
| C07 `b7dbc82e-ce74-4e18-9e75-c418f51ea1a7` | 기존 EXT 전체 설계/권한 게이트. CP leaf 등록은 사용자 승인 event가 아님 |
| C08 `6cd5d8c7-fbb6-434a-8b62-1c74397c6f6b` | S2-04 fetch, S2-07 독립 관리 API/UI, S2-08 소비 adapter |
| C09 `98e579f1-9d7f-46cc-b639-159b41549489` | S2-09 통합 negative/recovery/budget 검증 |
| C10 `de783003-aa73-49cb-a128-2161140a97ce` | S2-10 teacher off 파일럿/runbook |
| EXT-V `87f29d1e-8eb6-45b5-af82-efc12d450751` | generic ML/평가 실행 정본. V01 라벨/split, V02 모델·ablation, V03 calibration, V04 baseline/student, V06 bounded teacher, V08 holdout, V09 retrieval, V10 drift |
| K02 `cbdbc732-4f8c-4fee-9858-3b104e6af4e2` / K06 `faa476f8-8304-47e4-8bb1-8d82eb97e1cf` | 지식 저장/정본·revision 책임. collector가 모델을 복제하지 않고 S2-08 sink 계약으로 연결 |
| K04 `3c084b5c-9f76-4d5a-8ed8-180a0cfd26cf` / K09 `30c85e77-55ab-4c37-a90d-b568da931024` | S3-10 control/challenger 검색 평가 연계. 기존 recall 순위/종류 무단 변경 X |
| K07 `1e4090f0-6f03-4832-aaf7-cb7fc653db62` | 지식 관리 UI 소유. 수집 `/collect`는 별도 관리 화면 |
| Y06 `5281cea0-d027-4ce3-83bd-32f63a43b149` / Y07 `6fbcdff4-56e9-4e97-a652-333a0012a3f6` | 영상 adapter/자막·발견 전용 책임. collector 공통 상태/예산 소비. NotebookLM 자막 지원·시각 누락 미검증을 구현 완료로 쓰지 않음 |
| Y08 `34d438d7-d4f7-41b3-ab77-976e0664c81f` | 동일 envelope/status로 영상 입력 회귀를 이후 확장. HTTP first slice의 필수 범위 아님 |
| 5.4.7 `78e4e51f-fda3-44af-8977-8b344359e345` | 지식 원장 사후감사 후속. collector 자체와 다른 게이트 |
| 5.4.7.3.6 `87f921eb-7797-43cc-95e7-45139a4dc2f2` | S3-10 실제 predecessor. 첫 의미 추출에만 적용, 독립 collector/export 구현을 막지 않음 |

15개 관련 부모 description에 기존 내용 보존 후 CP 상세화 메모를 덧붙였다. 원래 광범위 EXT 승인 문구를 새 범용 collector 개발 재승인 질문으로 확대하지 않는다. 계획기에서 실행 순서를 읽을 때 CP leaf의 UUID 선행 그래프를 사용한다. 기존 부모의 predecessor는 과거 전체 서비스 gate 범위여서 새 leaf에 암묵적으로 상속하는 자동 실행기로 사용하지 않는다. PMS는 현재 수집 실행 scheduler가 아니다. 부모 상태는 집계값이며 범위 전체 운영 완료 판정에 쓰지 않는다.

마감 KB 정합화: ADR 0063 및 기존 Collector Protocol은 [계약 호환표](contract-compatibility.md)를 따른다. 현재 미커밋이라는 초기 관찰은 더 이상 최종 상태가 아니다. legacy knowledge 의미 추출 gate와 kb-only 원본/패시지 수입의 선행조건을 구분한다.
