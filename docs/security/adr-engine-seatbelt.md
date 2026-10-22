# ADR: 엔진 프로세스 OS 수준 격리(Seatbelt 래퍼) v1

- 상태: 구현(자격 증명 경로·배포 런타임·소켓 예외 후속 보완, 기능 플래그 기본 꺼짐). 후속 버전의 커널 정책·Claude 응답은 중첩 샌드박스 때문에 미검증. 코드 배포와 기능 활성화는 구분한다.
- 날짜: 2026-10-19
- 관련: `docs/security/threat-model.md` B5(에이전트 → 호스트 OS), `/tmp/ttk-probe-report.md`(guard 우회 16건)
- 코드: `src/ttakkari/security/seatbelt.py`, `src/ttakkari/runs/manager.py`(`_attempt`), `src/ttakkari/config.py`(`engine_seatbelt`)

## 배경

`security/guard.py`는 명령 문자열을 정규식과 verb 목록으로 판정한다. probe 보고서에서 `bash -c`, `eval`, Python `os.system`·`open()`, `xargs`, 간접 변수, 파일 입력으로 16건이 통과했다. 블록리스트는 셸·인터프리터 언어 전체를 닫을 수 없다. DB와 비밀 파일 접근 권한 자체를 프로세스에서 빼야 한다.

threat-model에는 S-9(Seatbelt) 항목이 없다. B5 행은 Codex `workspace-write`와 Antigravity `--sandbox`(의미 미검증)만 적고 있다.

## 현황(2026-10-19 실측과 코드)

| 엔진 | 현재 격리 | PG TCP 5432 | PG 소켓 | 비밀 파일 읽기 | 근거 |
|---|---|---|---|---|---|
| claude | 없음. PreToolUse guard 훅만 | 열림 | 열림 | guard만(우회 가능) | 이 Run(claude, 샌드박스 밖)에서 `pg_isready` 두 경로 모두 응답. `agents/claude.py` |
| codex | 자체 Seatbelt `workspace-write`, `network_access=true` | **열림** | **열림** | 열림(전체 디스크 읽기) | `codex sandbox -c sandbox_mode=workspace-write -c sandbox_workspace_write.network_access=true`로 실측. 쓰기는 작업 폴더와 /tmp 밖에서 EPERM |
| codex (`network_access=false`) | 같음 | 거부 | 거부 | 열림 | 같은 명령으로 실측. 과거 기록의 "소켓 Operation not permitted"는 이 설정이었던 것으로 추정 |
| antigravity | `--sandbox`("terminal restrictions"), 바이너리에 SBPL 내장 | 미검증 | 미검증 | 미검증 | `agy --help`, `strings`. 실제 Run 실측 안 함 |
| muse | 네이티브 샌드박스 필수 + `muse_hook.py` | 미검증 | 미검증 | 미검증 | `.muse-runtime/contract.mjs:4`가 `sandboxAvailability === 'available'`을 강제 |
| qwen-local | 하네스 Seatbelt(`qwen_local/sandbox.py`) | **열림** | 거부 | 거부(키체인 포함) | 하네스 `build_profile()`로 만든 프로파일을 sandbox-exec로 실측. localhost 전체 허용 후 백엔드 포트(8787·8791-8793)만 막는다 |

## 선택지

1. **guard 블록리스트 보강.** 우회가 원리적으로 닫히지 않는다. 기각(보조 계층으로만 유지).
2. **엔진별 자체 샌드박스 옵션.** codex는 `network_access=false`로 DB를 막지만 npm·pip·git push도 함께 끊긴다. codex에는 실험 기능 `network_proxy`(allowed_domains, allow_unix_sockets, allow_local_binding)가 바이너리 문자열로 보이지만 스키마와 동작은 미검증이다. claude는 Bash 도구 전용 샌드박스 설정이 있으나 Read 도구와 MCP 자식 프로세스를 덮지 못하고 Ttakkari는 쓰지 않는다.
3. **엔진 프로세스 전체를 `sandbox-exec`로 감싸기.** 엔진과 모든 자식(Bash, Python, MCP 서버, 훅)이 같은 커널 정책을 받는다. 단, 자체 Seatbelt를 거는 엔진은 중첩이 실패한다(codex 실측: `sandbox_apply: Operation not permitted`, rc=71).
4. **별도 OS 사용자로 엔진 실행.** 파일 권한으로 비밀을 가르고 PG `pg_hba`로 사용자를 거부할 수 있어 가장 견고하다. 계정 폴더 소유권, outbox 공유, 키체인 분리, launchd 변경이 필요하다. 이번 범위(재시작·launchctl 금지) 밖이다.

