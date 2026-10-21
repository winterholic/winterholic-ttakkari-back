# ADR0036 후속 구현 범위와 경계

2026-10-21. 기준 checkout은 main `9afe3b2701383f2d606ec868e8f25ddad3c3bc87`이며, 독립 구현 branch에서 원 slice `95fc0a75b919ee1da64a423c40b5fc69cecb06ec`를 적용했다. 이 문서는 첫 slice의 완료 범위를 넓혀 쓰지 않고 이번 변경과 미완료 경계를 기록한다.

## 서비스 소유권

| 서비스 | 원격 API에서 하던 일 | remote API / executor 이후 | 이유와 상태 |
|---|---|---|---|
| Run engine / queued Run 재개 | API generation이 RunManager child를 생성 | executor가 기존 DB queue, command, lease/fence를 소유 | 첫 slice 유지. API remote는 start/stop 때 child를 만들거나 죽이지 않는다. |
| Preview 변환 | API lifespan이 DB artifact queue consumer 기동 | remote API는 기동하지 않고 executor가 durable queue를 소비 | pending/processing 상태와 artifact 경로가 DB에 있어 이관했다. local 모드는 기존 API consumer를 유지한다. |
| Local STT | API lifespan이 DB job consumer와 재시작 복구 기동 | remote API는 기동하지 않고 executor가 durable queue를 소비 | LocalSttJob, Meeting provider-job link, 업로드 경로가 durable 입력이다. local 모드는 기존 동작 유지. |
| TTS | API가 worker와 API 프로세스 메모리의 원문 payload를 관리 | API가 계속 worker를 소유 | `_pending_texts`가 process-local이다. consumer만 executor로 옮기면 accepted job 원문이 유실되므로 이관하지 않았다. 암호화 저장/원문 보존 정책 변경이 필요하다. |
| Qwen desk server | API generation별 request-time warm server 기동·정지 | API가 계속 소유, executor remote Run은 기존 Qwen CLI fallback | DB durable queue가 아니라 API 요청 지연을 줄이는 서비스이며 세대별 loopback owner다. |
| Creative render | API maintenance loop의 주기 scan | executor maintenance loop가 scan | 결과/상태가 DB와 artifact에 남는 주기 작업. |
| NotebookLM | 주기적인 stale-job recovery와 API 요청 실행 | stale-job recovery는 executor, 요청 실행과 service shutdown은 API | recovery scan은 유지보수 작업으로 옮겼다. 사용자 요청은 API에서 처리한다. durable query consumer가 API에 추가되면 별도 이관 검토가 필요하다. |
| Schedules | active API coordinator와 maintenance loop | executor maintenance loop | API coordinator의 중복 due-run 호출을 막았다. local mode에서는 기존 coordinator 유지. |
| 기타 maintenance | API generation이 retention, vectors, reminders, weather, usage, resource history, archive, ops log 작업 | executor가 공용 `maintenance_jobs()` 목록을 그대로 실행 | 목록/간격을 재사용한다. local 모드에서 기존 API 실행 유지. |

## Executor activation과 배포 감독

`deploy/run-executor-manifest.json`은 immutable release의 Python 경로, 정확한 argv, runtime 환경, readiness 증거, activation gate를 기록한다. `deploy/generation-contract.json`의 `run_executor.manifest_sha256`은 이 manifest 파일의 **release archive 내 raw bytes**에 대한 SHA-256이다. digest는 contract에만 두어 manifest가 자기 자신을 참조하지 않는다. manifest 누락, digest 누락/형식 오류/불일치, manifest schema·내용 불일치는 모두 닫힌다. `Releases.archive()`는 git archive로 만든 artifact를 검증하고 기존 artifact 재사용 때도 다시 검증한다. 이는 `.release.json`의 release SHA와 executor 명령의 pinned interpreter 경로 검사를 대체하지 않는다. 기존 `GenerationController.reconcile_services()`는 contract가 `activation=enabled`일 때만 `Slots.ensure_executor()`로 전달한다. 기본 계약은 `disabled`라 feature-off Stage3 후보에서는 executor child를 시작하지 않는다.

enabled 후보도 다음 조건을 모두 검사한다: release marker/SHA와 pinned interpreter, 모든 live API의 protocol2·remote mode, 남은 Run lease 없음, 지원되는 pending command뿐임. readiness는 `runtime_control.executor_generation` pointer, revision, PID+process start time, heartbeat, 해당 PID의 실제 `ttakkari.cli executor --activate` command line을 대조한다. process 조회가 거부되거나 값이 다르면 준비되지 않은 상태로 닫는다.

