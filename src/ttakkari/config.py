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
    # 오케스트레이터가 다른 엔진 계정으로 넘어갈 때 쓰는 모델·생각 수준(비서 지침의 허용 모델). 그 엔진의 모델을 직접 고른 Run 은 건드리지 않는다.
    orchestrator_fallback_models: dict[str, dict[str, str]] = {
        "claude": {"model": "opus", "effort": "medium"},
        "codex": {"model": "gpt-6.1-sol", "effort": "high"},
    }
    # 한 Run 이 모든 계정의 한도 초기화를 기다릴 수 있는 최대 횟수. 넘으면 실패로 끝낸다(무한 대기 방지).
    max_limit_waits: int = 8
    # 오케스트레이터 MCP 서버가 부를 백엔드 주소. serve 포트를 바꾸면 같이 바꾼다.
    internal_api_url: str = "http://127.0.0.1:8787"

    # 모든 Run 에 넣는 고정 기억·비서 지침의 최대 글자 수(토큰 절약)
    context_brief_chars: int = 3000
    # 일정 알림을 찾는 주기(초). 0 이면 끈다(테스트는 직접 부른다).
    reminder_scan_seconds: int = 30
    # 지식(RAG) 출처로 등록할 수 있는 상위 폴더
    context_source_roots: list[Path] = [HOME]

    claude_bin: str = "claude"
    codex_bin: str = "codex"
    # 사용자 전역 hook 이 headless 실행에도 끼어든다. 필요하면 "project,local" 로 좁힌다.
    claude_setting_sources: str | None = None
    max_concurrent_runs: int = 3
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