## 결정

- 선택지 3을 **claude에만** 적용할 수 있게 구현했다. 기능 플래그 `TTAKKARI_ENGINE_SEATBELT`(목록, 기본 `[]`)로 켜고, 기본값은 꺼짐이다.
- 자체 샌드박스가 있는 codex·antigravity·qwen-local·muse를 플래그에 넣으면 Run은 샌드박스 없이 뜨지 않고 `RuntimeError`로 실패한다(fail-closed).
- 이들 엔진은 각자의 샌드박스 설정을 고치는 후속 작업으로 다룬다(롤아웃 순서 참고).

### 프로파일 규칙 (`render_profile`)

SBPL은 나중 규칙이 이긴다. 순서가 곧 우선순위다.

1. `(allow default)`: 엔진 CLI가 쓰는 mach 서비스, 키체인 IPC, node 런타임을 하나씩 열지 않기 위해서다.
2. DB 네트워크 거부: `(remote ip "*:PORT")`로 5432·5433·3306·6379·27017. 원격 호스트도 포함한다.
3. DB 유닉스 소켓 거부: `.s.PGSQL.N`, `mysqld.sock`, `redis*.sock`, `mongodb-N.sock`.
4. 소켓 예외: 명시한 `workspace/.ttakkari-db-sockets` 아래만 허용한다. 일반 `write_roots`는 소켓 허용 근거가 아니다. manager는 쓰기 루트에 `/tmp`와 시스템 임시 디렉터리를 항상 넣으므로 이를 재사용하면 운영 PG 소켓이 다시 열린다(코드 확인). `workspace`가 없으면 소켓 예외도 없다. 전용 디렉터리의 심링크 대상이 workspace 밖이면 프로파일 생성을 거부한다.
5. ttakkari 홈(`settings.home`) 읽기·쓰기 기본 거부. 내부에서 다시 여는 디렉터리는 이 Run의 `data/outbox/<run>`과 write_roots로 요청된 `data/uploads`, `inbox` 하위뿐이다. `write_roots`가 `accounts`, 다른 계정, `data`, `backups`, `config.env`를 포함해도 열지 않는다. 계정 예외는 **claude 엔진의 `CLAUDE_CONFIG_DIR` 한 곳**만 별도로 추가한다. `CODEX_HOME`, `XDG_*_HOME`은 재허용의 근거로 사용하지 않는다. 가드 로그는 `data/guard.log` 낱개 파일(`literal`)만 다시 연다. 넓은 홈·ttakkari·accounts 루트나 다른 자격 증명 저장소를 Claude 설정 폴더로 주면 거부한다.
6. 현재 코드가 `settings.home/deploy/releases/<40자리 SHA>`에 있으면 그 릴리스만 읽기 허용한다. Claude 가드 훅(`agents/claude.py`)과 stdio MCP(`orchestrator/runtime.py`)는 백엔드의 `sys.executable`로 실행되므로 배포 릴리스의 모듈·venv 읽기가 필요하다(코드 확인, 후속 커널 실측 미검증). 예외는 `__file__`에서 도출하며 환경 변수나 쓰기 루트에서 받지 않는다. ttakkari 내부에서 릴리스 형식이 아닌 런타임은 거부한다. 다른 릴리스·계정·설정은 열지 않으며 현재 릴리스 쓰기도 허용하지 않는다.
7. 비밀 경로 읽기·쓰기 거부. 위 예외보다 뒤라 항상 이긴다. 디렉터리 전체: `~/.ssh`, `.aws`, `.gnupg`, `.kube`, `.config/gh`, `.config/gcloud`, `.azure`, `.password-store`, **`.gemini`, `.codex`, `.docker`**. 낱개 파일: `.netrc`, `.pgpass`, `.git-credentials`, `.npmrc`, `.pypirc`, `.my.cnf`. 모든 경로의 `.env`·`.env.*`도 거부한다. 기본 Claude 계정일 때만 `~/.claude/.credentials.json`을 열어 둔다(`CLAUDE_CONFIG_DIR=~/.claude` 명시도 동일). 별도 계정이면 기본 계정의 이 파일은 닫는다. 비밀 경로가 심링크면 설정 당시의 실제 대상 경로도 거부 목록에 넣는다.
8. `.env.example|sample|template|dist`만 재허용한다. `require-all`/`require-not`으로 ttakkari 홈과 자격 증명 디렉터리 내부, 심링크로 옮긴 낱개 자격 증명 파일을 이 예외에서 제외한다.
9. 키체인 파일 거부는 `TTAKKARI_ENGINE_SEATBELT_DENY_KEYCHAIN`(기본 꺼짐)으로 따로 둔다.

