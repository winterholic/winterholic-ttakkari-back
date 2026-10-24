# P4 실행부 독립 프로세스 여정 실증

- 일시: 2026-10-25
- 베이스: `c2657bc` (integration/platform-v2-execution-all-20261025). 확장 단계(실행 중 교체·crash·다른 큐)는 `04c239d` 위에서 추가
- 이전 시도 철회: c2657bc의 `tests/test_executor_process_journey.py`와 이 보고서 이전판은 `python -c` 인라인 가짜 파일큐로 Core/Executor를 흉내냈고 ttakkari 코드를 import하지 않았다. 제품 증거가 아니므로 둘 다 이 커밋에서 실제 구현·보고서로 교체했다.
- 제품 src 수정: 없음. 실패 테스트(red)를 먼저 보일 제품 결함이 나오지 않았다. 변경 파일은 신규 테스트와 `tests/fake_agents/fake_claude.py`(지연 키워드 `HOLD<n>` 1개 추가)뿐이다.

## 1. 실증한 것

실제 entrypoint를 pytest의 own child로 임시 loopback 포트에 띄웠다.

| 역할 | 명령 | 근거 |
|---|---|---|
| Core API | `python -m ttakkari.cli serve --host 127.0.0.1 --port <임시>` | `tests/test_executor_process_journey.py` `core_process()` |
| 실행부 | `python -m ttakkari.cli executor --activate` | 같은 파일 `start_executor()` |

- 작업 유형: Run. 엔진은 conftest가 이미 주입하는 `tests/fake_agents/fake_claude.py`(`TTAKKARI_CLAUDE_BIN`)다. 이 fake는 기존 테스트 인프라이고 이번에 새로 만든 가짜가 아니다.
- DB: 주입된 `TTAKKARI_TEST_DB`(127.0.0.1, `ttakkari_test`). 자식 env에 URL을 그대로 상속해 계정·port·query를 유지한다. 비밀값은 출력하지 않았다.
- 검증 수단: 자식 `returncode`, `/api/ready` HTTP, DB 행(Run, RuntimeGeneration, RuntimeControl), 자식 로그 파일 assertion이다. 로그는 DB URL·비밀번호·JWT 비밀을 제거(`redact()`)한 뒤에만 단언·실패 메시지에 쓴다. psutil, ps 조회는 쓰지 않았다.
- Core 관측: `CoreObserver`가 교체 시작 전부터 테스트 끝까지 0.05초 간격으로 Core child `returncode is None`·같은 PID·`/api/ready` 200(1초 timeout)·같은 generation을 반복 단언한다. 실패는 저장해 두었다가 `check()`·`window()`·종료 시 `await`로 전파한다. 이것은 유한 표본이고 표본 사이의 순간 중단까지 보증하지 않는다.

