# 이전 분석의 정정 반영 추적표

2026-10-20 · 원본을 덮어쓰지 않는다. 검증 둘 다 PASSED_WITH_CORRECTIONS이며 원본만 단독 인용하지 않는다.

## 입력 정본

1. 현황 원본 `/Users/winterholic/.ttakkari/data/outbox/9bc2a53b-7baf-4272-bd9c-4a9714732616/01-system-analysis.md`
2. 현황 검증 `/Users/winterholic/.ttakkari/data/outbox/77e354b1-ba18-4d91-8f68-1bfcc12dde68/01-system-analysis-verification.md`
3. 모델 원본 `/Users/winterholic/development/scratch/collector-research/02-public-models-datasets-report.md`
4. 모델 검증 `/Users/winterholic/.ttakkari/data/outbox/afbe02f3-1c2e-4e68-a5f0-29373da8286c/02-report-verification.md`
5. 모델 재실행 원출력 `/tmp/collector-verify-run02.log`, `/tmp/collector-verify-run03.log`, `/tmp/collector-verify-memory.log`. 원본 hash/시각은 evidence/input-manifest.json.

| ID | 과거 주장/갭 | 채택한 정정과 적용 위치 | 상태·증거 범위 |
|---|---|---|---|
| A01 | SciPy 없음 | 현재 back venv SciPy 있음. 독립 ML stack 설치 여부와 분리 | 반영. verification 보고의 환경값이며 이번 재검사 값은 repository receipt |
| A02 | 주기 실행기 부재처럼 읽힘 | generation_service.periodic/consume/maintenance_jobs 재사용 | 반영. architecture §4, 현재 코드 열람 |
| A03 | Stock-gosu 수집 자산 누락 | collector scheduler/base/backfill, movers/providers/edgar_feed 비교 | 반영. architecture §5. 금융 결합/Redis/SEC 파싱 그대로 이식 X |
| A04 | assertion 0·Recall .417 운영 일반화 | 10/16 스냅샷과 10/15 고정 24질의 감사 | 반영. evaluation. 현재 운영값 미측정 |
| A05 | 2.6.2 게이트 | 5.4.7.3.6 실제 UUID로 adapter 의미 추출 의존 | 반영. WBS/PMS readback |
| A06 | PMS 371/458 | 조회 경로별 과거 차이, 원인 미확정 | 보존. 현재는 해당 leaf readback만 사용하며 총 프로젝트 진척 추정 X |
| A07 | back/front 변경 주의 | back 동시 dirty, front 발산. source 존재와 배포 분리 | 반영. evidence/repository.txt, 첫 slice 소유권 |
| M01 | 한영 good/bad 분리 성공 | 27 편의표본 expected는 독립 gold 아님. 중앙값/AUC 방향+범위 중첩 | 반영. evaluation의 candidate smoke. 운영 성능 결론 X |
| M02 | PlusPlus 채택·한국어 확정 | 후보 유지, 101언어 카드만으로 ko 성능 입증 X | 반영. model registry/split 계획 |
| M03 | mfwedu sigmoid를 품질확률로 사용 | raw logit 척도 보존. calibrator/gold 없으면 확률 없음 | 반영. schema 및 negative fixture |
| M04 | mdclm softmax 지침 | prose softmax vs 1-logit sigmoid 코드 모순 | 반영. raw 보존, 무보정 수치를 확률로 단정 X |
| M05 | 고정 속도 81.7/136.9/.2ms | 재실측 162.15/237.87/.23ms와 병기, 부하/환경 의존 | 반영. 과거 CPU benchmark이며 이번 재실행 아님 |
| M06 | memory 약1.2GB vs500MB | 실행 중 관찰값과 standalone peak RSS 458/502/2918MB 정의 분리 | 반영. 기법 채택·host GPU 보장 수치 X |
| M07 | 한국어 자산 부재 | 직접 적합한 웹문서 품질/웹스팸 gold 이번 조사 미확인 | 반영. 인접 KOREAN-WEBTEXT/KHJ/AI Hub 후보 비교 |
| M08 | NVIDIA 영어 전용/배제 | 언어 선언 미확인, 영어 중심 추론과 미실측·High recall .1776 구분 | 반영. 보조 후보 유지 |
| M09 | FineWeb2-HQ 한국어 암시 | 카드의 20언어 한국어 목록 없음. PlusPlus와 corpus 혼동 X | 반영. source license inventory 후 사용 |
| M10 | 라이선스 전체 허용 | dataset/subset/모델/원문 용도별 권리 분리 | 반영. source policy와 training manifest gate |
| D01 | 수집 원장으로 지식 원장 직접 사용 | 사용자의 독립 플랫폼 결정에 따라 envelope→adapter로만 연결 | 반영. architecture §2/3 |
| D02 | 초기 kb 병행 변경 | 당시 미추적 kb source와 ADR 참조 충돌 관찰 | 시간 한정 정적 관찰. D03으로 후속 상태 보완 |
| D03 | 마감 중 동시 커밋 | KB 커밋 63cf7e02, ADR 0063 정정·0055 강화·Collector Protocol 확인 | 반영. contract-compatibility.md; 운영 배포는 미검증 |
| R01 | envelope access를 그대로 수용(임의 tenant/principal 변조를 checker가 수용) | 인증 주체 산출값과 불일치하면 403. intake fixture 음성 5건(변조 4, cross-scope 재전송 1) | 반영. intake-binding.md §1, verify_contracts.py. 런타임 미실행 |
| R02 | 멱등 payload 동등성 미정의 | RFC 8785 JCS, `/recorded_at`만 제외, envelope_id 포함. key 순서·recorded_at 재전송 replay, 의미 필드 409 | 반영. intake-binding.md §2, JCS 벡터 11건. 런타임 라이브러리 선택 확인 필요 |
| R03 | CP-S2-00 gate 불명확, WBS의 S2-01 상태가 live와 다름 | intake/migration은 S2-00 이후. wbs.csv·readback을 live doing 10%로 갱신 | 반영. PMS description·메모, wbs.csv |

위 표의 '반영'은 설계에 정정이 들어갔다는 뜻이다. 제품 구현·운영 성능·P0 통과를 뜻하지 않는다. PMS의 현황/모델 조사 기록도 이 범위만 완료로 적고 기존 넓은 조사·통합은 남긴다.
