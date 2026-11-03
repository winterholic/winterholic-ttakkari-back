# KB v3 최신 통합 접근·스키마 교정 (2026-10-27)

기준은 `back-kb-v3-deploy-regression-20261027`의 `515982f3c120f166a370bd326504b2790260c093`입니다. 고정 검토 트리 `c07ea7cd0440363abc6cacb909b3f0cd338ef43b`는 보존했습니다. 80d3f58c 독립 검토의 원문과 제품 소스를 대조한 후 이 Run에서 직접 교정했습니다.

## 교정

- scope 병합 작업이 0075만 허용해 현재 0076에서 차단됐습니다. 실행 중인 릴리스의 검증된 generation contract head와 DB의 단일 head가 정확히 같아야 실행하도록 바꿨습니다. 구 스키마 거부, 활성 세대·이전 세대 drain·cutover 행 잠금·롤백 보호를 유지합니다.
- 회사 엔진 접근을 해제해도 `access`와 `pool`을 함께 전달한 경로가 pool을 무시했습니다. 신뢰된 접근 컨텍스트에 회사 엔진 조건을 추가하고 문서·패시지·엔터티·별칭·주장·관계의 원문 조인에 공통 적용했습니다. 실제 API에서 grant→revoke 뒤 목록·어휘/하이브리드 검색·문서·근거가 차단됩니다.
- 회사 엔진 grant가 원본 출처와 visibility까지 work/work_only로 바꾸던 부작용을 제거했습니다. 허용 설정은 pool만 바꾸며 기존 출처와 visibility는 보존합니다. 접근 행 없는 미상 자료는 새 접근 행을 만들 때도 work/work_only로 격리합니다.
- 소유자가 기존 scope=work로 접근하던 자료를 기본 통합 화면에서도 발견할 수 있도록 owner 컨텍스트를 연결했습니다. 개인 에이전트는 global만 조회하며 회사 자료를 새로 공개하지 않습니다. 실제 개인 세션 토큰의 문서·근거 404 및 검색 비노출과 개인 컨텍스트의 그래프·별칭·관계 비노출을 유지했습니다.
- Collector delivery의 단일 저장 scope는 personal이고 회사 입력의 pool/origin_scope/visibility는 work/work/work_only입니다. 낡은 scope 기대값만 새 저장 계약에 맞추고 세 가지 안전 단언과 자동 job 생성 단언을 유지했습니다. 기존 owner=personal 가정의 시험도 owner 통합 조회와 실제 개인 에이전트 거부를 각각 검증하도록 바꿨습니다.

## 실행 증거

현재 Run 소유 마커·포트·역할을 확인한 PostgreSQL만 사용했습니다. 자격증명은 기록하지 않았고 클러스터는 런타임 소유입니다. shared editable venv를 재사용하되 `PYTHONPATH=src`로 이 작업 트리 제품 소스를 지정했습니다.

- 변경 전 실제 회귀: `5 failed in 1.10s`. schema_not_ready, revoke 후 HTTP 200, grant의 출처 변경, 기본 owner의 회사 문서 404, 낡은 delivery scope 기대값을 재현했습니다.
- 관련 회귀: `151 passed in 16.90s`.
- 인접 API/MCP·백필·관측·파이프라인: `74 passed in 13.80s`.
- 마지막 좁은 재검사: `19 passed in 1.92s`. 앞선 결과와 중복되므로 19건을 합산하지 않습니다. 이 실행은 구 0075 스키마 추가 거부 사례도 포함합니다.
- 변경 파일 ruff: `All checks passed!`; `alembic heads`: `0076_kb_v3_collection_merge (head)`; `git diff --check`: exit 0.

전체 명령·종료 코드·원문 SHA-256은 `review/kb-v3/access-delta-20261027/evidence.json`에 있습니다. 원출력은 같은 폴더의 `.log.gz`로 무손실 보존했습니다. 첫 실행의 venv 미탐색(exit 127), editable 소스 경로 불일치로 인한 수집 오류, 초기 확대 시험의 낡은 owner 기대값과 uv 하위명령 exit 2도 성공으로 세지 않았습니다. 수집 오류는 도구 대화에 남으며, 최종 RED 파일은 그 뒤 현재 소스를 지정해 실행한 유효한 반례입니다. uv는 작업 폴더 캐시 지정 후 해당 offline SQL 시험까지 통과했으며, 초기 하위 stderr는 확인하지 않았습니다.

## 남은 실행과 소유자

이 결과는 실제 PostgreSQL fixture와 ASGI HTTP 검증 범위입니다. 전체 리팩토링, 실브라우저, 실제 L2 모델 응답, 운영 마이그레이션 또는 배포 성공을 뜻하지 않습니다.

- 프론트 `000c2556` 타 공급자 독립 검증: `787a5416-da19-4eee-807d-c1f1095b727b`.
- L2 기본 활성화 및 배포 런타임의 고정 public PDF/상충 주장 검증 operation: `26fff9a3-7859-4c2e-a79a-60bcc7a569dd`. Flash 구현은 반환 후 GPT/Claude 검증이 필요합니다.
- 루트는 위 결과를 원문·정확 SHA와 대조하고 교정본에 통합한 뒤 main/push/generation 배포를 예약합니다. 예약 후 폴링하지 않고 반환합니다. 배포 뒤 지원 operation으로 PDF·상충 주장 실경로, 운영 access/job 백필 dry-run→apply와 데이터 보존을 확인합니다.
- 알려진 중첩 Codex L2 app-server EPERM은 우회·동일 재시도하지 않습니다. 운영 장애로 일반화하지 않습니다. PMS done과 전체 완료는 아직 처리하지 않았습니다.
