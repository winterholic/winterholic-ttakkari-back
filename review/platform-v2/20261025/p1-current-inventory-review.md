# P1 현재 후보 수집 및 분류 대조

2026-10-25 · root Codex 명령 샌드박스

소스 기준은 `2fde9018fde4ea84af96331433ba789a85875433`, `src` tree `3b0619a1f50aca6bb799218f3794bee4853cc9f8`다. 제품 `src` 변경은 없다. 스캐너에는 이번 분류/표기 교정이 포함돼 있으며 정확한 SHA256은 [최종 snapshot](p1-inventory-after-classification.json)의 `measurement.scanner_sha256`에 기록했다. 소스 기준 SHA와 교정 스캐너를 구분한다.

## 실행과 발견

기존 `scripts.platform_v2_inventory.run_audit(AuditContext())`를 명시적으로 호출해 JSON으로 저장했다. `main(argv)`는 인자를 무시하고 과거 문서를 생성하므로 호출하지 않았다. `write_outputs`도 실제 문서 root에는 실행하지 않았다. 기존 문서/과거 감사 원문을 최신인 것처럼 일괄 덮어쓰지 않았다.

| 수집 범주 | 현재 수집 수 | coverage |
|---|---:|---|
| HTTP/WS routes | 581 | unknown |
| MCP tools | 135 | unknown |
| ORM tables | 106 | unknown |
| Alembic revisions | 72 | reconciled |
| 설정 필드 | 232 | unknown |
| `src/**/*.py` | 391 | reconciled |

`extraction.ok=true`, `coverage.mismatch=[]`, 최종 `classification.all_collected_classified=true`다. **이는 수집 분모의 분류 상태이며 기능 전체 감사 완료가 아니다.** 네 범주의 loaded object/source-byte binding은 미확인이고 routes의 독립 정적 발견 집합도 없다. 로더 경로만으로 실행된 객체가 현재 source byte와 같다고 단정하지 않는다. `scan_report`의 binding 관련 공통 이유 문구는 unknown도 포함하는 포괄 문구이며, 실제 다른 checkout의 import가 관측됐다는 뜻으로 읽지 않는다.

정적 파일 SCC 11개, 동적 import 위치 5개, 모듈 의존 방향 edge 145개도 최종 snapshot에 보존했다. SCC를 런타임 순환 장애로, edge를 실제 실행 시 호출/부하로 해석하지 않는다. 이 분모는 frontend·scripts·tests·배포 shell을 포함하지 않는다.

### 좁은 교정

- 최초 [현 후보 수집 원문](p1-current-inventory.json)에서 `0071_library_platform_v2_merge` 한 건이 `unmatched`였다. library/executor의 기존 부모 DDL 소유권을 보존하는 no-op merge이므로 배포 제어 `core.generation_deploy`에 정확한 revision명으로 분류했다. 알 수 없는 migration의 일반 fallback은 계속 unmatched다.
- 스캐너의 한계 설명에 과거 상수 `88/106 tables`, `op.execute 66건`, `SCC 8개`가 남아 있었다. 수치의 정본은 구조화된 수집 결과이므로, 한계 설명은 해당 필드를 참조하도록 바꿨다. 최초 snapshot의 이 설명 문자열은 **낡은 표기이며 사용하지 않는다**. 원문을 조용히 수정하지 않고 최종 snapshot을 구분했다.
- `write_outputs`의 다른 역사 수치/권고 문구와 CLI 부작용은 이번에 고치지 않았다. 현재 수집 근거에는 쓰지 않는다. 새 전체 문서 생성 경로를 실행하기 전에 별도 교정이 필요하다.

## 실제 역사 snapshot과의 차이

저장된 `backend-module-inventory.json`의 `scan_commit`은 전 모듈이 `1edfc43acc95c4e5a55045300e0b23b9f29b506a`다. 이를 직접 확인하고 [수집 ID 집합 차이](p1-current-vs-1edfc43.json)를 만들었다. manage_work에 있던 별도 141d146 측정값과 혼합하지 않았다.

- routes: 579→581. `POST /api/agent/keep`, `POST /api/orchestrator/work` 추가.
- tools: 133→135. `keep_artifact`, `manage_work` 추가.
- migrations: 68→72. library 0068, game executor 0069, consumer queue 0070, merge 0071 추가.
- Python sources: 385→391. keep evidence, background consumers, context index queue, creative asset worker, orchestrator work, queued work 파일 추가.
- 수집된 table/config ID 집합은 같다. 이것만으로 기존 필드/제약/설정 의미가 같다고 보장하지 않는다. 제거된 수집 ID는 이번 대조에서 없다.

## 확인 명령과 결과

- 수정 전 inline assertion: `classify_migration('0071_library_platform_v2_merge') == 'core.generation_deploy'` → `AssertionError: integrated deployment merge is unclassified`.
- 수정 후 merge 분류·unknown negative control·library parent 분류 inline assertions → **3 passed**. 별도 신규 테스트 파일은 추가하지 않았다.
- `TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src:. ../back/.venv/bin/python -m pytest --noconftest -o addopts='' unit_tests/test_platform_v2_inventory.py -q` → **51 passed in 49.97s**.
- 그 뒤 한계 설명의 낡은 숫자만 제거하고 동일 명령에 `-k write_outputs_reflects_new_contract` → **1 passed, 50 deselected in 3.65s**. 이 테스트의 writer는 pytest 임시 root에만 쓴다. 51+1을 서로 다른 52개 검사라고 합산하지 않는다.
- 최종 실제 `run_audit` 재수집으로 counts/coverage/분류와 스캐너 hash를 기록했다. 이 재수집은 마지막 스캐너 변경에 대한 것이며 제품 111개 회귀/기존 Caddy 반례를 반복한 것이 아니다.
- Ruff 두 Python 파일 및 `git diff --check` 통과.

## P0/PMS와 남은 범위

MCP `ops_list_deploys(limit=3)`와 `backend_deploy_status(job_id='596947fc-f72d-4d68-b0e9-966826673f21')`로 운영 영수증을 확인했다. 운영은 `d56455c816cf286e3cf367d083f3affcb45b00fe`, `succeeded`, `skip_tests=false`, candidate slot 0, candidate generation `aa06f54d-73a7-468d-ad50-d202e03413ca`다. release path는 `/Users/winterholic/.ttakkari/deploy/releases/d56455c816cf286e3cf367d083f3affcb45b00fe`. 이 메타데이터 확인은 현재 실시간 serving 요청/모델 응답/MCP/재개/backup restore 검증을 대체하지 않는다. prefix만 전달한 첫 status 조회는 UUID 길이 검증 422였고 full UUID 조회로 교정했다.

연결 PMS의 9개 항목을 실제 조회했고 P3.1 하나만 done, 나머지 8개는 미완료다. 상태를 완료로 올리지 않았다.

P1 전체는 review를 유지한다. frontend/runtime/test 분모의 현 후보 정합화와 기능 의미 대조가 남는다. P0 백업/복원·실시간 소유권·성능, P2 계약 통합, FND1 자동 전환, 실제 DB 왕복/DB 9건·library 통합·전체 gate·독립 residual 검증·P4/P6 운영 실증도 남는다. 알려진 initdb shmget 거부는 재시도하지 않았다. main push와 운영 배포는 계속 보류한다.
