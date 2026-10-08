from __future__ import annotations

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
    allowed_roots: list[Path] = [HOME / "development", HOME / "Documents", HOME / "Desktop", HOME / "Downloads"]

    claude_bin: str = "claude"
    codex_bin: str = "codex"
    # 사용자 전역 hook 이 headless 실행에도 끼어든다. 필요하면 "project,local" 로 좁힌다.
    claude_setting_sources: str | None = None
    max_concurrent_runs: int = 3
    run_timeout_seconds: int = 2 * 60 * 60
    cancel_grace_seconds: float = 5.0

    copy_max_bytes: int = 200 * 1024 * 1024
    upload_max_bytes: int = 200 * 1024 * 1024
    artifact_retention_days: int = 90
    soffice_bin: str | None = None

    login_max_failures: int = 5
    login_lock_seconds: int = 15 * 60

    @field_validator("allowed_roots", mode="after")
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
    def outbox_root(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def guard_log(self) -> Path:
        return self.data_dir / "guard.log"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def ensure_dirs(self) -> None:
        for d in (self.home, self.data_dir, self.artifact_store, self.outbox_root, self.uploads_root):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.jwt_secret:
        if s.is_prod:
            raise RuntimeError("TTAKKARI_JWT_SECRET 가 비어 있습니다. ~/.ttakkari/config.env 에 설정하세요.")
        s.jwt_secret = "dev-insecure-secret-change-me-dev-insecure"
    return s