| 요구 | 테스트 | 확인한 내용 |
|---|---|---|
| 1 접수 | `test_executor_replacement_keeps_core_and_accepted_work` | 실행부가 없는 동안 `POST /api/sessions/{id}/runs`가 202. DB `Run.status=queued`, `pid`와 `exit_code`는 None. 1.5초간 DB와 API 모두 queued 유지 |
| 2 Core 무배포 | 같은 테스트 | 구 실행부 SIGTERM(exit 0), 접수, 새 revision(`f`*40) 기동·등록·작업 완료 구간에서 observer가 Core child `returncode is None`, 같은 PID, `/api/ready` 200, 같은 generation id를 반복 단언(표본 증거는 3절). 구간 끝에서는 DB `role=api, state=active, pid=Core PID`도 확인 |
| 3 유실 없음 | 같은 테스트 | 새 실행부가 DB에 `role=executor, revision=f*40, pid=child PID`로 등록. 접수 작업이 `succeeded, exit_code=0`. 구 실행부 행은 `offline`, `RuntimeControl.executor_generation`은 새 행 |
| 4 실패 격리·rollback | `test_failed_executor_start_is_isolated_and_rollback_resumes_work` | ① 실행부가 살아 있을 때 새 revision 기동은 `already has a live owner`로 비정상 종료. 기존 실행부 `returncode None`, DB `active` 유지, 이후 작업 정상 완료 ② 실행부를 내리고 작업 접수 후, pinned revision 없는 설정과 접속 불가 DB(port=1)의 새 실행부는 각각 비정상 종료. 로그에서 원인을 단언: 전자는 `pinned immutable release revision`, 후자는 `ConnectionRefusedError`와 `Connect call failed`, traceback에 이 worktree의 `src/ttakkari/executor.py` 경로가 있고 `ImportError`·`ModuleNotFoundError`·`SyntaxError`는 없음. Core 무영향(observer 표본 유지), 작업은 queued 유지, 실패 revision `f*40`의 등록 행 없음 ③ 이전 정상 revision(`e`*40)으로 rollback하면 접수 작업이 완료되고 rollback 이후 새 작업도 완료 |
| 5 실행 중 교체 | `test_sigterm_during_running_run_drains_then_new_executor_takes_over` | 계약(`current-execution-contract.md` 전환·drain 5, `executor.drain`): SIGTERM은 새 claim을 닫고 진행 Run의 자연 완료를 기다린 뒤 종료한다. 관측: `HOLD8` Run이 running(lease 1)일 때 SIGTERM → 실행부 DB 행 `draining`, 실행부 `returncode None`, 같은 엔진 PID 생존·Run `running`. drain 중 접수한 새 Run은 `queued` 유지. drain 중 새 실행부 기동은 `already has a live owner`로 거절. Run은 같은 엔진 PID로 `succeeded, exit_code=0`, lease 0이 된 뒤 실행부 exit 0. 실행부가 없는 동안 queued Run은 그대로, 새 실행부(`f`*40)가 이어서 `succeeded`. 이 구현에서 새 실행부가 진행 Run을 "이어받는" 경로는 없다(기존 lease가 있으면 새 실행부 등록 거절). 진행 Run은 구 실행부가 끝낸다 |
| 6 register·claim 후 crash | `test_sigkill_after_claim_keeps_ownership_and_blocks_new_executor` | `SLEEP` Run이 claim·엔진 init 이벤트 뒤 실행부 SIGKILL(returncode -9). 1초 뒤에도 Run `running`·lease 1·죽은 실행부 행 `active`, 엔진 PID 생존, Core 무영향. 시간 경과로 만료·자동 회수되는 경로는 없다. 새 실행부는 `existing lease requires drain or orphan reconciliation`으로 거절. 제품 회수 경로 `executor-reconcile --dry-run`은 `process_group_live / would_reconcile`을 보고하고 DB를 바꾸지 않음 |
| 6b 운영자 회수 | `test_operator_reconcile_releases_sigkill_orphan_and_new_executor_resumes` | **이 환경에서 미실행(skip).** 제품 CLI가 agent Run 환경(`TTAKKARI_RUN_ID` 설정)의 `executor-reconcile --apply`를 거절한다(`orphan reconciliation apply belongs to the external operator, not an agent Run`). 가드를 우회하지 않았다. 운영자 shell에서 실행하면 apply→`interrupted`·lease 해제·엔진 종료·새 실행부 재개를 단언하도록 작성했으나, 한 번도 통과시키지 못했으므로 검증된 것으로 보지 않는다 |
| 7 다른 큐 | `test_idle_replacement_consumes_context_index_request` | 컨텍스트 색인(hash embedder, 모델 없음). 실행부가 없는 동안 `POST /api/assistant/sources` 201, DB `index_requested_at` 유지·`last_indexed_at` 없음(1.5초). 새 실행부 기동 뒤 `last_indexed_at` 기록, 요청·started 해제, `last_error` 없음, 문서 1건 이상 |
| 보조 | `test_child_commands_are_real_ttakkari_entrypoints` | 자식 `PYTHONPATH` 첫 항목이 pytest가 import한 같은 소스 트리 `src`이고 DB가 loopback `ttakkari_test`임을 고정 |

