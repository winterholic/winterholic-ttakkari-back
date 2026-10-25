# Runner 파일 등록 원자성 독립 검증 — `9f087f0`

- 판정: **요청한 예외 처리 경로에서 재현된 결함 없음.** 측정·raw/env 보존, 등록 실패 cleanup, terminal 상태, 일반 파일 연결 회귀를 실제 PostgreSQL로 확인했다.
- 검증 대상: `9f087f070c254d57e6be2e971ef572a97379d1a7` (parent `f1dfaaa66eeb97267fc7235de110ae3805375f29`).
- 실행 위치: `/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2`만 사용. 시작 HEAD `0f675591bc25c3c2329f299290f5180e5c2d0079`는 대상의 자식이며, 대상 대비 `src/` 및 `tests/` diff는 비어 있었다. 제품 소스와 기존 테스트는 수정하지 않았다.
- 비교 기준: `refactor-v2-requirements.md` §6의 모든 원본 측정·실패 보존, `runner-evidence-review-121ab9e.md`의 등록 후 예외 FAIL, `runner-registration-atomicity-fix.md`에 적힌 교정 계약.

## DB와 실행 환경

`TTAKKARI_TEST_DB` 환경변수는 연결 URL 자체를 출력하지 않고, 이 identity 쿼리 결과만 출력했다.

```text
select current_database(), current_user, inet_server_addr()::text, inet_server_port()
database=ttakkari_test user=ttk_test host=127.0.0.1/32 port=59432
```

이는 주입된 표준 테스트 DB다. 운영 DB나 기존 실패한 PG 복원 경로는 사용하지 않았다. 테스트는 지정 worktree의 `.venv` (Python 3.13.15, pytest 9.1.1)와 실제 PostgreSQL·실제 artifact 보관 서비스를 사용했다. 오류 지점만 monkeypatch로 주입했다.

## 결과

| 검증 | 결과 | 관측 |
|---|---|---|
| `register_file` 두 번째 반환 직후 예외 | PASS | Run `failed`; 별도 DB session에서 raw/env 및 RTF 측정 1건 유지; 신규 Artifact/ResearchArtifact 0; 보관 디렉터리 증가 0; 원본·기존 marker 보존 |
| 두 번째 파일의 부분 복사 후 `OSError` | PASS | 같은 보존·rollback 조건. 목적 디렉터리는 복사 직전 추적되어 정리됨 |
| 실제 FK 오류 — `flush` 시점 | PASS | PostgreSQL FK 제약이 flush를 거부; 측정은 보존되고 연결 및 새 Artifact row와 복사본은 0 |
| 실제 FK 오류 — 최종 `commit` 시점 | PASS | FK 오류가 최종 commit에서 발생; succeeded 상태는 영속되지 않고 Run `failed`; 측정·raw/env 보존, 신규 row/복사본 0 |
| `CancelledError` | PASS | Run `cancelled`; rollback 후 측정·raw/env 유지, 신규 row/복사본 0 |
| WAV API·동일 경로 dedupe·legacy 연결 | PASS | WAV metadata/content 응답 유지, 같은 경로는 1개로 연결, 기존 Artifact ID legacy 연결 회귀 유지 |
| cleanup 자체가 `OSError` | PASS (오류 보고) | cleanup 실패가 logger ERROR 및 Run `error_message`에 경로와 함께 표시됨. 정리 실패 자체는 파일을 남기므로 잔여 사본은 1개이며, 정상 cleanup 성공으로 오인되지 않음 |
| 실패 Run 측정 analyzer 제외 | PASS | failed Run에 overall 측정을 넣어 `build_compare_series` 실행; `metrics_catalog={}`, `metrics=[]` |

### 실제 실행 명령과 출력

새 등록 실패 테스트의 다섯 경로(기존 네 반례 + 최종 commit 오류):

```sh
.venv/bin/python -m pytest -vv tests/test_scenario_b_artifacts.py -k registration_failure
```

```text
test_artifact_registration_failure_keeps_measurements_and_removes_only_new_copies[after_register] PASSED
test_artifact_registration_failure_keeps_measurements_and_removes_only_new_copies[during_copy] PASSED
test_artifact_registration_failure_keeps_measurements_and_removes_only_new_copies[during_flush] PASSED
test_artifact_registration_failure_keeps_measurements_and_removes_only_new_copies[during_commit] PASSED
test_artifact_registration_failure_keeps_measurements_and_removes_only_new_copies[cancelled] PASSED
5 passed, 7 deselected in 0.70s
```

등록 실패 다섯 경로와 WAV API·dedupe·legacy 연결을 함께 실행:

```sh
.venv/bin/python -m pytest -q tests/test_scenario_b_artifacts.py -k 'registration_failure or persists_adapter_artifacts or deduplicates_artifacts_by_normalized_path or preserves_legacy_artifact_id'
```

```text
8 passed, 4 deselected in 1.62s
```

cleanup 실패의 비침묵 처리와 analyzer 필터는 `tests/test_review_9f087f0_probe.py` 임시 probe로 실행했다. 파일은 실행 후 제거했고 기존 테스트 파일은 변경하지 않았다.

```text
2 passed in 0.54s
```

## 코드 경계 확인

고정 대상 소스에서 측정과 raw/env를 먼저 저장할 때 Run을 `running`으로 두고 commit한다. Artifact 연결 완료 후에만 `succeeded`를 설정하며 그 다음 최종 commit을 호출한다. 외부 session에서 보이는 succeeded는 commit 뒤에만 성립한다. 최종 commit 예외에서는 rollback 후 새 session 상태 기준으로 Run을 `failed`로 commit한다.

`register_file`은 UUID 디렉터리 생성 직후, 파일 복사 전에 그 경로를 `created_copies`에 기록한다. rollback/cancel 경로는 이 목록만 정리한다. 원본 source, 기존 보관 marker, legacy Artifact는 cleanup 대상으로 넘기지 않는다. raw artifact 선언의 `artifact_id`는 갱신하지 않는다.

## 범위와 미검증

- 이것은 예외 처리와 DB/파일 보존 검증이다. 갑작스러운 프로세스 종료·호스트 종료 시 DB와 파일 시스템 사이의 원자성을 주장하지 않는다.
- 전체 회귀 313건은 기존 evidence에 있어 반복하지 않았다. 이번 변경과 직접 연결된 다섯 반례 및 세 저장/API 회귀만 실행했다.
- 실제 TTS/Supertonic/Whisper 재추론, GPU/Metal 직접 probe, 호스트 프로세스 목록, runtime 재시작, 브라우저, 다른 Run outbox는 다루지 않았다.
- `git pull --ff-only`는 시작 당시 로컬 브랜치가 원격과 분기되어 실패했다(`fatal: Not possible to fast-forward, aborting.`). 사용자 지정 고정 commit을 바꾸지 않았고, push/deploy도 하지 않았다.
