# C4 held item 복구 후속 (2026-10-25)

- 기준 revision: `ace2c8be1ab2a898d078453df7837f0c197872da`
- 구현 branch/worktree: `feat/collection-recovery-20261025`, `back-collection-recovery`
- 코드 교정 commit: `619f09320d73211798030a277c53212a8f31e035`
- `back-collection-ml`과 진행 중인 `back-collection-precise` worktree는 변경하지 않았다.

## 변경

`unhold_item`은 기존 held envelope의 수집 시점 권리를 유지하고 현재 활성 정책의 `store`·`knowledge` 권한도 확인한다. 무제한 retention만 수동 KB 전달을 허용한다. 성공 시 append-only `CollectorReview`에 문서 효용 검토만 기록하고 사실성은 `unverified`로 표시한 뒤, 원래 envelope/payload hash를 가리키는 `knowledge` outbox를 만든다. 다른 tenant, 미인증, 수집 시점 knowledge 거부, 현재 권리 철회, 유한 retention 경로는 전달을 거부한다.

## 실행 증거

기존 isolated PostgreSQL 및 HTTP fixture로 held 수집부터 인증 unhold, `kb_delivery.deliver_once`, imported delivery receipt, KB 검색, `KbSource.origin_meta` 출처 확인까지 실행했다. 검증에서 envelope, payload hash, raw hash가 그대로였고 review에는 사실성 미검증이 기록됐다.

RED 재현 명령:

```sh
PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_collection_missions.py -k 'unhold_imports_original or unhold_rejects_revoked or unhold_requires_auth' --tb=short
```

구현 전 결과: `3 failed, 12 deselected`; 그중 positive 경로는 review 응답이 없었고 cross-tenant 경로는 기존 구현에서 unhold를 허용했다.

최종 좁은 검증 명령:

```sh
PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_collection_missions.py::test_unhold_imports_original_held_envelope_through_review_receipt_and_search tests/test_collection_missions.py::test_unhold_rejects_revoked_knowledge_rights_without_mutation tests/test_collection_missions.py::test_unhold_cannot_grant_knowledge_denied_at_collection tests/test_collection_missions.py::test_unhold_cannot_import_under_finite_retention tests/test_collection_missions.py::test_unhold_requires_auth_and_rejects_cross_tenant_item unit_tests/test_collection_mission_triage_unhold.py --tb=short
```

결과: `8 passed in 1.22s`; `db reset: 5 selective (avg 11.8/108 tables), 0 forced full, 0 fell back to full`.

```sh
../back/.venv/bin/ruff check src/ttakkari/api/collection_missions.py src/ttakkari/collector/missions.py tests/test_collection_missions.py unit_tests/test_collection_mission_triage_unhold.py
git diff --check
```

결과: `All checks passed!`; `git diff --check` 출력 없음(종료 코드 0).

변경 코드 파일은 `src/ttakkari/collector/missions.py`, `src/ttakkari/api/collection_missions.py`, `tests/test_collection_missions.py`, `unit_tests/test_collection_mission_triage_unhold.py`다. 이 기록은 별도 commit에 포함한다.

## 범위와 한계

이것은 수동 unhold→KB 적재 복구의 실행 증거이며 C4 전체 완료나 precise stage3 완료가 아니다. pending을 소비하는 teacher/정밀평가 engine은 여전히 없다. stage2 평가, 모델 실행·다운로드 및 전체 240 회귀는 재실행하지 않았다. 검색은 KB endpoint의 결과와 해당 문서의 imported receipt/source provenance를 확인했으며, 답변 품질이나 사실성을 평가하지 않았다.

평가 보고 숫자를 새로 검증하거나 재주장하지 않는다. 보고서의 관련성 평가는 Mr. TyDi 질의-위키 문단 proxy이며 실제 임무 목표-웹 문서 분포를 보증하지 않는다. 보고서에 기록된 300쌍 pair bootstrap CI는 AUC 0.949의 `[0.922, 0.970]`이다. 별도 질의 군집 bootstrap CI는 이번에 증거를 확인하지 못해 주장하지 않는다.
