# 3차 (리드 결정 3건, 커밋 263a162 이후)

## 1. 계정 pool 엄격 분리
- 앱의 personal 모드는 work 계정까지 후보로 나열한다. L2 어댑터는 `subscription_transport.restrict_to_pool` 로
  `availability()` 와 호출 시 후보/대기 목록을 문서의 target pool(`profile.pool == target_pool`)로 다시 거른다.
  personal 원천은 pool=personal 계정만, work 원천은 pool=work 계정만 쓴다.
- 반례 테스트 `test_personal_document_never_uses_a_work_pool_account`: work 계정만 있을 때 personal 문서는
  availability False, 호출은 `ScopeCapacityUnavailableError(consumed=False)`, fake CLI 호출 0회. personal 계정을 추가하면
  그 계정(CODEX_HOME)으로만 나가고, work 문서는 work 계정으로만 나간다.
- RED: `red_third.stdout.txt` (base `red_third.base_sha`=263a162 에 새 테스트만 복사) -> `3 failed, 11 deselected`(pool, stdin 2건). GREEN: `green_third.stdout.txt`.

## 2. 프롬프트 stdin 전달
- 지원 근거: `cli_stdin_help.txt`. codex 0.162.1 `codex exec [PROMPT]` 는 "If not provided as an argument (or if `-` is used),
  instructions are read from stdin". claude 2.1.285 는 `-p/--print` "useful for pipes", `--input-format text(default)`.
- 구현: `desk._ask(..., prompt_via_stdin=False)` 새 인자. True 면 `_codex_cmd` 는 끝에 `-`, `_claude_cmd` 는 `-p` 뒤 본문 없이
  만들고 stdin=PIPE 로 `communicate(input)` 한다. 기본값(False)은 기존 동작과 같다. L2 transport 만 True.
  시스템 프롬프트는 고정 상수라 argv 에 남는다. 임시파일 방식은 stdin 이 되므로 쓰지 않았다(디스크에 본문을 남기지 않음).
- 테스트: fake codex 가 argv 마지막 인자 `-` 이면 stdin 을 읽고, 본문 마커가 argv 에 없고 stdin 에만 있음을 확인.
  기존 argv 경로는 `desk._codex_cmd/_claude_cmd` 단위 테스트로 불변을 확인.
- 실제 CLI 1회(claude, 합성 passage 1개): `smoke_stdin_claude.stdout.txt` 성공, 인용 10/10 정확 부분문자열.
  실행 중 `ps -axo args` 를 0.5초 간격으로 샘플링해 claude 프로세스가 보인 27회 모두에서 본문 구절이 없었다(`smoke_stdin_claude.ps_probe.txt`).
  통제 실험(argv 방식에서 보임)은 실행하지 않았다. argv 방식에서 본문이 argv 에 들어가는 것은 코드상 사실이다.
- codex 실호출 stdin 은 검증 못함: 호스트 기본 `~/.codex` 에 로그인이 없어 401(2차 기록 참고). 관리 계정 경로는 보호 영역.

## 3. 합산 실행 test_desk 48 errors 조사
- 재현 못함. 동일 5개 파일 합산 실행 3회 모두 `51 passed`.
- 새로 찾은 사실 A: 이 호스트의 pytest 는 `TTAKKARI_TEST_DB`(127.0.0.1:65499/ttakkari_test)가 환경에 지정돼 있어
  공유 DB 를 쓰고, `tests/conftest.py::_schema_and_app` 이 세션 시작에 `drop_all/create_all` 한다. 같은 파일을 동시에 두 프로세스로
  돌리면 `deadlock detected`, `KeyError: 'status'` 등 18~17개 실패가 난다(재현 명령: 같은 pytest 명령을 백그라운드와 포그라운드로 동시 실행).
  OSError 는 이 방식으로도 나오지 않았다. 따라서 48 errors 는 같은 시각의 다른 pytest 실행(공유 DB 스키마 재생성)이었을 가능성이
  가장 크지만 **추정이며 확정하지 못했다**.
- 새로 찾은 사실 B(별건, 실제 결함): 세션 앱의 lifespan 이 `consumer:kb-pipeline` 루프를 띄우고, 이 기본 워커가 등록된 잡을
  claim 하면서 `attempt_token` 을 바꿔 직접 호출 테스트가 `StaleWriterError` 로 간헐 실패했다(합산 14회 중 3회).
  기존 `test_kb_v3_integration_flow.py` 처럼 기본 워커 `pause_claiming()` 하는 autouse fixture 를 추가한 뒤 같은 합산 실행
  24회 모두 `37 passed`. 이 flake 는 테스트 수정만으로 해결했고 앱 코드는 바꾸지 않았다.
