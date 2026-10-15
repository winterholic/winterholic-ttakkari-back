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
| `TTAKKARI_LOCAL_STT_ENABLED`, `TTAKKARI_LOCAL_STT_PYTHON`, `TTAKKARI_LOCAL_STT_MODEL_PATH`, `TTAKKARI_LOCAL_STT_PROFILE` | 기본 전사 엔진 로컬 Whisper(ADR 0025·0026). 기본 켜짐·baseline, 로컬 가중치 경로는 별도 지정. 요청마다 자식 프로세스에서 로드·추론 후 종료한다. [설치·API·측정 안내](docs/stt/README.md). 미준비 시 사유를 알리며 외부 엔진으로 자동 전환하지 않는다 |
| `TTAKKARI_CONTEXT_SOURCE_ROOTS` | 비서 관리 > 지식에 등록할 수 있는 상위 폴더 JSON 배열. 기본 홈 |
| `TTAKKARI_CORS_ORIGINS` | 프론트 origin JSON 배열 |
| `TTAKKARI_ENV` | `prod` 면 refresh 쿠키가 `SameSite=None; Secure`, `/docs` 비활성 |
| `TTAKKARI_TRUST_PROXY_IP_HEADER` | Cloudflare Tunnel 뒤면 `CF-Connecting-IP` |
| `TTAKKARI_CLAUDE_SETTING_SOURCES` | 비우면 사용자 전역 설정(CLAUDE.md·스킬·hook) 포함. `project,local` 이면 제외 |
| `TTAKKARI_MAX_CONCURRENT_RUNS`, `TTAKKARI_RUN_TIMEOUT_SECONDS` | 동시 실행 수, Run 타임아웃 |

**uvicorn 워커는 1개만 쓴다.** 실행 중 프로세스를 메모리에서 관리한다(ADR 0001).

## 맥락 도서관

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

`deploy/install-services.sh deployer`로 독립 배포 감독(`net.winterholic.ttakkari.deployer`)을 설치한다.
`deploy/deploy.sh [--no-pull]`은 배포를 **예약하고 즉시 반환**한다. 이 응답은 배포 성공이 아니다.
`deploy/deploy.sh status [예약 ID]`로 상태를 확인한다. 로그는 `~/.ttakkari/deploy/jobs/<ID>.log`, 감독 로그는 `~/.ttakkari/logs/deployer.log`다.

따까리 안의 작업은 커밋·푸시 후 `request_backend_deploy(revision=<전체 SHA>)` MCP 도구를 부르고 턴을 끝낸다.
작업 안에서 잠금 파일을 쓰거나 `launchctl`을 호출하지 않는다. 개인 세션의 기존 인증 토큰만으로 예약하며 회사 세션은 거절한다.
`backend_deploy_status(job_id)`는 상태 조회 도구다. 요청한 작업 자신도 종료를 기다리므로 활성 작업에서 완료를 기다리며 폴링하지 않는다.

감독은 `requested → building → testing → waiting → migrating → restarting → verifying → succeeded` 순서로 처리한다.
실패 시 `failed`와 이유를 남긴다. 테스트는 배포별 독립 DB를 만들고 정리한다.
대기 중 다른 작업은 계속 실행된다. 실행 작업이 없을 때 작업 시작과 공유하는 배타 잠금을 잡고 다시 확인한 뒤 마이그레이션·재시작한다.
이 구간에 들어온 요청은 DB 대기열에 남으며 확인 후 자동 재개한다. 감독이 종료되면 OS가 잠금을 반환하고 다음 기동에서 예약 상태를 이어 읽는다.

성공은 로컬 DB health와 `/api/version`의 시작 시점 SHA·새 PID, 공개 health·버전까지 확인한 경우에만 기록한다.
`current`는 검증된 커밋이고 `previous`는 그 이전 성공 기록이다. `rollback`도 직전 커밋의 배포를 예약하며 DB 마이그레이션을 되돌리지는 않는다. 감독·버전 확인 기능이 없는 구버전은 자동 롤백 전에 거절한다.
같은 커밋의 중복 예약은 기존 ID를 반환한다. 다른 커밋의 동시 예약과 예약 이후 작업 트리 변경은 명시적으로 거절한다.
빌드에는 기존 체크아웃을 사용하므로 배포 대기 중 해당 저장소를 추가 수정했다면 새 커밋으로 다시 예약해야 한다.
`--skip-tests`는 운영자가 이미 같은 커밋을 검사했을 때만 직접 CLI에서 사용한다. 에이전트 예약 API는 테스트 생략·임의 명령·경로·환경 변수를 받지 않는다.

- 백엔드: `deploy/install-services.sh backend` → launchd(`net.winterholic.ttakkari.backend`)가 127.0.0.1:8787 로 상시 실행. 로그 `~/.ttakkari/logs/backend.log`.
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
