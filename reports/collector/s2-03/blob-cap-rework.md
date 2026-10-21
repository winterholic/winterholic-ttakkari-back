# Collector blob cap 재작업 — 2026-10-21

## 변경 결정

- 설정 `collector_blob_cap_bytes`를 collector 전용 blob root 전체에 적용되는 전역 cap으로 집행한다. 현재 설정 기본은 20 GiB다. tenant 별 quota는 없다.
- 집행 단위는 실제 raw blob 및 staging 파일의 logical bytes다. SHA가 같은 raw body는 파일 한 개로 계산하고, DB row가 아직 참조하지 않는 durable blob/orphan도 계산한다. DB 참조 hash 합계(`store.blob_bytes_used`)는 별도 원장 지표이며 cap 판정에 쓰지 않는다. `.lineage` provenance metadata와 `.quota.lock` 파일은 raw-byte cap에서 제외한다.
- DB와 파일 간 원자 transaction은 만들지 않는다. 대신 provenance manifest를 먼저 내구화하고, 이후 SHA staging file을 내구화해 원자 replace한다. DB commit 전 프로세스 종료/rollback/fencing/cancel 뒤에도 blob은 삭제하지 않으며 cap에 계속 반영한다.
- quota lock은 collector root의 OS `flock`이다. 교차 프로세스 put/usage scan이 직렬화되고 프로세스 종료 시 kernel이 lock을 해제한다. 거절은 `BlobQuotaExceeded` → job `retry_wait/storage_cap_reached` 및 warning log다.
- 같은 크기의 기존 파일도 SHA 검증한다. 손상이면 정확한 bytes를 staging 후 원자 replace한다. 읽기는 계속 SHA 확인한다.
- symlink root/shard/blob/stage와 root 안 비정규 파일은 거부한다. 일반 orphan 자동 삭제는 하지 않는다. DB 원장과 manifest를 자동 reconcile해 row를 재생성하는 작업은 아직 없다.

## 중단점 계약

| 중단점 | 재개 동작 / 보존 |
|---|---|
| manifest 전 | raw 파일 없음. 원래 fetch를 재시도한다. |
| manifest 뒤, raw staging 전 | `.lineage`에 tenant/scope/job/item/source/URL/SHA 의도가 남고 원문은 없다. fetch 재시도 대상이다. |
| staging 쓰기 중 | 다음 lock 소유자가 hash가 불일치하는 `.stage-<sha>`만 제거한다. manifest는 유지한다. |
| staging fsync 뒤, final rename 전 | 재개 시 hash가 맞는 staging을 최종 SHA 경로로 승격한다. bytes와 manifest를 보존한다. |
| final rename 뒤, DB commit 전/rollback/fence/cancel/crash | 최종 blob과 manifest를 유지한다. 실제 raw bytes cap에 포함한다. retry는 같은 SHA를 재사용한다. |
| DB commit 뒤 | item 원장은 기존 DB transaction으로 저장되고 manifest는 감사·복구 자료로 남는다. |
| quota 초과 | 추가 raw 파일을 쓰지 않고 job을 `retry_wait/storage_cap_reached`로 둔다. 같은 page에서 이미 준비된 prefix만 DB transaction으로 commit하고, 거절된 entry와 뒤쪽은 frontier에 남긴다. prefix commit 자체가 rollback/fence되면 manifest와 blob으로 복구 근거를 남긴다. |

## 수정 파일

- `src/ttakkari/collector/blobs.py`: cross-process quota lock, root의 실제 bytes 집계, staging 복구, provenance manifest, hash 기반 atomic repair, 경로 검사.
- `src/ttakkari/collector/pipeline.py`: 실제 blob root 기준 cap 사전검사, item provenance 전달, quota race의 명시적 retry 상태/로그.
- `src/ttakkari/collector/store.py`: DB 참조 bytes helper가 filesystem usage가 아님을 명시.
- `unit_tests/test_collector_blob_store.py`: DB-free quota/crash/recovery/repair/symlink/cross-process 검사.
- `docs/design/collector-platform/stage-handler.md`: cap 계약과 crash 창 기록.

구현 commit: `bc9907f2874a5f3fc5d8e29cb49cef98a1c9d504`

구현 tree: `dc6a21835f517492731334c7151df231ada1464b`

## 이번 Run 실행 증거

명령:

```text
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q -p no:cacheprovider --confcutdir=unit_tests unit_tests/test_collector_blob_store.py unit_tests/test_collector_migration_chain.py unit_tests/test_collector_parser.py unit_tests/test_collector_policy.py
```

```text
rc=0
55 passed, 68 subtests passed in 0.94s
```

포함한 직접 관측: put 후 synthetic DB rollback exception에서도 blob+lineage 보존, final rename failure 후 reopen/stage promote, subprocess `os._exit(73)` 직후 reopen 및 kernel lock 회수, 서로 다른 동시 cross-process writer 중 한 건만 cap 내 저장, 동일 SHA dedupe, 같은 크기 손상 복구, repair 원자 교체, stale partial stage, cap 경계, root symlink 거부, DB row 없이 orphan이 `_storage_full()`에 반영됨, quota pause 시 성공 prefix만 진행되고 거절 entry/tail은 frontier에 유지됨.

```text
ruff check <변경 collector 경로와 관련 unit tests>
rc=0
All checks passed!

git diff --cached --check
rc=0
(no output)
```

생성 patch는 base archive에 대해 `git apply --check` 통과 후 적용했고, 임시 git checkout의 `git diff --check`도 통과했다.

raw byte 측정은 격리 temporary root에 11-byte blob 1개와 manifest 1개를 넣어 실행했다. 출력:

```text
db_referenced_bytes=0 (no DB opened); raw_file_count=1; raw_logical_bytes=11; store_usage_bytes=11; allocated_raw_bytes=4096; lineage_files=1
```

이는 실제 collector 운영 root 사용량이 아니라 임시 fixture root 측정이다. quota는 논리 raw bytes 기준이며 filesystem allocated block bytes와는 다른 지표다.

## 보존한 이전 근거와 미검증 범위

`collector-s2-resume-verification-2026-10-21.md`의 원래 근거는 재실행하지 않고 보존한다: S2-02 cancel/fencing pure counterexample 및 S2-03/04 fetch/TLS/policy 관련 150 passed/68 subtests의 기존 영수증. 이는 이번 Run의 재검증으로 세지 않는다.

실DB 시험·공유/운영 PostgreSQL·전체 suite는 실행하지 않았다. `tests/conftest.py`의 initdb 실패 원인은 이전 실행에서 stderr가 남지 않아 미상이고, 같은 조건을 재시도하지 않았다. 이번 변경에서 미검증인 항목은 다음과 같다.

- DB rollback, `AttemptFenced`, 취소 transaction 뒤 DB item/dedupe/cache/outbox/cursor 원자성과 재생성 동작
- manifest를 DB 참조와 자동 대조해 item을 재구성하는 reconciler
- DB-backed end-to-end pipeline quota 거절 상태, 기존 0063 upgrade/downgrade와 PostgreSQL 통합
- full repository test suite

teacher/judge/LLM 호출은 하지 않았고 default-off 설정·migration/schema는 변경하지 않았다. PMS `[CP-S2-03]`은 이미 `review`였으며 done으로 승격하지 않았다. sharedmain, merge, 배포 예약, 05:00 stop 예약은 변경하지 않았다.
