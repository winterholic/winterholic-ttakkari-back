from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ttakkari.model_catalog import ANTIGRAVITY_DEFAULT, ANTIGRAVITY_MODELS

HOME = Path.home()
DEFAULT_HOME = HOME / ".ttakkari"
STT_RUNTIME_ROOT = HOME / "development/projects/winterholic-ttakkari/back/.stt-runtime"


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
    # Opt-in only after one idle bootstrap into isolated releases.
    runtime_generation: bool = False
    runtime_api_url: str = "http://127.0.0.1:8791"
    runtime_poll_seconds: float = Field(default=5, ge=0.05)
    runtime_sse_drain_seconds: int = Field(default=45, ge=1)
    release_revision: str | None = None
    deploy_strategy: Literal["idle", "generations"] = "idle"
    deploy_source_root: Path | None = None
    migration_online: bool = False
    database_pool_size: int = Field(default=5, ge=1, le=20)
    database_pool_overflow: int = Field(default=3, ge=0, le=10)

    jwt_secret: str = Field(default="", repr=False)
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 30
    download_token_ttl_seconds: int = 5 * 60

    # The separate public UI is available. Only owner-approved snapshots get a capability.
    # Operators can explicitly disable delivery without disabling private exports.
    public_sharing_enabled: bool = True
    public_share_origin: str = "https://ttakkari.winterholic.net"

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
    # 명시 엔진·레거시 기본값. 신규 auto 작업자는 runs/worker_routing.py의 task 정책을 사용한다.
    worker_default_models: dict[str, dict[str, str]] = {
        "claude": {"model": "sonnet", "effort": "high"},
        "codex": {"model": "gpt-6-luna", "effort": "high"},
        "antigravity": {"model": ANTIGRAVITY_DEFAULT, "effort": "high"},
        "auto": {"model": "gpt-6-luna", "effort": "high"},
    }
    # 오케스트레이터와 같은 급이라 작업 세션에 쓰려면 승격 사유가 필요한 모델.
    peer_models: list[str] = ["opus", "fable", "gpt-6.1-sol", *[
        m for m in ANTIGRAVITY_MODELS if m.startswith(("claude-opus-", "gemini-4-argon-"))
    ]]
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
        "antigravity": ANTIGRAVITY_MODELS,
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
    # Legacy model hint; meeting routing now follows its fixed personal model chain.
    meeting_analysis_model: str | None = None
    meeting_analysis_effort: str | None = "high"
    # Local-first transcription; model files are never downloaded on a request.
    local_stt_enabled: bool = True
    local_stt_python: Path = STT_RUNTIME_ROOT / "venv/bin/python"
    local_stt_model_path: Path = STT_RUNTIME_ROOT / "faster-whisper-large-v3"
    local_stt_timeout_seconds: int = 7200
    local_stt_max_audio_seconds: int = 7200
    local_stt_cpu_threads: int = 4
    local_stt_profile: Literal["baseline", "improved", "robust"] = "robust"

    @field_validator("local_stt_profile")
    @classmethod
    def safe_operational_stt_profile(cls, value: str) -> str:
        # Benchmark profiles stay available in the standalone worker only.
        return "robust"

    local_stt_max_reprocess_seconds: int = 120
    local_stt_diarization_model_path: Path | None = None  # complete offline Community-1 snapshot
    local_stt_diarization_device: Literal["cpu", "mps", "cuda"] = "cpu"
    local_stt_speaker_embedding_model_path: Path = STT_RUNTIME_ROOT / "ecapa-voxceleb"
    local_stt_separation_model_path: Path | None = None  # offline SepFormer tensor weights

    # 회의록 벡터 저장(ADR 0021). e5 = 로컬 multilingual-e5-large, hash = 테스트용 결정적 해시, off = 끔.
    context_embedder: str = "e5"
    context_embedding_cpu_threads: int = Field(default=2, ge=1, le=16)
    context_embedding_timeout_seconds: int = Field(default=300, ge=1)
    context_model: str = "intfloat/multilingual-e5-large"

    claude_bin: str = "claude"
    codex_bin: str = "codex"
    antigravity_bin: str = "/opt/homebrew/bin/agy"
    # 사용자 전역 hook 이 headless 실행에도 끼어든다. 필요하면 "project,local" 로 좁힌다.
    claude_setting_sources: str | None = None
    # 동시 실행 자리는 역할별로 따로 센다(ADR 0013). 접수는 짧고 가벼워서 따로, 기획이 작업 세션 자리를 막지 않게 따로.
    max_concurrent_desk: int = 16
    max_concurrent_leads: int = 16
    max_concurrent_workers: int = 16
    # 접수: 엔진 순서(앞이 먼저)와 엔진별 모델. 한도·시간 초과·잘못된 출력이면 다음 계정으로 넘어간다.
    desk_engine_order: list[str] = ["codex", "claude"]
    desk_models: dict[str, dict[str, str]] = {
        "codex": {"model": "gpt-6-luna", "effort": "low"},
        "claude": {"model": "haiku"},
    }
    desk_timeout_seconds: float = 25.0
    # 접수 1순위는 로컬 Qwen3.5-4B(llama.cpp 서버, runs/desk_qwen). 서버 장애·시간 초과·잘못된 출력이면 위 엔진 순서(Luna→Haiku)로 넘어간다.
    desk_qwen_enabled: bool = True
    # 비우면 백엔드(세대) 프로세스가 llama-server 자식 프로세스를 직접 띄우고 끝낸다(runs/desk_qwen_service, OS 서비스 등록 없음).
    # 이미 떠 있는 서버를 쓰려면 주소를 지정한다: 이때는 health 만 확인하고 띄우지도 죽이지도 않는다.
    desk_qwen_base_url: str = ""
    desk_qwen_manage: bool = True
    desk_qwen_server_path: str = "~/private-resources/service-data/ttakkari-qwen/llama/llama-b11655/llama-server"
    desk_qwen_model_path: str = "~/private-resources/service-data/ttakkari-qwen/model/Qwen3.5-4B-Q4_K_M.gguf"
    desk_qwen_threads: int = 6
    desk_qwen_ctx: int = 8192
    desk_qwen_start_timeout_seconds: float = 120.0
    # 모델 크기의 1.5배와 이 값 중 큰 쪽보다 가용 메모리가 적으면 띄우지 않는다(접수는 CLI 로 간다).
    desk_qwen_min_free_memory_gb: float = 6.0
    desk_qwen_model: str = "qwen3.5-4b-q4_k_m"
    desk_qwen_timeout_seconds: float = 20.0
    desk_qwen_connect_timeout_seconds: float = 1.0
    desk_qwen_cooldown_seconds: float = 60.0
    # 작업판+최근 대화+메시지 글자 수가 이보다 길면 Qwen 컨텍스트(서버 ctx 8192 기준)를 넘길 수 있어 CLI 경로로 보낸다.
    desk_qwen_max_prompt_chars: int = 4000
    # Qwen desk observer is preparation-only and opt-in only in test/staging environments.
    # No queue consumer or persistent sink is started by application startup.
    qwen35_shadow_enabled: bool = False
    qwen35_shadow_sample_rate: float = Field(default=0.0, ge=0.0, le=0.05)
    # 접수 둘이 동시에 같은 새 일을 넘길 때 기획이 둘 생기지 않게, 이 시간 안에 같은 접수에서 만든 같은 제목(공백·대소문자·
    # 구두점 무시) 기획이 있으면 거기에 이어 넣는다. 0 이면 끈다.
    lead_dedup_seconds: int = 120
    # 끝난 기획 세션 자동 보관(접수 오른쪽 패널·작업판 정리). 기획과 그 작업 세션에 대기·실행 Run 이 없고 마지막 활동이
    # after_hours 넘게 지난 것을 작업 세션과 함께 보관한다. scan_seconds 는 훑는 주기, 0 이면 끈다(테스트는 직접 부른다).
    lead_archive_after_hours: float = 24
    lead_archive_scan_seconds: int = 600
    # 접수 상주화(runs/desk_pool): 계정마다 엔진 프로세스를 하나 미리 띄워 기동 시간을 뺀다. 끄면 매번 CLI 를 새로 띄운다.
    desk_warm_pool: bool = True
    # 오래 띄워 둔 프로세스는 새로 띄운다(CLI 업데이트·인증 갱신을 늦게 반영하지 않게).
    desk_warm_max_age_seconds: float = 600.0
    # 이 시간 동안 안 쓴 계정은 다시 띄우지 않는다(안 쓰는 계정이 메모리·기동 요청을 계속 쓰지 않게).
    desk_warm_idle_seconds: float = 6 * 60 * 60
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
