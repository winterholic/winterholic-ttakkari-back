# winterholic-ttakkari-back

Winterholic Ttakkari 백엔드. Mac Studio 에서 Claude Code·Codex 에이전트를 원격으로 실행하고, 실행 로그와 결과물(Artifact)을 PWA 로 보내는 FastAPI 서버다.

- 프론트: [winterholic-ttakkari-front](https://github.com/winterholic/winterholic-ttakkari-front)
- API 계약: [`docs/api-contract.md`](docs/api-contract.md)
- 설계 결정: [`docs/adr/`](docs/adr/)

## 요구 사항

- Python 3.13, [uv](https://docs.astral.sh/uv/)
- PostgreSQL 17 (Homebrew `postgresql@17`)
- 로그인된 `claude` CLI, `codex` CLI
- 선택: LibreOffice (PPTX·DOCX 미리보기 변환)
- macOS Codex 화면 검증: Node.js 18+·설치된 Google Chrome·Playwright MCP 런타임(아래)

## 처음 설정

```bash
uv sync
createdb ttakkari
uv run ttakkari init-config      # ~/.ttakkari/config.env 생성(JWT 비밀키 포함, 0600)
uv run ttakkari migrate
uv run ttakkari set-password     # 12자 이상
uv run ttakkari serve            # http://127.0.0.1:8787
```

설정은 `~/.ttakkari/config.env` 에 `TTAKKARI_*` 로 둔다. 항목 전체는 [`.env.example`](.env.example). 자주 바꾸는 값:

| 변수 | 의미 |
|---|---|
| `TTAKKARI_ALLOWED_ROOTS` | 워크스페이스로 등록할 수 있는 상위 경로 JSON 배열. 기본 `~/development`, `~/docs`, `~/personal`, `~/private-resources`, `~/testing`, `~/.claude`, `~/.codex`. 워크스페이스의 `extra_writable_roots` 도 이 안쪽만 허용 |
| `TTAKKARI_DEFAULT_WORKSPACE_ROOT` | 서버가 자동으로 만드는 기본 워크스페이스 루트(에이전트 실행 위치). 기본 `~/development` |
| `TTAKKARI_DEFAULT_WRITABLE_ROOTS` | 기본 워크스페이스를 처음 만들 때 넣는 추가 쓰기 경로 JSON 배열. 없는 폴더는 건너뛴다. 기본 `~/docs`, `~/personal`, `~/private-resources`, `~/testing`, `~/.claude`, `~/.codex`(하네스 편집용. 로그인 토큰은 guard 가 막는다) |
| `TTAKKARI_ENGINE_PROFILES` | 엔진 프로필 JSON 배열(순서 = 우선순위). `{"name","engine","config_dir"?}`. 기본 claude → codex. 오케스트레이터·작업 세션(`auto`)은 사용량 한도면 다음 프로필로 넘어간다(ADR 0006) |
| `TTAKKARI_PROFILE_COOLDOWN_SECONDS` | 한도에 걸린 프로필을 쉬게 하는 시간. 기본 1800 |
| `TTAKKARI_INTERNAL_API_URL` | 오케스트레이터 MCP 서버가 부르는 백엔드 주소. 기본 `http://127.0.0.1:8787` |
| `TTAKKARI_TRANSFER_INBOX` | 폰에서 보낸 파일 기본 폴더. 비우면 `~/.ttakkari/inbox`(에이전트가 읽고 쓸 수 있는 ~/.ttakkari 안 유일한 곳) |
| `TTAKKARI_MEETING_TRANSCRIPT_MAX_CHARS`, `TTAKKARI_MEETING_ANALYSIS_MODEL`, `TTAKKARI_MEETING_ANALYSIS_EFFORT` | 회의록(ADR 0010): 전사 한 건 최대 글자(기본 12만), 분석 Run 의 모델·생각 수준(기본 세션 기본 모델·high) |
| `TTAKKARI_CONTEXT_EMBEDDER` | 회의록 벡터 저장: `e5`(기본, 로컬 multilingual-e5-large) · `hash`(테스트) · `off`. E5도 요청별 자식 프로세스에서 로드·종료한다. 가중치는 사전 설치하며 요청 중 다운로드하지 않는다 |
| `TTAKKARI_CONTEXT_EMBEDDING_CPU_THREADS`, `TTAKKARI_CONTEXT_EMBEDDING_TIMEOUT_SECONDS` | E5 자식의 CPU 스레드(기본 2), 요청 제한시간(기본 300초) |
| `TTAKKARI_LOCAL_STT_ENABLED`, `TTAKKARI_LOCAL_STT_PYTHON`, `TTAKKARI_LOCAL_STT_MODEL_PATH`, `TTAKKARI_LOCAL_STT_PROFILE` | 기본 전사 엔진 로컬 Whisper(ADR 0025·0026). 기본 켜짐·robust(VAD·독립 25초 창·문맥 차단·온도 fallback·반복 재처리), 로컬 가중치 경로는 별도 지정. 구 baseline/improved 설정도 robust로 승격한다. 요청마다 자식 프로세스에서 로드·추론 후 종료한다. [설치·API·측정 안내](docs/stt/README.md). 미준비 시 사유를 알리며 외부 엔진으로 자동 전환하지 않는다 |
| `TTAKKARI_CONTEXT_SOURCE_ROOTS` | 비서 관리 > 지식에 등록할 수 있는 상위 폴더 JSON 배열. 기본 홈 |
| `TTAKKARI_ARTCRAFT_ROOT`, `TTAKKARI_ARTCRAFT_AUTO_REMOTE_CHECK` | 더보기 > ArtCraft 관제(ADR 0035)가 호출하는 고정 저장소(기본 `~/development/projects/artcraft`)와 최신 릴리스 6시간 자동 확인. 디자인시스템과 별개 도메인이다 |
| `TTAKKARI_CORS_ORIGINS` | 프론트 origin JSON 배열 |
| `TTAKKARI_ENV` | `prod` 면 refresh 쿠키가 `SameSite=None; Secure`, `/docs` 비활성 |
| `TTAKKARI_TRUST_PROXY_IP_HEADER` | Cloudflare Tunnel 뒤면 `CF-Connecting-IP` |
| `TTAKKARI_CLAUDE_SETTING_SOURCES` | 비우면 사용자 전역 설정(CLAUDE.md·스킬·hook) 포함. `project,local` 이면 제외 |
| `TTAKKARI_MAX_CONCURRENT_DESK`, `TTAKKARI_MAX_CONCURRENT_LEADS`, `TTAKKARI_MAX_CONCURRENT_WORKERS` | 접수·기획·작업/일반 대화별 동시 실행 수. 기본 각각 16개이며 역할별로 독립 대기한다 |
| `TTAKKARI_DESK_QWEN_SERVER_ENABLED`, `TTAKKARI_DESK_QWEN_SERVER_PATH`, `TTAKKARI_DESK_QWEN_MODEL_PATH` | 접수 요청 때 healthy Qwen 서버를 재사용하고 없으면 로컬 loopback에 llama-server 자식 프로세스를 띄운다(기동 제한 15초). 기본 실행 파일·모델은 `~/private-resources/service-data/ttakkari-qwen/`의 영구 배치 경로다. 세대별 포트와 프로세스 소유권을 분리하며 세대 종료 시 자신이 띄운 자식만 종료한다 |
| `TTAKKARI_RUN_TIMEOUT_SECONDS` | Run 타임아웃 |

**uvicorn 워커는 1개만 쓴다.** 실행 중 프로세스를 메모리에서 관리한다(ADR 0001).

## 맥락 도서관

지식 기반 재설계는 [실행 설계](docs/knowledge/architecture.md)와 [ADR 0031](docs/adr/0031-evidence-first-knowledge.md)에 따른다.
첫 단계의 `ttakkari context-inventory /path/to/export --expected-shards 6 --output /new/output/directory`는
ChatGPT 원본의 모든 분기·처리상태를 읽기 전용으로 기록한다. `--timeout` 기본 60초, 초과 시 unknown/incomplete와 오류 원장을 남기고 exit 2로 끝난다.
원본 밖 새 출력 폴더가 필요하며 추출·색인·DB 변경은 수행하지 않는다. 원본 처리와 의미 보존율은 별개다.

후속 근거 원장·추출·정정·검색 API/MCP와 도서관 `근거·검토` 화면은 [운영 절차](docs/knowledge/operations.md)를 따른다.
`knowledge-import-chatgpt`는 지정한 shard를 원장에 **쓰기**하며, `knowledge-backfill`은 등록 문서만 수집한다.
`knowledge-eval`은 원문 gold로 기존/원문/BM25/관계/하이브리드 경로를 비교한다. 구조 구현과 운영 이행·검색 개선 입증은 별개다.


별도 텍스트 임베딩 모델·다운로드·외부 API 없이 PostgreSQL 키워드·부분 일치 검색과 온톨로지를 사용한다.
`library_catalog`로 종류·프로젝트 목차를 보고, `recall`로 출처·시각이 붙은 기억과 문서 조각을 찾는다.
프로젝트 별칭은 정식 이름·연결된 기억·프로젝트 문서로 이어진다. 회사 세션은 회사·공통 맥락만 검색한다.
개인 세션과 비서 관리 화면은 전체 맥락을 볼 수 있다. 이것은 맥락 검색 범위 구분이며 파일 접근 권한을 대신하지 않는다.

프로젝트 맥락은 출처를 명시한 로컬 JSON 목록으로 반복 등록할 수 있다.

```sh
uv run ttakkari context-import /absolute/path/catalog.json
```

기존 사용자 기억은 보존한다. 같은 프로젝트·기억 키·출처는 갱신하며, 지운 항목은 자동 부활시키지 않는다.
등록 문서는 시작 시와 매시간 변경분을 색인하고, 비서 관리 > 지식에서 수동 재색인도 가능하다.
설계와 목록 형식은 [ADR 0009](docs/adr/0009-context-library.md)을 따른다.

## 구조

```
src/ttakkari/
  api/          REST·SSE 라우터 (auth, workspaces, sessions·runs, artifacts, system)
  agents/       엔진 어댑터. claude stream-json, codex exec --json → 공통 이벤트
  runs/         Run 수명주기: 실행, 이벤트 기록, 취소, 재시작 복구, git 체크포인트
  broker/       File Broker 경로 정책(루트 고정, .. · 심링크 탈출 차단, 민감 파일 판정)
  artifacts/    등록·저장(copy/reference), 형식 판별, 미리보기 변환
  security/     비밀번호, JWT, 로그인 제한, 에이전트 guard hook
alembic/        마이그레이션
```

## 보험 (자동 실행 안전장치)

에이전트는 승인 없이 돈다. 대신 아래가 걸려 있다. 자세한 내용과 한계는 [ADR 0003](docs/adr/0003-autonomy-and-insurance.md).

- Claude: PreToolUse guard hook 이 워크스페이스 밖 쓰기, 고위험 명령, 민감 파일 읽기를 차단
- Codex: OS 샌드박스 `workspace-write`
- git 워크스페이스는 Run 마다 체크포인트 `refs/ttakkari/runs/<run_id>` 를 남긴다. 되돌리기:
  ```bash
  git diff refs/ttakkari/runs/<run_id>          # 무엇이 바뀌었나
  git checkout refs/ttakkari/runs/<run_id> -- . # 추적 파일을 Run 직전으로
  ```
- 비상 정지 `POST /api/system/stop-all`
- 차단 기록: `~/.ttakkari/data/guard.log`, 감사 로그 `audit_logs` 테이블

## 운영 로그·오류 이슈 (ADR 0033)

배포 job 로그, Run 실패·엔진 폴백, API 500, `~/.ttakkari/logs/*.log`의 ERROR/Traceback을 백엔드가 읽어 비밀값을 가리고 fingerprint로 이슈를 묶는다. 에이전트는 보호 영역을 건드리지 않고 MCP 도구로 읽는다: `ops_list_issues` → `ops_get_issue`, `ops_deploy_log(job="latest_failed")`, `ops_search_logs`, `ops_list_deploys`, `ops_update_issue`.
응답은 요약 우선이고 항상 16,000자 이하다. 프론트는 부가 기능 > 로그·이슈. 주기 인입은 `TTAKKARI_OPS_LOG_SYNC_SECONDS`(기본 60, 0이면 끔)이고 조회 직전에도 20초 넘게 지났으면 한 번 돌린다. 보존·삭제 정책은 아직 없다.

## 디자인시스템 조회

디자인시스템 조회는 부가 기능 > 디자인시스템에서 프로젝트별 토큰·테마·참조·문서·컴포넌트 CSS·예제 원문을 읽는다.
MCP는 `design_system_catalog` → `design_system_search` → `design_system_item` 순서다. 개인 세션만 허용하며 커밋된 원본만 읽는다.
설정은 `TTAKKARI_DESIGN_SYSTEM_ROOT`(기본 `~/development/projects/winterholic-design-system`), 계약과 범위는 [ADR 0034](docs/adr/0034-design-system-browser.md)를 따른다.

## 테스트

```bash
createdb ttakkari_test   # 처음 한 번
uv run pytest -q
```

테스트는 가짜 엔진 스크립트(`tests/fake_agents/`)를 쓴다. 실제 CLI 를 부르지 않는다.

## 비서 맥락(ADR 0007)

PostgreSQL 에 pgvector 가 필요하다(`brew install pgvector`). 마이그레이션 0006 이 `vector`·`pg_trgm` 확장을 만든다.
에이전트는 모든 Run 에서 `ttakkari` MCP 도구(`recall`·`remember`·`share_file` 등)를 쓴다. 비서 지침은 `~/.ttakkari/harness/instructions.md`.

## 배포(Mac Studio + Cloudflare 터널, 포트 안 엶)

`<home>/deploy/generations-enabled.json`으로 활성화된 설치는 세대별 무중단 배포를 사용한다.
설정 기본값 `idle`은 아직 최초 전환을 하지 않은 설치용이며, 활성화 marker가 있으면 `generations`가 우선한다.
[세대별 배포 구성·초기 전환·복구 기준](docs/design/generation-deployment.md)을 먼저 읽는다.

`deploy/install-services.sh deployer`로 독립 배포 감독(`net.winterholic.ttakkari.deployer`)을 설치한다.
`deploy/deploy.sh [--no-pull]`은 배포를 **예약하고 즉시 반환**한다. 이 응답은 배포 성공이 아니다.
`deploy/deploy.sh status [예약 ID]`로 상태를 확인한다. 로그는 `~/.ttakkari/deploy/jobs/<ID>.log`, 감독 로그는 `~/.ttakkari/logs/deployer.log`다.

개인 따까리 개발은 관련 검사를 통과한 변경을 커밋·푸시하고 `request_backend_deploy(revision=<전체 SHA>)` MCP 도구로 적극 예약한다.
사용자가 명시적으로 배포를 보류하지 않았다면 별도 배포 요청·승인을 다시 요구하지 않는다.
진행 중인 다른 작업이나 예약을 요청하는 작업 자신이 있다는 이유로 예약·전환을 미루지 않는다.
작업 안에서 잠금 파일을 쓰거나 `launchctl`을 호출하지 않는다. 개인 세션의 기존 인증 토큰만으로 예약하며 회사 세션은 거절한다.
`backend_deploy_status(job_id)`는 상태 조회 도구다. 배포 완료를 기다리려고 Run을 유지하며 폴링하지 않는다.
접수 시 예약 ID와 상태를 보고하고, 기존 예약이 있다는 이유로 새 변경을 잊거나 사용자에게 다시 요청하도록 하지 않는다.

세대별 감독은 고정 SHA를 격리 빌드·검사하고 준비된 신세대로 신규 요청을 전환한다.
`draining`은 신버전 전환을 확인한 상태이며, 구세대의 기존 엔진이 끝나면 종료하고 `succeeded`로 기록한다.
세 슬롯(8791/8792/8793)이 모두 사용 중이면 후속 예약은 자동 대기하며, 시작하지 않은 같은 계약의 누적 커밋만 최신 예약으로 합친다.
구버전 고정 감독은 앱 배포만으로 갱신되지 않는다. [감독 갱신 절차](docs/design/generation-deployment.md#고정된-배포-감독-갱신)에 따라 Run 밖에서 검증된 릴리스로 감독을 한 번 교체해야 3슬롯 연속 배포가 적용된다.
검사·readiness·schema/protocol 호환성·실행 소유권·안전한 구세대 종료 조건은 유지한다.
실패 시 이유와 복구 상태를 남긴다. 테스트는 배포별 독립 DB를 만들고 정리한다.

**최초 전환 전의 legacy `idle` 경로에만** 전체 Run 종료 대기와 admission 잠금 후 migration·재시작을 적용한다.
이때 들어온 요청은 DB 대기열에 남으며 확인 후 자동 재개한다. 감독이 종료되면 OS가 잠금을 반환하고 다음 기동에서 예약 상태를 이어 읽는다.

성공은 로컬 DB health와 `/api/version`의 시작 시점 SHA, 공개 health·버전까지 확인한 경우에만 기록한다. legacy 재시작은 PID 변경도 확인한다.
`current`는 검증된 커밋이고 `previous`는 그 이전 성공 기록이다. `rollback`도 직전 커밋의 배포를 예약하며 DB 마이그레이션을 되돌리지는 않는다. 감독·버전 확인 기능이 없는 구버전은 자동 롤백 전에 거절한다.
기본 `idle` 모드에서 같은 커밋의 중복 예약은 기존 ID를 반환한다. 다른 커밋의 동시 예약과 예약 이후 작업 트리 변경은 명시적으로 거절한다.
legacy 빌드는 기존 체크아웃을 사용하므로 예약 이후 변경했다면 새 커밋으로 다시 예약해야 한다. 세대별 빌드는 예약 SHA의 immutable archive를 사용하므로 이후 개발이 기존 준비·배출 중인 릴리스를 바꾸지 않는다.
`--skip-tests`는 운영자가 이미 같은 커밋을 검사했을 때만 직접 CLI에서 사용한다. 에이전트 예약 API는 테스트 생략·임의 명령·경로·환경 변수를 받지 않는다.

- 최초 전환 전 백엔드: `deploy/install-services.sh backend` → launchd(`net.winterholic.ttakkari.backend`)가 127.0.0.1:8787 로 상시 실행. 세대별 전환 이후에는 이 설치 명령을 다시 실행하지 않는다. 프록시가 8787, 앱이 8791/8792/8793을 사용한다.
- 터널: `cloudflared tunnel login` → `cloudflared tunnel create ttakkari` → `~/.cloudflared/ttakkari.yml`(예시 `deploy/cloudflared-ttakkari.yml.example`) → `cloudflared tunnel route dns ttakkari ttakkari-api.winterholic.net` → `deploy/install-services.sh tunnel`.
- 주소는 한 단계 서브도메인(`ttakkari-api.winterholic.net`)이어야 Cloudflare 무료 인증서(`*.winterholic.net`)가 덮는다. `api.ttakkari.winterholic.net` 은 TLS 핸드셰이크에서 실패했다.
- 운영 `config.env`: `TTAKKARI_ENV=prod`, `TTAKKARI_CORS_ORIGINS` 에 `https://ttakkari.winterholic.net`, `TTAKKARI_TRUST_PROXY_IP_HEADER=CF-Connecting-IP`.

## 회의 전사 런타임 기본 경로 (ADR 0027)

회의 녹음은 로컬 Whisper만 사용한다. 별도 서비스·Chrome·Playwright·외부 계정은 필요하지 않다.
설정을 비우면 Python은 `~/development/projects/winterholic-ttakkari/back/.stt-runtime/venv/bin/python`,
모델은 같은 `.stt-runtime/faster-whisper-large-v3/`에서 찾는다. 이 Mac에는 해당 환경과 공개 large-v3 가중치를 준비했다.
앱 코드는 현재 배포본의 `src`를 자식의 PYTHONPATH로 전달하므로 런타임에 앱 패키지를 중복 설치하지 않는다.
명시한 `TTAKKARI_LOCAL_STT_PYTHON`·`TTAKKARI_LOCAL_STT_MODEL_PATH`는 계속 우선한다.
이전 외부 연동·기본 provider 설정 키는 제거되어 `extra=ignore`로 무시된다. 환경파일을 편집할 필요가 없다.
설치·데이터 보존·배포 후 스모크: [제거·전환 안내](docs/stt/remove-clovanote-2026-10-15.md).
백엔드 배포·마이그레이션은 기획 세션의 독립 배포 감독만 실행한다.

## 명시 선택형 로컬 Qwen 작업자

`spawn_worker(engine="qwen-local", task_type="general")`은 로컬 `qwen-worker run` 어댑터를 명시 선택한다. `engine="auto"`의 기존 작업자 정책에는 Qwen을 넣지 않는다. Qwen 경로에는 Ttakkari MCP 서버·계정 인증 환경을 전달하지 않으며, 로컬 작업에는 cloud quota/account 사용량을 매기지 않는다. 라우팅 기록은 `qwen-local` 실제 엔진·Qwen3.8 Q2_K 모델·개인/회사 pool scope·workspace를 기록한다. 접수용 Qwen3.5-4B와는 독립이다.

작업 턴 기본값은 standalone과 같은 20회다. `TTAKKARI_QWEN_LOCAL_TURNS`로 1~60회 안에서 조정한다.

기본값 `TTAKKARI_QWEN_LOCAL_ENABLED=false`는 fail-closed다. 켜져 있어도 고정 `qwen-worker doctor`가 JSON으로 `contract: qwen-local-readiness-v1`과 `checks.seatbelt`, `checks.hostmon`, `checks.fixture` 전부 `true`를 반환하지 않으면 작업을 시작하지 않는다. 현재 standalone 하네스 doctor는 이 readiness 계약을 제공하지 않으며, command sandbox에서 sandbox/sysctl 검사가 EPERM으로 막힌 상태다. 따라서 현재 adapter는 운영 ready가 아니다. 자동 cloud fallback도 없다.

## macOS Codex 브라우저 검증

공식 Playwright MCP를 프로젝트 안에 고정 버전으로 설치한다. 배포 서버를 새로 구성할 때도 한 번 실행한다.

```sh
npm ci --prefix .browser-runtime --ignore-scripts
```

설치되어 있고 Node가 PATH에 있으면 제한된 macOS Codex Run(신규·재개)에 `ttakkari_browser` stdio MCP가 붙는다.
화면 이동·DOM 스냅샷·스크린샷 등 18개 도구만 노출한다. 임의 Node 실행·페이지 JS 평가·파일 업로드는 노출하지 않는다.
브라우저는 headless·isolated 프로필이며 Chromium 자체 샌드박스도 켠다. 기존 Chrome 로그인·쿠키를 공유하지 않는다.
스크린샷은 파일명을 생략하면 해당 Run의 `outbox/browser/`에 저장된다. 작업이 끝나면 `browser_close`를 호출한다.
런타임이 없으면 자동 다운로드하지 않는다. 쉘의 `workspace-write`와 `approval_policy=never`는 유지한다.

명령 도구에서 Chrome을 직접 실행해야 한다면 먼저 `src/ttakkari/agents/browser_preflight.py`를 **그 명령 도구 안에서** 실행한다.
검사는 현재 프로세스의 macOS IPC 권한만 조회하며 브라우저를 띄우지 않는다. `blocked`·`unknown`은 종료 코드 78이다.
권한 거부 시 브라우저 종류·headless·`--no-sandbox`를 바꿔 반복하지 않고 브라우저 검증 미실행으로 보고한다.
이 지침은 임의 쉘 명령을 강제 가로채는 보안 경계가 아니다. MCP 연결만으로 실제 브라우저 실행 성공을 보증하지 않는다.

설정·사전 검사 단위 검증(브라우저·DB 미실행):

```sh
.venv/bin/python -m unittest discover -s tests -p 'test_browser*.py'
```
