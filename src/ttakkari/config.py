from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HOME = Path.home()
DEFAULT_HOME = HOME / ".ttakkari"


class Settings(BaseSettings):
    # NOTE: 설정 파일은 워크스페이스 밖(~/.ttakkari/config.env)에 둔다. 에이전트가 정책을 바꾸지 못하게 하는 경계다.
    model_config = SettingsConfigDict(
        env_prefix="TTAKKARI_",
        env_file=(str(DEFAULT_HOME / "config.env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "dev"  # dev | prod
    database_url: str = "postgresql+asyncpg://localhost/ttakkari"
    home: Path = DEFAULT_HOME

    jwt_secret: str = Field(default="", repr=False)
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 30
    download_token_ttl_seconds: int = 5 * 60

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Cloudflare Tunnel 뒤에서만 CF-Connecting-IP 를 믿는다. 직접 노출 시 위조 가능.
    trust_proxy_ip_header: str | None = None

    # 워크스페이스로 등록할 수 있는 상위 경로. 이 밖은 등록 자체를 거부한다.
    allowed_roots: list[Path] = [
        HOME / "development", HOME / "docs", HOME / "personal", HOME / "private-resources", HOME / "testing",
        # 하네스(Claude Code·Codex 설정·스킬·hook)를 따까리가 고칠 수 있게(사용자 결정 10-11). 로그인 토큰은 guard 가 막는다.
        HOME / ".claude", HOME / ".codex",
    ]

    # 서버가 알아서 만드는 기본 워크스페이스. 에이전트 실행 위치가 루트이고, 추가 경로는 쓰기만 허용한다.
    default_workspace_root: Path = HOME / "development"
    default_writable_roots: list[Path] = [
        HOME / "docs", HOME / "personal", HOME / "private-resources", HOME / "testing", HOME / ".claude", HOME / ".codex",
    ]

    # 엔진 프로필(순서 = 우선순위). config_dir 는 claude 면 CLAUDE_CONFIG_DIR, codex 면 CODEX_HOME 으로 계정을 고른다(ADR 0006).
    engine_profiles: list[dict[str, str]] = [{"name": "claude", "engine": "claude"}, {"name": "codex", "engine": "codex"}]
    profile_cooldown_seconds: int = 30 * 60
    # 메인 오케스트레이터가 엔진별로 쓰는 유일한 모델·생각 수준. 세션에 다른 모델이 저장돼 있어도 이것으로 띄운다(사용자 결정 10-13).
    orchestrator_fallback_models: dict[str, dict[str, str]] = {
        "claude": {"model": "opus", "effort": "medium"},
        "codex": {"model": "gpt-6.1-sol", "effort": "high"},
    }
    # 작업 세션 기본값. 모델을 안 고르면 엔진별 기본, engine=auto 면 토큰이 싼 Luna 로 띄운다.
    worker_default_models: dict[str, dict[str, str]] = {
        "claude": {"model": "sonnet", "effort": "high"},
        "codex": {"model": "gpt-6-luna", "effort": "high"},
        "auto": {"model": "gpt-6-luna", "effort": "high"},
    }
    # 오케스트레이터와 같은 급이라 작업 세션에 쓰려면 승격 사유가 필요한 모델.
    peer_models: list[str] = ["opus", "fable", "gpt-6.1-sol"]
    # 작업 세션이 한도 등으로 다른 엔진 계정으로 넘어갈 때 같은 급의 상대 모델.
    model_counterparts: dict[str, str] = {
        "gpt-6-luna": "sonnet", "sonnet": "gpt-6-luna", "haiku": "gpt-6-luna",
        "gpt-6.1-sol": "opus", "opus": "gpt-6.1-sol", "fable": "gpt-6.1-sol",
    }
    # 메인에게 알리는 컨텍스트 사용률 기준(%): review 이상이면 교체 검토, replace 이상이면 교체.
    context_review_percent: int = 50
    context_replace_percent: int = 70
    # 엔진별로 작업 세션에 지정할 수 있는 모델(account_usage 도구가 알려 준다). 프런트 새 세션 화면의 목록과 같다.
    engine_models: dict[str, list[str]] = {
        "claude": ["opus", "sonnet", "haiku"],
        "codex": ["gpt-6-luna", "gpt-6.1-sol"],
    }
    # 계정 사용량 수집(runs/usage_collector.py). 0 이면 수집기를 끈다(테스트는 직접 부른다).
    # 평소 poll, Run 이 그 계정을 쓰는 중이거나 잔여가 low_remaining_percent 미만이면 busy 주기로 당긴다. 실패하면 두 배씩 늦춘다(최대 backoff_max).
    usage_poll_seconds: int = 300
    usage_poll_busy_seconds: int = 90
    usage_low_remaining_percent: int = 20
    usage_poll_jitter: float = 0.15
    usage_backoff_max_seconds: int = 30 * 60
    usage_history_days: int = 14
    # account_usage 도구가 값을 낡았다고 알리는 나이(보통 수집 주기의 3배).
    usage_stale_seconds: int = 15 * 60
    # 한 Run 이 모든 계정의 한도 초기화를 기다릴 수 있는 최대 횟수. 넘으면 실패로 끝낸다(무한 대기 방지).
    max_limit_waits: int = 8
    # 오케스트레이터 MCP 서버가 부를 백엔드 주소. serve 포트를 바꾸면 같이 바꾼다.
    internal_api_url: str = "http://127.0.0.1:8787"

    # 모든 Run 에 넣는 고정 기억의 최대 글자 수(토큰 절약)
    context_brief_chars: int = 3000
    # 비서 지침 파일 하나의 최대 길이. 고정 기억(context_brief_chars)과 따로 센다.
    instructions_max_chars: int = 8000
    # 일정 알림을 찾는 주기(초). 0 이면 끈다(테스트는 직접 부른다).
    reminder_scan_seconds: int = 30
    # 지식(RAG) 출처로 등록할 수 있는 상위 폴더
    context_source_roots: list[Path] = [HOME]

    # 회의록(ADR 0010). 전사 한 건의 최대 글자 수(분석 Run 의 인자 길이·컨텍스트 한도), 분석 Run 의 모델·생각 수준(비우면 세션 기본).
    meeting_transcript_max_chars: int = 120_000
    meeting_analysis_model: str | None = None
    meeting_analysis_effort: str | None = "high"
    # 클로바노트 연동용 브라우저(Playwright) 프로필 폴더. 아직 연동이 없어 읽지 않는다. 인증 정보는 이 폴더 밖 어디에도 두지 않는다.
    clovanote_profile_dir: Path | None = None

    claude_bin: str = "claude"
    codex_bin: str = "codex"
    # 사용자 전역 hook 이 headless 실행에도 끼어든다. 필요하면 "project,local" 로 좁힌다.
    claude_setting_sources: str | None = None
    # 동시 실행 자리는 역할별로 따로 센다(ADR 0013). 접수는 짧고 가벼워서 따로, 기획이 작업 세션 자리를 막지 않게 따로.
    max_concurrent_desk: int = 6
    max_concurrent_leads: int = 4
    max_concurrent_workers: int = 6
    # 접수: 엔진 순서(앞이 먼저)와 엔진별 모델. 한도·시간 초과·잘못된 출력이면 다음 계정으로 넘어간다.
    desk_engine_order: list[str] = ["codex", "claude"]
    desk_models: dict[str, dict[str, str]] = {
        "codex": {"model": "gpt-6-luna", "effort": "low"},
        "claude": {"model": "haiku"},
    }
    desk_timeout_seconds: float = 25.0
    run_timeout_seconds: int = 2 * 60 * 60
    cancel_grace_seconds: float = 5.0

    copy_max_bytes: int = 200 * 1024 * 1024
    upload_max_bytes: int = 200 * 1024 * 1024
    # 폰 → 컴퓨터 파일 보내기(조각 업로드). 동영상 때문에 크게 잡는다.
    transfer_max_bytes: int = 20 * 1024 * 1024 * 1024
    transfer_chunk_bytes: int = 8 * 1024 * 1024
    # 폰에서 보낸 파일의 기본 폴더. 비우면 <home>/inbox(~/.ttakkari/inbox). ~/.ttakkari 안에서 에이전트가 읽고 쓸 수 있는 유일한 곳이다.
    transfer_inbox: Path | None = None
    artifact_retention_days: int = 90
    soffice_bin: str | None = None
    # Web Push VAPID 연락처(mailto: 또는 https:). Apple 은 형식이 틀리면 거절한다. 비우면 vapid.json 값.
    vapid_subject: str | None = None

    login_max_failures: int = 5
    login_lock_seconds: int = 15 * 60

    @field_validator("allowed_roots", "default_writable_roots", mode="after")
    @classmethod
    def _resolve_roots(cls, v: list[Path]) -> list[Path]:
        return [Path(p).expanduser().resolve() for p in v]

    @field_validator("home", mode="after")
    @classmethod
    def _resolve_home(cls, v: Path) -> Path:
        return Path(v).expanduser().resolve()

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def artifact_store(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def uploads_root(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def harness_dir(self) -> Path:
        return self.home / "harness"

    @property
    def inbox_root(self) -> Path:
        return Path(self.transfer_inbox).expanduser().resolve() if self.transfer_inbox else self.home / "inbox"

    @property
    def outbox_root(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def guard_log(self) -> Path:
        return self.data_dir / "guard.log"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.data_dir, self.artifact_store, self.outbox_root, self.uploads_root, self.inbox_root):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache
def get_settings() -> Settings:
    # 테스트는 운영 설정 파일(~/.ttakkari/config.env)을 읽지 않는다. 운영 값이 테스트 기대값을 바꾸지 않게.
    s = Settings(_env_file=None) if os.environ.get("TTAKKARI_NO_ENV_FILE") else Settings()
    if not s.jwt_secret:
        if s.is_prod:
            raise RuntimeError("TTAKKARI_JWT_SECRET 가 비어 있습니다. ~/.ttakkari/config.env 에 설정하세요.")
        s.jwt_secret = "dev-insecure-secret-change-me-dev-insecure"
    return s