## 2. 코드로 본 실제 모듈 실행 (grep)

```
$ grep -n 'ttakkari.cli\|PYTHONPATH\|row.role == "executor"' tests/test_executor_process_journey.py   (확장 커밋 직전 값, docstring 행 생략)
48:    path = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH", "")]))
49:    return os.environ | {"PYTHONPATH": path, "TTAKKARI_RUNTIME_GENERATION": "true",
151:            sys.executable, "-m", "ttakkari.cli", "serve", "--host", "127.0.0.1", "--port", str(port),
176:            sys.executable, "-m", "ttakkari.cli", "executor", "--activate",
188:                assert row.role == "executor" and row.revision == revision
229:    assert child_env()["PYTHONPATH"].split(os.pathsep)[0] == str(SRC)
412:            sys.executable, "-m", "ttakkari.cli", "executor-reconcile", *flags,
```

`python -c`나 가짜 파일큐 스크립트는 없다. 아래 `row.role == "executor"` 행은 자식이 제품 코드(`ExecutorRuntime.register`)로 DB에 남긴 등록 행을 확인한다. `executor-reconcile` 행은 제품 회수 CLI를 자식으로 실행한다.

## 3. 실행 명령과 결과

공유 venv의 editable 설치(`.venv/.../ttakkari.pth`)가 다른 체크아웃 `local-llm-bench-spec-4-1-20261023/src`를 가리킨다. 그대로 실행하면 기존 `test_run_executor.py`조차 import 오류(`cannot import name 'drain'`)가 난다. 따라서 모든 명령에 `PYTHONPATH=<worktree>/src`가 필요하다. 신규 테스트의 자식은 이 값을 스스로 고정한다. 환경 문제이며 이번 범위에서 고치지 않았다.

```
$ export PYTHONPATH=$PWD/src
$ python -m pytest tests/test_executor_process_journey.py -v -s -p no:cacheprovider
6 passed, 1 skipped in 28.77s (확장 후. skip은 6b, 가드 사유)

$ python -m pytest tests/test_run_executor.py tests/test_run_executor_processes.py tests/test_generation_processes.py \
    tests/test_generations.py tests/test_runs_events_masking.py tests/test_agent_stop_all.py -q -p no:cacheprovider
54 passed in 23.19s           (fake_claude 변경과 helper 관련 회귀)
```

초기 구현(cb1be7a)의 넓은 회귀 `97 passed in 31.22s`(`test_pg_sandbox`, `test_tts_executor_isolation` 포함)는 그 시점의 값이다. 확장은 신규 테스트와 fake_claude의 키워드 1개만 바꿨으므로 fake_claude를 쓰는 위 54건을 다시 돌렸다.

Core observer 표본(0.05초 간격 목표, 실제 간격은 요청 시간이 더해져 약 0.06초). 값은 `-s` 출력이며 실행마다 조금 달라진다:

| 구간 | 표본 수 / 경과 | 하한 단언 |
|---|---|---|
| 새 실행부 기동→등록 완료 | 7개 / 0.43초 | 3개 이상 |
| 구 실행부 종료→접수 작업 완료 | 41개 / 2.46초 | 20개 이상 |
| 중복 소유자 기동 실패 | 8개 / 0.45초 | 3개 이상 |
| 설정 누락 기동 실패 | 8개 / 0.43초 | 3개 이상 |
| 접속 불가 DB 기동 실패 | 7~8개 / 0.5초 | 3개 이상 |
| rollback 기동→등록 완료 | 7개 / 0.43초 | 3개 이상 |
| 테스트 전체(observer 수명) | 54개 / 3.2초, 70개 / 4.1초 | 종료 시 실패 전파 |

확장 시나리오의 observer 표본(동일 방식, `-s` 출력):

