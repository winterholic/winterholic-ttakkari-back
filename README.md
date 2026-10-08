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
| `TTAKKARI_ALLOWED_ROOTS` | 워크스페이스로 등록할 수 있는 상위 경로 JSON 배열. 기본 `~/development`, `~/Documents`, `~/Desktop`, `~/Downloads` |
| `TTAKKARI_CORS_ORIGINS` | 프론트 origin JSON 배열 |
| `TTAKKARI_ENV` | `prod` 면 refresh 쿠키가 `SameSite=None; Secure`, `/docs` 비활성 |
| `TTAKKARI_TRUST_PROXY_IP_HEADER` | Cloudflare Tunnel 뒤면 `CF-Connecting-IP` |
| `TTAKKARI_CLAUDE_SETTING_SOURCES` | 비우면 사용자 전역 설정(CLAUDE.md·스킬·hook) 포함. `project,local` 이면 제외 |
| `TTAKKARI_MAX_CONCURRENT_RUNS`, `TTAKKARI_RUN_TIMEOUT_SECONDS` | 동시 실행 수, Run 타임아웃 |

**uvicorn 워커는 1개만 쓴다.** 실행 중 프로세스를 메모리에서 관리한다(ADR 0001).

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
