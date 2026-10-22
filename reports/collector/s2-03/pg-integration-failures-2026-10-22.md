# PostgreSQL 통합 시험 실패 3건 판정 — 2026-10-22

대상: `collector/s2-03-resume-20261021` @165e42b. 독립 검증에서 collector 묶음 212 passed / 3 failed였다. 세 건을 단독 재현하고 설계 문서(`stage-handler.md`)와 코드를 대조했다. **세 건 모두 코드 결함이 아니라 시험이 낡은 것**이었고, 시험이 지키려던 계약은 코드가 지키는 것을 따로 관측해 확인했다. 시험은 의도를 보존해 갱신했고, 낡은 단언을 지우기만 한 것이 아니다.

## 1. `test_cancel_queued_running_and_finished` — 시험 낡음

- 재현: 마지막 줄의 cancel이 409가 아니라 `200 {"status":"cancelled"}`를 돌려줬다.
- 원인: `jobs.finish`(`jobs.py:246`)가 cancel 요청이 기록된 job의 `succeeded/partial/retry_wait/budget_wait`를 `cancelled`로 바꾼다(`stage-handler.md`의 취소 경합 수정, `last_error.finished_as`에 원래 상태 기록). 시험의 "running job" 은 cancel 요청 뒤 `succeeded`로 끝났으므로 이미 `cancelled`이고, `jobs.cancel`은 cancelled job에 멱등이다. queued job 두 번째 cancel이 200인 것과 같은 규칙이다.
- 계약 확인: `jobs.cancel`(`jobs.py:311-332`)은 succeeded 같은 종료 상태에는 여전히 `JobStateError`(409 `invalid_job_state`)를 던진다. 코드 결함 없음.
- 갱신: (a) cancel이 먼저 기록된 job은 cancelled로 끝나고 재-cancel은 200 멱등임을 단언, (b) cancel 없이 실제로 succeeded가 된 별도 job에 cancel하면 409 `invalid_job_state`임을 단언. 원래 의도("종료된 job은 취소할 수 없다")는 (b)로 보존했다.

## 2. `test_storage_cap_pauses_the_job_without_fetching` — 시험 낡음, 계약은 변형되어 지켜짐

- 재현: `_storage_full` monkeypatch가 handler에 닿지 않아 job이 `succeeded`로 끝났다. `_storage_full`은 `src`에서 정의만 있고 호출처가 없다(unit 시험 1건만 직접 호출).
- 원인: cap 집행이 `FileBlobStore.put`의 `flock` 아래 실제 root 사용량 검사와 handler의 `_order_quota_waiters`로 이동했다(`bc9907f`, `165e42b`). 시험의 "cap이 차면 fetch 없이 멈춘다"는 문장은 현재 설계와 정확히 같지 않다. 새 bytes가 필요한지는 fetch해야 알 수 있고, 같은 SHA가 이미 durable orphan이면 cap-full이어도 진행해야 하므로 fresh URL은 **한 번** fetch한다(`stage-handler.md` cap-full replay 항, `blob-cap-rework.md`). 그 뒤 `used + quota_bytes <= cap`이 되기 전에는 다시 fetch하지 않는다.
- 실제 계약을 실DB·실 handler로 관측(cap = /a 크기, PAGE_SIZE=1, 두 URL):
  - 1회차: `retry_wait` / `storage_cap_reached`, `requested_bytes`·`cap_bytes` 기록, `processed==1`, item 1건, `/a` fetch 1회·`/b` fetch 1회, root 사용량은 `/a` 크기 그대로(새 blob·stage 파일 없음), frontier에 `/b`가 `t=1, quota_bytes=size_b`로 남음, accounting 합 일치.
  - 2회차(공간 그대로): 여전히 `retry_wait`, `/b` fetch 횟수 1 그대로.
  - cap 확장 후: `succeeded`, item 2건, `/b` fetch 2회, frontier 비어 있음.
- 갱신: 시험 이름을 `..._pauses_the_job_and_does_not_refetch_while_full`로 바꾸고 위 계약을 단언한다. 낡은 `_storage_full` monkeypatch는 제거했다. 변이 시험으로, 대기 항목을 다시 fetch하게 만들면(`_order_quota_waiters` 조건 무력화) 이 시험이 실패함을 확인했다.
- **판정 주의**: 원 시험의 문자 그대로의 "fetch 0회"는 더 이상 계약이 아니다. 현재 계약은 "공간이 안 생기면 같은 URL을 재-fetch하지 않는다"이다. 이는 문서에 이미 있는 설계이며 이번에 바꾼 것이 아니다.

## 3. `test_store_raw_false_keeps_hash_only_and_says_so` — 시험 낡음, 원문은 저장되지 않음

- 재현: `blobs.root.rglob("*")`가 비어 있지 않았다.
- 원인 확인: root를 직접 열어 보니 내용은 **0바이트 `.quota.lock` 하나뿐**이다. 샤드 디렉터리, blob, stage, `.lineage`는 없고, 어떤 파일에도 페이지 바이트(`<html`)나 SHA가 없다. 파일은 handler가 매 loop `blobs.usage_bytes()`로 cap을 확인할 때 quota lock을 만들기 때문에 생긴다. `put()`은 `spec.store_raw`가 false면 호출되지 않는다(`pipeline.py:234-247`). 결함 아님.
- 갱신: `stored_raw_files(root)` helper(0바이트 `.quota.lock` 제외 모든 일반 파일)가 `[]`이고, `.lineage`·sha 샤드 디렉터리가 없고, 남은 파일에 `<html`이 없음을 단언한다. 같은 helper가 `store_raw=true`에서는 blob과 manifest를 실제로 찾는다는 양성 대조 시험(`test_store_raw_true_leaves_the_blob_the_scan_looks_for`)을 추가했다. 변이 시험으로, `store_raw` 분기를 항상 참으로 바꾸면 이 시험이 실패함을 확인했다.

## 남은 관찰 (이번에 바꾸지 않음)

- `pipeline._storage_full`은 handler에서 쓰이지 않는 죽은 코드다. `unit_tests/test_collector_blob_store.py`의 사전검사 시험만 호출한다. 제거 여부는 별도 결정으로 남긴다.
- cap이 계속 찬 동안 retry 때마다 claim으로 `attempts`가 늘어 `max_attempts`에 도달하면 `jobs.finish`가 `attempts_exhausted`로 `failed` 처리한다. 이 상호작용은 이번에 재현하지 않았다(미검증).