`file-read-data`만 막고 `file-read-metadata`(stat)는 열어 둔다. 존재 확인으로 프로그램이 깨지는 일을 줄이기 위해서다. 경로에 `"`, `\`, 개행, NUL이 있거나 상대 경로면 프로파일 생성을 거부한다. 프로파일은 `sandbox-exec -p`로 넘겨 임시 파일 정리를 없앴다. 경로만 담기므로 argv 노출에 비밀이 없다.

### 홈 dotfile 기본 거부 검토(2026-10-19 후속)

이번에는 **저장소 단위 거부 목록 + ttakkari 내부 기본 거부/제한된 허용 목록**을 선택한다. `oauth_creds.json` 하나를 추가하는 대신 `.gemini` 전체를, `auth.json`만 막던 Codex와 `config.json`만 막던 Docker도 디렉터리 전체를 닫는다. 파일명 변경·백업·추가 토큰 파일도 같은 디렉터리 안이면 포함된다.

홈 dotfile 전체 기본 거부는 새 저장소 누락을 줄이는 장점이 있다. 다만 `.local`의 실행 파일, `.cache`, `.config`, `.claude`의 공용 스킬·플러그인·훅까지 필요한 예외를 도출해야 한다. 운영 계정의 인증·응답을 확인하지 못한 상태에서 그 모델로 바꾸면 동작 유지를 보증할 수 없다. 이 후속 작업의 Codex 중첩 작업 샌드박스에서는 최소 `sandbox-exec`도 rc=71로 거부됐다. 따라서 이 변경에서 광범위한 전환은 보류하고, 기본 꺼짐을 유지한다. 이는 현재 거부 목록이 완전하다는 판단이 아니다.

추후 독립 검증 환경에서 dotfile 기본 거부 후보를 실측할 때는 `CLAUDE_CONFIG_DIR` 한 계정, 실제 실행 파일·공용 스킬 경로, 산출물·첨부·가드 로그부터 허용하고 인증 → 모델 응답 → 대화 재개 → MCP 도구 실행을 따로 검증한다. 키체인 IPC와 엔진 자기 계정 토큰 노출은 그 모델에서도 별도 문제다.

## 최초 버전의 과거 검증(2026-10-19, Mac Studio, claude 엔진 Run 안, 샌드박스 밖)

아래는 선행 작업의 기록이다. 후속 변경된 프로파일의 커널 적용·Claude 응답 검증으로 재사용하지 않는다.

- `pytest --noconftest tests/test_seatbelt.py`: 16 passed. 커널 실측 테스트 1건 포함(미끼 파일, 임시 소켓, 임시 TCP 포트).
- 실제 홈 경로 대상 프로파일 아래 probe: PG TCP와 소켓, `~/.ssh` 키 열기와 목록, gh·docker 인증, `~/.claude/.credentials.json`, ttakkari `config.env`, 백엔드 `.env`, 워크스페이스 `.env` 모두 EPERM. 백엔드 API 8787, 일반 파일 읽기·쓰기는 허용.
- 우회 시도: 심링크 경유 읽기, 하드링크 생성, 대소문자(`.ENV`, `.SSH`), `cp`, IPv6 `::1:5432`, `psql`·`pg_isready` 모두 거부.
- 실제 claude CLI(haiku)를 `ClaudeAdapter.build_command` + 가드 훅 + 래퍼로 실행: 인증과 응답 정상, Bash·git commit·Edit 정상, Read 도구의 `.env` 읽기는 EPERM. probe 보고서 우회 패턴인 `bash -c 'python3 -c "socket.connect((127.0.0.1, 5432))"'`는 가드를 통과했지만 커널이 EPERM으로 거부했다.
- 엔진 자식인 Ttakkari MCP 서버: 샌드박스 안에서 기동하고 `initialize`에 응답. 가드 훅도 정상 판정.
- 샌드박스 안에서 `pytest --noconftest`(seatbelt, broker_policy, browser_preflight): 70 passed, 1 skipped(중첩 시 미끼 생성 불가로 스킵).

## 후속 검증(2026-10-19, Codex 중첩 작업 샌드박스)

내용을 열지 않고 경로 존재·이름만 확인했다. `.gemini`, `.codex`, `.config/gh`, `.docker/config.json`, `.claude`, `.ssh`, `.ttakkari`가 존재했다. 현재 기본 `.gemini/oauth_creds.json`, `.codex/auth.json`은 목록에 없지만 계정별 `~/.ttakkari/accounts/<id>/auth.json`은 존재한다. `.aws`, `.kube`, `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`, `.config/gcloud`, `.azure`, `.gnupg`는 없었다. 존재하지 않아도 거부 규칙은 항상 생성한다.

```
$ /usr/bin/sandbox-exec -p '(version 1)(allow default)' /usr/bin/true
sandbox-exec: sandbox_apply: Operation not permitted
exit code: 71
```

실패 단계는 최소 정책의 커널 적용이다. 경로 열기 및 Claude 실행 단계에 도달하지 못했다. 이 결과는 해당 Codex 작업 환경에 한정되며 호스트나 운영 Claude의 실패를 뜻하지 않는다. 알려진 거부를 다시 시도하거나 우회하지 않았다.

```
$ TTAKKARI_TEST_SEATBELT_UNAVAILABLE='Codex nested worker sandbox: sandbox-exec minimal preflight rc=71; sandbox_apply: Operation not permitted' .venv/bin/python -m pytest --noconftest tests/test_seatbelt.py -q -rs
SKIPPED [1] tests/test_seatbelt.py:148: Codex nested worker sandbox: sandbox-exec minimal preflight rc=71; sandbox_apply: Operation not permitted
39 passed, 1 skipped in 0.09s
$ .venv/bin/ruff check src/ttakkari/security/seatbelt.py src/ttakkari/config.py src/ttakkari/runs/manager.py tests/test_seatbelt.py
All checks passed!
```

커널 테스트에는 추가 자격 증명 경로 21개 미끼, 다른 계정/설정을 열려는 write_roots·CODEX_HOME·XDG 예외, 현재 Claude 계정 허용을 넣었다. 읽기는 `os.open(O_RDONLY)` 후 즉시 닫으며 내용을 읽거나 출력하지 않는다. 거부 판정은 단순 파일 없음이 아닌 errno=EPERM을 요구한다. 위 실행에서는 커널 테스트를 명시적 사유로 스킵했다. 후속 프로파일 컴파일·EPERM 실측, Claude 인증·모델 응답, 대화 재개, MCP 도구 실행은 모두 미검증이다.

### 기획 세션 직접 검토 후 보완(2026-10-19)

운영 manager 호출 경로까지 읽고 두 항목을 추가 수정했다. 이전 버전의 `socket_roots=list(write_roots)`가 `/tmp`까지 재허용하는 문제와, `settings.home` 기본 거부가 배포 릴리스의 훅·MCP Python 모듈도 닫는 문제다. 각각 위 규칙 4·6으로 제한했다. 수정 전 회귀 테스트는 소켓 관련 3건과 런타임 관련 2건에서 실패했으며 수정 후 통과했다.

```
$ TTAKKARI_TEST_SEATBELT_UNAVAILABLE='<알려진 Codex 중첩 sandbox_apply 거부; 재시도 안 함>' .venv/bin/python -m pytest --noconftest tests/test_seatbelt.py tests/test_guard.py tests/test_broker_policy.py -q -p no:cacheprovider
197 passed, 1 skipped, 2 xfailed
$ .venv/bin/ruff check src/ttakkari/security/seatbelt.py src/ttakkari/config.py src/ttakkari/runs/manager.py tests/test_seatbelt.py
All checks passed!
```

커널 테스트에는 자격 증명 및 다른 릴리스 22개 미끼, 현재 릴리스 읽기 허용·쓰기 거부, 넓은 일반 쓰기 루트로 운영 소켓을 다시 열지 못하는 경우를 포함했다. 이 테스트는 위 환경에서 스킵했으므로 후속 정책 컴파일·EPERM·실제 모델 응답·재개·MCP 실행은 여전히 미검증이다. 기본 꺼짐 상태의 코드 배포는 기능 활성화 검증을 대체하지 않는다.

## 남은 위험

- **기본 허용 모델.** 열거하지 않은 홈 dotfile, 프로젝트별 `credentials.json`, ttakkari 밖의 브라우저 프로필, 환경 변수로 옮긴 다른 이름의 kubeconfig·클라우드 자격 증명은 열려 있을 수 있다. 기본 거부 모델로 가려면 엔진별 허용 목록을 실측으로 쌓아야 한다. 심링크 대상 거부는 프로파일 생성 시점 기준이다.
- **키체인 IPC.** 파일 거부를 켜도 `security find-generic-password`는 securityd IPC로 간다. claude 인증이 키체인에 의존할 수 있어 기본으로 막지 않았다. 효과와 부작용은 미검증이다.
- **엔진 자신의 인증 파일.** 엔진이 자기 계정 폴더를 읽어야 하므로 그 안의 토큰은 에이전트도 읽을 수 있다.
- **DB 포트 목록 밖.** 비표준 포트나 원격 DB는 포트로 막히지 않는다. SSH 키와 `.pgpass`가 닫혀 있어 피해 범위는 줄어든다.
- **HTTP 경로.** 8787 백엔드 API는 열려 있다. 권한은 Run에 묶인 MCP 토큰(SEC2-AGT-02)에 기댄다.
- **`.env` 쓰기 거부.** 작업 세션이 앱용 `.env`를 만들거나, 테스트가 `.env` 미끼를 만들면 실패한다(테스트 1건에서 확인). 롤아웃 1단계에서 관찰한다.
- **테스트 DB.** 현재 테스트는 같은 클러스터의 `ttakkari_test`(5432)를 쓴다. 래퍼를 켜면 작업 세션에서 DB 기반 pytest가 막힌다. 워크스페이스 임시 클러스터 도입 전까지 감수한다.
- **`sandbox-exec` 폐기 예고.** Apple은 사용 중단(deprecated)으로 표기한다. macOS 27.0(26A428)에서는 동작을 확인했다.
- **codex·qwen-local은 아직 열려 있다.** 위 현황표 그대로다.

## Run별 통합 테스트 DB (2026-10-22)

- 백엔드가 각 엔진 프로세스 시도 직전에 독립 임시 PostgreSQL 클러스터를 만든다. 클러스터는 임의 loopback 포트에만 바인딩하고, Run 전용 계정·무작위 비밀번호와 `ttakkari_test` DB를 만든다. 엔진에는 이 URL을 `TTAKKARI_TEST_DB`로만 넘긴다. 앱의 `TTAKKARI_DATABASE_URL`은 생성이나 전달에 사용하지 않는다.
- Run마다 별도 클러스터와 계정이므로 테스트 데이터·superuser 권한·포트가 다른 Run과 공유되지 않는다. 공유 PostgreSQL 서버의 임시 DB를 택하지 않은 이유는 연결 권한·실패 시 잔재가 운영 클러스터로 번지지 않게 하기 위해서다.
- 프로세스 종료·시작 실패·취소 때 backend가 `pg_ctl`로 멈추고 클러스터 폴더를 삭제한다. 백엔드 기동과 다음 Run DB 준비 때는 고아 마커·owner PID·postmaster PID를 확인해 정리한다. SIGKILL로 SysV 공유메모리가 남았으면 `ipcs`의 key, creator PID, owner, attachment 수가 postmaster 기록과 일치할 때만 제거한다. 증거가 불완전하면 다른 IPC 세그먼트를 지우지 않고 정리 실패를 로그에 남긴다.
- Codex의 `sandbox_mode="workspace-write"`, `network_access=true`, 그리고 claude만 감싸는 기존 Seatbelt 설정은 바꾸지 않는다. Claude 프로파일은 기본 DB 포트와 PostgreSQL 소켓을 막지만, 이 클러스터는 임의 포트를 사용해 Run 전용 DB에 접속한다. 중첩 Seatbelt는 사용하지 않는다.
- 통합 테스트에서 명시한 loopback 포트 URL도 migration 테스트의 직접 `asyncpg.connect(host="localhost")`에 적용한다. 운영 DB 이름 `ttakkari`는 기존 conftest 가드가 계속 거부한다.

## 롤아웃 순서

1. claude: 먼저 중첩 샌드박스 밖의 허용된 검증 환경에서 후속 프로파일 컴파일·22개 미끼 EPERM·현재 배포 릴리스 읽기/쓰기 경계·실제 계정 CLI 응답을 확인한다. 인증/모델 응답/대화 재개/MCP 도구는 각각 확인한다. 그 뒤 운영에서 `TTAKKARI_ENGINE_SEATBELT='["claude"]'`를 검토한다. 초기 구현 작업자의 커밋·푸시·배포 금지는 작업자에게 지정된 단계 제한이었으며 사용자의 영구 정책이 아니다. 검증된 코드는 기본 꺼짐을 유지해 커밋·푸시·배포 예약하고, 활성화는 위 실측 뒤에 진행한다. 적용 후 실패율, `Operation not permitted` 로그, 가드 로그를 1주 관찰한다.
2. codex: 래퍼 대신 자체 설정을 쓴다. `network_proxy`로 DB 포트·소켓을 거부하고 일반 외부망만 허용할 수 있는지 먼저 실측한다. 안 되면 작업 성격에 따라 `network_access=false`를 고른다. 읽기 제한은 codex permission profile의 filesystem deny 지원 여부를 확인한다(미검증).
3. qwen-local: 하네스 `DEFAULT_BLOCKED_LOOPBACK_PORTS`에 DB 포트를 더한다(별도 저장소).
4. antigravity: `--sandbox`의 실제 정책을 실측한다. 부족하면 래퍼 중첩 가능성부터 확인한다.
5. muse: 네이티브 샌드박스 정책을 실측한 뒤 판단한다.
6. 그다음 기본 거부 모델 또는 별도 OS 사용자(선택지 4)를 검토한다.

## 되돌리기

`TTAKKARI_ENGINE_SEATBELT`를 비우면 `maybe_wrap`이 명령을 그대로 돌려준다(단위 테스트 `test_flag_off_leaves_command_untouched`).
