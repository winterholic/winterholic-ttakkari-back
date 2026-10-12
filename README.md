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

- 백엔드: `deploy/install-services.sh backend` → launchd(`net.winterholic.ttakkari.backend`)가 127.0.0.1:8787 로 상시 실행. 로그 `~/.ttakkari/logs/backend.log`.
- 터널: `cloudflared tunnel login` → `cloudflared tunnel create ttakkari` → `~/.cloudflared/ttakkari.yml`(예시 `deploy/cloudflared-ttakkari.yml.example`) → `cloudflared tunnel route dns ttakkari ttakkari-api.winterholic.net` → `deploy/install-services.sh tunnel`.
- 주소는 한 단계 서브도메인(`ttakkari-api.winterholic.net`)이어야 Cloudflare 무료 인증서(`*.winterholic.net`)가 덮는다. `api.ttakkari.winterholic.net` 은 TLS 핸드셰이크에서 실패했다.
- 운영 `config.env`: `TTAKKARI_ENV=prod`, `TTAKKARI_CORS_ORIGINS` 에 `https://ttakkari.winterholic.net`, `TTAKKARI_TRUST_PROXY_IP_HEADER=CF-Connecting-IP`.