이 controller path의 실제 launchd 실행, pinned child readiness, API 후보 교체 중 SSE cursor 연속성은 이번 Run에서 실행하지 않았다. executor revision 교체를 위한 orderly drain/bootout/rollback은 아직 구현되지 않았다. pointer/lease가 바뀌지 않은 상태에서 revision이 다른 프로세스를 자동으로 종료하거나 덮어쓰지 않는다. 따라서 manifest와 readiness 연결은 구현됐으나 executor 운영 교체 기능 완료로 보지 않는다.

## Attempt process group과 quarantine

실제 reproduction에서 child leader가 exit code 0으로 끝나고 stdio를 닫아도, 그 child가 남긴 descendant가 process group에 남을 수 있음을 관측했다. `_Handle.proc` 하나만 검사하면 앞선 attempt group이 누락될 수 있어 handle과 `runtime_process_groups` ledger에 owner/fence와 함께 모든 attempt의 `(pid, process_started_at, pgid)`를 기록하고, lease 해제 전 각 group을 확인하도록 했다.

`ttakkari executor-reconcile`는 기본 dry-run, `--apply` 선택 방식이다. owner PID/start time과 fence-bound PGID 증거, permission을 확인한다. lease owner가 살아 있거나 ledger가 없거나 PID/PGID 조회가 불명확하면 회수하지 않는다. apply는 control row → token lease row → owner/process-group/Run rows 순서로 잠그고 snapshot의 owner identity, token, 전체 attempt ledger를 현재 값과 비교한 뒤에만 signal한다. 해당 잠금을 그룹 종료 확인, Run interrupted 기록, lease 해제까지 유지한다. 정상 claim과 process-group 기록은 control lock을 사용하고, 정상 release도 같은 control→lease 잠금·token fence를 사용한다. 기존 Run write fence는 lease row를 잠가 선행 변경을 끝내거나 stale writer를 거부한다. stale token/ledger interleaving에서는 signal 0회를 pure unit/fake DB로 확인했다. PostgreSQL 동적 경합 재현 전에는 이를 실제 운영 피해로 주장하지 않는다. dry-run은 apply를 호출하지 않으며 DB 변경도 하지 않음을 단위 경계로 확인했다. PostgreSQL transaction/locking 및 실제 DB dry-run/apply는 미검증이다. heartbeat 만료만으로 lease를 가져가거나 provider 작업을 복제하지 않는다.

2026-10-21 독립 검증 후속 수정은 별도 feature branch에서 수행했다. manifest content digest와 archive 검증, reconcile pre-signal fence 및 정상 release 잠금 경계를 보강했다. migration 파일/번호/down_revision/contract migration 목록은 이 수정에서 건드리지 않았다. 현재 executor `0063_run_executor`와 delivery `0062_delivery_change_sets`가 각각 `0061_local_tts`를 parent로 둔다는 점만 기록하며, 최종 직렬 graph는 부모 통합 단계에서 정한다.

## Migration 통합

현재 graph는 `0060_collector_job_runtime → 0061_local_tts → 0063_run_executor`이며 `0063`은 ownership trigger와 `runtime_process_groups` ledger를 additive로 만든다. `deploy/generation-contract.json`의 head와 `online_migration_from`도 현재 chain에 맞췄다. executor revision `0063_run_executor`는 사용자가 지정한 임시 통합 번호다. delivery 작업 c77b60de의 `0062`가 통합되면 executor `down_revision`을 해당 최종 chain으로 바꾸고 contract head/온라인 허용 목록 및 downgrade 순서를 함께 재검증해야 한다. 그 통합 전에는 최종 main graph라고 주장할 수 없다.

## 남은 검증

- 미실행: PostgreSQL claim/lock/trigger, migration 실제 적용·downgrade, API/executor 두 프로세스 및 SSE cursor 교체 경계. `tests/` conftest는 initdb 단계에서 실패해 테스트 collection에 진입하지 못했다. SQLite/fake 검사를 PG 증거로 대체하지 않는다.
- 미실행: executor activation enabled 후보의 실제 generation controller / launchd / process readiness 통합과 executor replacement/rollback.
- 잔여 소유권 문제: TTS accepted payload는 process-local이라 비정상 API 종료 때 durable하지 않다. Qwen managed server는 API generation마다 중복 기동될 수 있다. TTS payload 정책, Qwen 공유 소유권, executor replacement/drain/rollback은 미구현이다.
- 구현됐으나 미실행: durable attempt PGID ledger와 dry-run/apply orphan reconcile. owner/group identity가 unknown이면 apply가 fail-closed한다.
- API 소유 유지: TTS 원문/worker, request-time Qwen. remote API가 해당 worker를 시작하는 것은 현재 소스/데이터 수명에 근거한 선택이다.
- 미실행: 실모델 응답, 실제 MCP tool 실행, provider conversation resume.

운영 활성화는 이 문서만으로 승인하지 않는다. 기본 feature-off release와 remote API/executor 실제 activation은 서로 다른 상태다.
