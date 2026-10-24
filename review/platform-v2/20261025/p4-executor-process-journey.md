# P4 실행부 독립 프로세스 여정 실증

- 일시: 2026-10-25
- 베이스: `c2657bc` (integration/platform-v2-execution-all-20261025)
- 이전 시도 철회: c2657bc의 `tests/test_executor_process_journey.py`와 이 보고서 이전판은 `python -c` 인라인 가짜 파일큐로 Core/Executor를 흉내냈고 ttakkari 코드를 import하지 않았다. 제품 증거가 아니므로 둘 다 이 커밋에서 실제 구현·보고서로 교체했다.
- 제품 src 수정: 없음. 실패 테스트(red)를 먼저 보일 제품 결함이 나오지 않았다.

## 1. 실증한 것

실제 entrypoint를 pytest의 own child로 임시 loopback 포트에 띄웠다.

| 역할 | 명령 | 근거 |
|---|---|---|
| Core API | `python -m ttakkari.cli serve --host 127.0.0.1 --port <임시>` | `tests/test_executor_process_journey.py` `core_process()` |
| 실행부 | `python -m ttakkari.cli executor --activate` | 같은 파일 `start_executor()` |

- 작업 유형: Run. 엔진은 conftest가 이미 주입하는 `tests/fake_agents/fake_claude.py`(`TTAKKARI_CLAUDE_BIN`)다. 이 fake는 기존 테스트 인프라이고 이번에 새로 만든 가짜가 아니다.
- DB: 주입된 `TTAKKARI_TEST_DB`(127.0.0.1, `ttakkari_test`). 자식 env에 URL을 그대로 상속해 계정·port·query를 유지한다. 비밀값은 출력하지 않았다.
- 검증 수단: 자식 `returncode`, `/api/ready` HTTP, DB 행(Run, RuntimeGeneration, RuntimeControl)뿐이다. psutil, ps 조회는 쓰지 않았다.

| 요구 | 테스트 | 확인한 내용 |
|---|---|---|
| 1 접수 | `test_executor_replacement_keeps_core_and_accepted_work` | 실행부가 없는 동안 `POST /api/sessions/{id}/runs`가 202. DB `Run.status=queued`, `pid`와 `exit_code`는 None. 1.5초간 DB와 API 모두 queued 유지 |
| 2 Core 무배포 | 같은 테스트 | 구 실행부 SIGTERM(exit 0), 접수, 새 revision(`f`*40) 기동 구간 내내 Core child `returncode is None`, 같은 PID, `/api/ready` 200, 같은 generation id, DB `role=api, state=active, pid=Core PID` |
| 3 유실 없음 | 같은 테스트 | 새 실행부가 DB에 `role=executor, revision=f*40, pid=child PID`로 등록. 접수 작업이 `succeeded, exit_code=0`. 구 실행부 행은 `offline`, `RuntimeControl.executor_generation`은 새 행 |
| 4 실패 격리·rollback | `test_failed_executor_start_is_isolated_and_rollback_resumes_work` | ① 실행부가 살아 있을 때 새 revision 기동은 `already has a live owner`로 비정상 종료. 기존 실행부 `returncode None`, DB `active` 유지, 이후 작업 정상 완료 ② 실행부를 내리고 작업 접수 후, pinned revision 없는 설정과 접속 불가 DB(port=1)의 새 실행부는 각각 비정상 종료. Core 무영향, 작업은 queued 유지, 실패 revision `f*40`의 등록 행 없음 ③ 이전 정상 revision(`e`*40)으로 rollback하면 접수 작업이 완료되고 rollback 이후 새 작업도 완료 |
| 보조 | `test_child_commands_are_real_ttakkari_entrypoints` | 자식 `PYTHONPATH` 첫 항목이 pytest가 import한 같은 소스 트리 `src`이고 DB가 loopback `ttakkari_test`임을 고정 |

## 2. 코드로 본 실제 모듈 실행 (grep)