| 시나리오 | 구간 | 표본 수 / 경과 | 하한 단언 |
|---|---|---|---|
| 5 실행 중 교체 | SIGTERM→새 실행부 처리 완료 | 148~149개 / 약 9.0초 | 40개 이상 |
| 6 crash 차단 | SIGKILL 전후 시나리오 전체 | 41~50개 / 2.4~2.9초 | 40개 이상 |
| 7 컨텍스트 색인 | 실행부 종료→색인 완료 | 39개 / 2.3초 | 20개 이상 |

이 표본은 구간 안에서 Core가 반복 확인으로 정상이었다는 증거이지, 표본 사이의 매 순간 무중단 보증이 아니다.

```
$ ruff check tests/test_executor_process_journey.py   -> All checks passed!
$ git diff --check                                    -> 출력 없음
```

신규·변경 파일 `ruff check tests/test_executor_process_journey.py tests/fake_agents/fake_claude.py`는 All checks passed, `git diff --check`는 출력 없음. `ruff check src tests unit_tests`는 14건, 저장소 전체는 99건을 낸다. 모두 이번에 건드리지 않은 파일(experiments, unit_tests/test_backup_restore.py 등)의 기존 항목이고 신규 파일은 0건이다.

## 4. 변이 검증 (테스트가 실패할 수 있는지)

| 변이 | 결과 |
|---|---|
| 구 실행부를 내리지 않음 | `assert 'succeeded' == 'queued'`로 실패. 접수 후 queued 유지 단언이 실제로 의미를 가짐 |
| 실행부 교체 중 Core를 종료 | `assert (-15 is None)`로 실패. Core 무배포 단언이 실제로 의미를 가짐 |
| Core를 `local` 모드로 변경 | 실행부가 `executor activation requires remote APIs`로 거절해 실패(제품의 기존 가드). 의도한 단언은 아니고 참고용 |

관측 교정 후 추가한 negative control:

| 변이 | 결과 |
|---|---|
| 새 실행부 기동 직후 Core에 SIGSTOP 2초 후 SIGCONT(일시 중단, 종료 후 단발 검사는 통과하는 형태) | `httpx.ReadTimeout`을 원인으로 `core observer failed after 41 samples`로 실패. 기동·등록 구간 관측 실패가 전파됨 |
| 표본 간격 100초(기동·등록 구간 표본 0회) | `only 0 core samples in 0.48s window (need 3)`로 실패 |
| baddb 자식이 `ImportError`(shadow `ttakkari` 패키지)로 실패 | `ConnectionRefusedError` 단언에서 실패. DB 연결 거부가 아닌 시작 실패는 green이 되지 않음 |
| baddb 자식이 `PYTHONPATH` 무효로 다른 체크아웃의 `ttakkari`를 import | `src/ttakkari/executor.py` 경로 단언에서 실패. 처음에는 이 변이가 통과해, 같은 소스 트리 경로 단언을 추가했다 |

확장 시나리오 변이:

| 변이 | 결과 |
|---|---|
| SIGTERM 대신 SIGKILL로 실행부를 죽임 | `timeout waiting for executor draining state`로 실패. drain 계약 단언이 의미를 가짐 |
| 색인 요청 전에 구 실행부를 내리지 않음 | `index_requested_at is not None` 단언 실패(실행부가 먼저 소비). 요청이 DB에 남는다는 단언이 의미를 가짐 |

변이용 임시 파일은 삭제했고 커밋에 포함하지 않았다. 변이 대상은 신규 테스트 한 파일뿐이다.

## 4b. 확장 중 찾은 테스트 환경 함정 (제품 결함 아님)