```
$ grep -n 'ttakkari.cli\|PYTHONPATH\|"serve"\|"executor"' tests/test_executor_process_journey.py
36:    path = os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH", "")]))
37:    return os.environ | {"PYTHONPATH": path, ...
64:            sys.executable, "-m", "ttakkari.cli", "serve", "--host", "127.0.0.1", "--port", str(port),
89:            sys.executable, "-m", "ttakkari.cli", "executor", "--activate",
101:                assert row.role == "executor" and row.revision == revision
```

`python -c`나 가짜 파일큐 스크립트는 없다. 행 101은 자식이 제품 코드(`ExecutorRuntime.register`)로 DB에 남긴 등록 행을 확인한다.

## 3. 실행 명령과 결과

공유 venv의 editable 설치(`.venv/.../ttakkari.pth`)가 다른 체크아웃 `local-llm-bench-spec-4-1-20261023/src`를 가리킨다. 그대로 실행하면 기존 `test_run_executor.py`조차 import 오류(`cannot import name 'drain'`)가 난다. 따라서 모든 명령에 `PYTHONPATH=<worktree>/src`가 필요하다. 신규 테스트의 자식은 이 값을 스스로 고정한다. 환경 문제이며 이번 범위에서 고치지 않았다.

```
$ export PYTHONPATH=$PWD/src
$ python -m pytest tests/test_executor_process_journey.py -v -p no:cacheprovider
3 passed in 10.07s            (3회 반복 3 passed, 9.8~10.1s)

$ python -m pytest tests/test_run_executor.py tests/test_run_executor_processes.py tests/test_pg_sandbox.py \
    tests/test_executor_process_journey.py tests/test_generation_processes.py tests/test_generations.py \
    tests/test_tts_executor_isolation.py -q -p no:cacheprovider
97 passed in 31.22s

$ ruff check tests/test_executor_process_journey.py   -> All checks passed!
$ git diff --check                                    -> 출력 없음
```

`ruff check src tests unit_tests`는 14건, 저장소 전체는 99건을 낸다. 모두 이번에 건드리지 않은 파일(experiments, unit_tests/test_backup_restore.py 등)의 기존 항목이고 신규 파일은 0건이다.

## 4. 변이 검증 (테스트가 실패할 수 있는지)

| 변이 | 결과 |
|---|---|
| 구 실행부를 내리지 않음 | `assert 'succeeded' == 'queued'`로 실패. 접수 후 queued 유지 단언이 실제로 의미를 가짐 |
| 실행부 교체 중 Core를 종료 | `assert (-15 is None)`로 실패. Core 무배포 단언이 실제로 의미를 가짐 |
| Core를 `local` 모드로 변경 | 실행부가 `executor activation requires remote APIs`로 거절해 실패(제품의 기존 가드). 의도한 단언은 아니고 참고용 |

변이용 임시 파일은 삭제했고 커밋에 포함하지 않았다.

## 5. 실증하지 않은 것

- 실제 모델 응답, MCP, 외부 네트워크: 엔진은 fake_claude다.
- 실행 중(running) Run이 있는 상태에서의 교체, 장기 Run, drain 대기, claim 경합. 이번 교체는 모두 실행부가 idle일 때다. 기존 `test_run_executor*.py`가 일부 다루지만 이 여정의 증거는 아니다.
- 접수 작업 유형은 Run 하나다. STT, TTS, Creative, Games, NotebookLM 등 다른 큐는 실증하지 않았다.
- "새 revision"은 `TTAKKARI_RELEASE_REVISION` 값 차이이며 다른 코드 빌드가 아니다. 두 revision 모두 같은 소스 트리를 실행한다. immutable release 디렉터리 전환은 검증하지 않았다.
- 실패 기동 유형은 중복 소유자, pinned revision 없음, 접속 불가 DB 세 가지다. register 이후 단계에서 죽는 기동 실패, 크래시 후 orphan lease 회수는 다루지 않았다.
- API 쪽 revision 교체(Core 배포)와 요청 전환, Caddy, 운영 프로세스·DB·포트(8791~8793), launchctl, backup restore는 다루지 않았다. 운영 검증이 아니다.
- 이 여정은 FND1, P5, P6, 전체 gate의 대체 증거가 아니다. main push와 배포 예약은 하지 않았다.