- conftest의 세션 autouse 픽스처가 pytest 프로세스 안에서 앱 lifespan(legacy)을 열어 `background_consumers`를 돌린다. 이 in-process 소비자가 Core가 기록한 컨텍스트 색인 요청을 실행부보다 먼저 가져가 실행부 증거를 무효로 만들었다(처음 구현에서 요청이 0.5초 만에 소비되어 실패). 이 시나리오는 `pause_claiming()`/`resume_claiming()` 픽스처로 in-process 소비를 멈춘다. 다른 큐 시나리오를 추가할 때도 같은 함정을 확인해야 한다.
- crash 시나리오에서 Run이 `running`이 되자마자 실행부를 죽이면 fake 엔진이 출력 중 파이프가 끊겨 같이 죽는 경합이 있었다(전체 실행 1/2회 실패). 엔진 `agent.session` 이벤트를 기다린 뒤 죽이도록 고쳤고 이후 8회 반복 통과했다.
- 색인 완료 판정은 `last_indexed_at`만 보면 `index_started_at` 해제 전에 통과해 1/4회 실패했다. 둘 다 확인하도록 고쳐 12회 반복 통과했다.

## 5. 실증하지 않은 것

- Core 가용성은 0.05초 간격(실제 약 0.06초)의 유한 표본 관측이다. 표본 사이의 순간 중단이나 지연은 보증하지 않으며, 교체 구간 전체의 무중단을 증명했다고 주장하지 않는다.
- 실제 모델 응답, MCP, 외부 네트워크: 엔진은 fake_claude다.
- 진행 Run 중 교체는 8초짜리 `HOLD8` Run 하나에 대한 SIGTERM drain만 확인했다. 장기 Run, 여러 Run, drain timeout 후 계속 대기, claim 경합, Run 도중 cancel 명령 처리는 다루지 않았다. 새 실행부가 진행 Run을 이어받는 동작은 없고(구현상 거절) 구 실행부가 끝내는 방식만 확인했다.
- crash 후 orphan 회수: 자동 만료·회수 경로가 없고 새 실행부는 거절된다는 현 동작과 dry-run 보고까지만 확인했다. `--apply`로 interrupted 처리·lease 해제·새 실행부 재개는 미실행이다(agent Run 환경에서 제품이 거절, 6b skip). 운영자 shell에서 `-k operator_reconcile`로 실행해야 검증된다(`PYTHONPATH=<worktree>/src` 필요). 회수가 자동이 아니라 운영자 개입인 것이 결함인지는 이 증거로 판단하지 않는다. 계약 문서는 orphan 회수를 외부 controller·운영자 소유로 둔다.
- 접수 작업 유형은 Run과 컨텍스트 색인 둘이다. 컨텍스트 색인은 idle 교체만 다뤘다.
- TTS는 하지 않았다. 실행부의 TTS 소비는 `get_provider()`가 항상 실제 `SupertonicProvider`(로컬 TTS python·모델 assets)를 만들고, 가짜 엔진은 `tests/conftest_tts.py`가 in-process monkeypatch로만 주입한다. 자식 프로세스에 주입할 env·설정 지점이 없어 실제 모델 없이는 끝낼 수 없다. 주입 지점을 새로 만들면 제품 변경이므로 하지 않았다. STT, Creative, Games, NotebookLM도 다루지 않았다.
- "새 revision"은 `TTAKKARI_RELEASE_REVISION` 값 차이이며 다른 코드 빌드가 아니다. 두 revision 모두 같은 소스 트리를 실행한다. immutable release 디렉터리 전환은 검증하지 않았다.
- 실패 기동 유형은 중복 소유자, pinned revision 없음, 접속 불가 DB 세 가지다. register 이후 단계에서 죽는 기동 실패는 다루지 않았다. register·claim 후 SIGKILL은 위 6에서 다뤘다.
- API 쪽 revision 교체(Core 배포)와 요청 전환, Caddy, 운영 프로세스·DB·포트(8791~8793), launchctl, backup restore는 다루지 않았다. 운영 검증이 아니다.
- 이 여정은 FND1, P5, P6, 전체 gate의 대체 증거가 아니다. main push와 배포 예약은 하지 않았다.
