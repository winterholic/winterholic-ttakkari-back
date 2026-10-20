from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ttakkari.model_catalog import ANTIGRAVITY_DEFAULT, ANTIGRAVITY_MODELS, MUSE_EFFORT

log = logging.getLogger(__name__)

# SEC2-AUTH-02: env가 prod가 아닐 때 자동으로 들어가는 공개 개발 상수.
DEV_INSECURE_JWT_SECRET = "dev-insecure-secret-change-me-dev-insecure"
# HS256 서명키 최소 길이(바이트). 문자 수가 아니라 UTF-8 바이트로 잰다.
MIN_PROD_JWT_SECRET_BYTES = 32

HOME = Path.home()
DEFAULT_HOME = HOME / ".ttakkari"
STT_RUNTIME_ROOT = HOME / "development/projects/winterholic-ttakkari/back/.stt-runtime"


def _host(origin: str) -> str:
    try:
        return (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return ""


def _is_local_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(
        (".localhost", ".local", ".test", ".example", ".invalid")
    )


def validate_operational_settings(s: Settings) -> None:
    """SEC2-AUTH-02 기동 시 검사. 결과는 로그로만 남기고 /api/health 등 응답에는 노출하지 않는다.

    실패(RuntimeError: 기동 중단)는 prod 서명키 결함처럼 모호하지 않은 경우에만 쓴다.
    env != prod + 프로덕션 origin 조합은 실패가 아니라 강한 경고로 남긴다. 근거: public_share_origin
    기본값 자체가 프로덕션 도메인이라 실패로 하면 dev·테스트의 정상 조합까지 기동이 막히고,
    env 값만으로는 인터넷 노출을 단정할 수 없기 때문이다.
    """
    if s.is_prod:
        if not s.jwt_secret:
            raise RuntimeError(
                "TTAKKARI_JWT_SECRET 가 비어 있습니다. ~/.ttakkari/config.env 에 설정하세요."
            )
        if s.jwt_secret == DEV_INSECURE_JWT_SECRET:
            raise RuntimeError(
                "운영(env=prod)에서는 개발용 기본 서명키를 쓸 수 없습니다. "
                "TTAKKARI_JWT_SECRET 에 32바이트 이상의 무작위 값을 설정하세요."
            )
        secret_len = len(s.jwt_secret.encode("utf-8"))
        if secret_len < MIN_PROD_JWT_SECRET_BYTES:
            raise RuntimeError(
                f"운영(env=prod) 서명키가 {secret_len}바이트로 짧습니다. "
                f"TTAKKARI_JWT_SECRET 은 {MIN_PROD_JWT_SECRET_BYTES}바이트 이상이어야 합니다."
            )
    elif _host(s.public_share_origin) and not _is_local_host(
        _host(s.public_share_origin)
    ):
        # dev에서는 서명키가 공개 상수 + 쿠키 Secure 미적용(api/auth.py)이라 공개 origin이면 위험하다.
        log.warning(
            "env가 %r인데 public_share_origin이 공개 도메인(%s)입니다. "
            "개발용 서명키와 Secure 미적용 쿠키로 동작 중이니, 운영 트래픽을 받으면 "
            "TTAKKARI_ENV=prod와 32바이트 이상 서명키를 설정하세요.",
            s.env,
            s.public_share_origin,
        )
    if s.cors_origins and all(_is_local_host(_host(o)) for o in s.cors_origins):
        log.warning(
            "cors_origins가 localhost/loopback 전용(%s)입니다. "
            "운영 프론트(별도 사이트)가 있으면 브라우저 호출이 막히니 구성을 확인하세요.",
            ",".join(s.cors_origins),
        )
    # 운영은 Cloudflare Tunnel 뒤에서 CF-Connecting-IP를 믿는다(설정 주석·client_ip 참조).
    # 그 조합은 검증된 구성이니 조용히 통과하고, 다른 헤더는 터널 전제를 확인하라고 경고한다.
    header = (s.trust_proxy_ip_header or "").strip()
    if header and header.lower() != "cf-connecting-ip":
        # security/auth.py client_ip는 loopback 피어의 헤더만 믿지만, 설정 자체가 터널 전제를 깬다.
        log.warning(
            "trust_proxy_ip_header(%s)가 설정되어 클라이언트 IP를 프록시 헤더에서 읽습니다. "
            "Cloudflare Tunnel 뒤가 아니면 IP 위조(로그인 레이트리밋 우회)가 가능하니, "
            "직접 노출 환경이면 비우세요.",
            s.trust_proxy_ip_header,
        )


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
        # 네이티브가 모두 한도면 오케스트레이터 auto 가 이 쌍으로 Muse 에 이어서 돈다.
        "muse": {"model": "muse-spark-1.3", "effort": MUSE_EFFORT},
    }
    # 명시 엔진·레거시 기본값. 신규 auto 작업자는 runs/worker_routing.py의 task 정책을 사용한다.
    worker_default_models: dict[str, dict[str, str]] = {
        "claude": {"model": "sonnet", "effort": "high"},
        "codex": {"model": "gpt-6-luna", "effort": "high"},
        "antigravity": {"model": ANTIGRAVITY_DEFAULT, "effort": "high"},
        "muse": {"model": "muse-spark-1.3", "effort": MUSE_EFFORT},
        "auto": {"model": "gpt-6-luna", "effort": "high"},
        "qwen-local": {"model": "Qwen3.8-Flash-Next-Q2_K", "effort": "high"},
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
        # Only the subscriber model with a successful response on this account is admitted.
        "muse": ["muse-spark-1.3"],
        # This local engine is explicitly selectable; it is never a ROUTES auto stage.
        # Separate single-entry paths (emergency fallback, parallel-fit workers) still pass this gate.
        "qwen-local": ["Qwen3.8-Flash-Next-Q2_K"],
    }
    qwen_local_enabled: bool = False
    qwen_local_turns: int = Field(default=20, ge=1, le=60)
    qwen_local_worker_path: Path = HOME / "development/projects/qwen-local-harness/bin/qwen-worker"
    # 평시 병렬-fit 작업자의 Qwen 자동 배정(runs/worker_routing 평시 경로). 끄면 명시 지정·비상 경로만 남는다.
    qwen_parallel_routing: bool = True
    # 명시 지정 Qwen Run이 슬롯 사용 중일 때 대기열에서 다시 두드리는 간격(초). 슬롯이 비면 종료 시점에 깨운다.
    qwen_queue_retry_seconds: int = Field(default=30, ge=5, le=600)
    # 계정 사용량 수집(runs/usage_collector.py). 0 이면 수집기를 끈다(테스트는 직접 부른다).
    # 평소 poll, Run 이 그 계정을 쓰는 중이거나 잔여가 low_remaining_percent 미만이면 busy 주기로 당긴다. 실패하면 두 배씩 늦춘다(최대 backoff_max).
    usage_poll_seconds: int = 300
    resource_sample_seconds: int = Field(default=30, ge=0)
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
    # Ask each account's own CLI which models it can run (`agy models`); off in tests.
    antigravity_model_probe: bool = True
    # Muse coding, MCP and resume passed on deployed 6d5f580 (2026-10-18).
    muse_enabled: bool = True
    # Joins Muse to auto worker routing (general/routine). Also requires the code acceptance latch;
    # TTAKKARI_MUSE_AUTO_ROUTING=false remains an explicit opt-out.
    muse_auto_routing: bool = True
    muse_bin: str = str(HOME / "development/tooling/muse/bin/muse")
    muse_node_bin: str = "node"
    muse_runtime_root: Path = HOME / "development/projects/winterholic-ttakkari/back/.muse-runtime"
    muse_profile_root: Path | None = None  # defaults to <configured home>/accounts/muse-default
    # 사용자 전역 hook 이 headless 실행에도 끼어든다. 필요하면 "project,local" 로 좁힌다.
    claude_setting_sources: str | None = None
    # 엔진 프로세스를 sandbox-exec 로 감싸 DB 포트·소켓과 비밀 경로를 커널에서 막는다. 단계 적용용으로 기본은 비어 있다.
    # 지금은 claude 만 가능. 자체 샌드박스를 거는 엔진을 넣으면 Run 이 샌드박스 없이 뜨지 않고 실패한다.
    # (docs/security/adr-engine-seatbelt.md) 예: TTAKKARI_ENGINE_SEATBELT='["claude"]'
    engine_seatbelt: list[str] = Field(default_factory=list)
    engine_seatbelt_deny_keychain: bool = False
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
    # Read-only monitoring of the separate qwen-local-harness server. Never starts it.
    qwen_flash_monitor_port: int = Field(default=18987, ge=1, le=65535)
    qwen_flash_monitor_model: str = "qwen-flash-next-q2k"
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
    # 엔진과 무관한 호스트 GPU 시험 경로(host_gpu.py, ADR 0037). 허용 목록의 모델만 띄우고 한 번에 하나만 둔다.
    host_gpu_enabled: bool = True
    host_gpu_server_path: str = "~/private-resources/service-data/ttakkari-qwen/llama/llama-b11655/llama-server"
    host_gpu_models: dict[str, str] = {
        "qwen3.8-flash-next-q2k":
            "~/private-resources/models/qwen38-flash-next-q2k/Qwen3.8-Flash-Next-Q2_K-00001-of-00002.gguf",
        "qwen3.5-4b-q4_k_m": "~/private-resources/service-data/ttakkari-qwen/model/Qwen3.5-4B-Q4_K_M.gguf",
    }
    host_gpu_max_runtime_seconds: int = Field(default=1800, ge=60, le=7200)
    host_gpu_start_timeout_seconds: int = Field(default=600, ge=10, le=1800)
    host_gpu_swap_growth_gb: float = Field(default=1.0, gt=0, le=8)
    host_gpu_port_min: int = Field(default=19300, ge=1024, le=65000)
    host_gpu_port_max: int = Field(default=19399, ge=1024, le=65535)
    # 운영 Qwen 서버 포트. 시험 인스턴스는 이 포트를 쓰지도 건드리지도 않는다.
    host_gpu_reserved_ports: list[int] = [18987, 57170]
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
    # 운영 로그·오류 이슈 인입 주기(ADR 0033). 0이면 주기 인입을 끄고 조회 직전 갱신만 한다.
    ops_log_sync_seconds: int = Field(default=60, ge=0)
    # Fixed repository; API callers cannot supply paths or trigger builds/fetches.
    design_system_root: Path = HOME / "development/projects/winterholic-design-system"
    # ArtCraft 관제(artcraft.py). 고정 저장소의 bin/artcraft 만 실행한다. 최신 릴리스는 6시간 캐시로 GitHub 에서 가져온다.
    artcraft_root: Path = HOME / "development/projects/artcraft"
    artcraft_auto_remote_check: bool = True
    # ArtCraft 작업 세션의 워크스페이스(ADR 0039). git 저장소가 아니어야 작업마다 개발 저장소에 체크포인트가 생기지 않는다.
    artcraft_workspace_root: Path = HOME / "development/projects/artcraft-workspace"
    # 접수 상주화(runs/desk_pool): 계정마다 엔진 프로세스를 하나 미리 띄워 기동 시간을 뺀다. 끄면 매번 CLI 를 새로 띄운다.
    desk_warm_pool: bool = True
    # 오래 띄워 둔 프로세스는 새로 띄운다(CLI 업데이트·인증 갱신을 늦게 반영하지 않게).
    desk_warm_max_age_seconds: float = 600.0
    # 이 시간 동안 안 쓴 계정은 다시 띄우지 않는다(안 쓰는 계정이 메모리·기동 요청을 계속 쓰지 않게).
    desk_warm_idle_seconds: float = 6 * 60 * 60
    run_timeout_seconds: int = 2 * 60 * 60
    cancel_grace_seconds: float = 5.0

    # 주식 투자·토스증권 읽기 전용 브라우저 어댑터(ADR 0044). 주문·이체 등 계정 상태를 바꾸는 경로는 두지 않는다.
    # 자동 새로고침·스케줄이 없고, 수동 트리거만 check_interval_seconds 간격으로 받는다(사람 수준 저빈도).
    tossinvest_enabled: bool = True
    tossinvest_profile: Path | None = None  # 비우면 <home>/tossinvest-chrome (테스트의 임시 HOME 을 따라간다)
    tossinvest_chrome_bin: str | None = None  # 비우면 설치된 Google Chrome 을 자동 탐색한다
    tossinvest_check_interval_seconds: int = Field(default=300, ge=300)
    tossinvest_page_timeout_seconds: int = Field(default=45, ge=5, le=300)

    @property
    def tossinvest_profile_dir(self) -> Path:
        if self.tossinvest_profile is not None:
            return Path(self.tossinvest_profile).expanduser().resolve()
        return self.home / "tossinvest-chrome"

    # NotebookLM 연동(ADR 0045). 기존 Google 계정의 NotebookLM 권한·할당량만 쓰고 유료 API 키 경로는 없다.
    # 세션은 <home>/notebooklm/accounts/<account_id>/ 에 소유자 전용 권한으로 둔다.
    notebooklm_enabled: bool = True
    notebooklm_profile: Path | None = None  # 비우면 <home>/notebooklm-chrome (로그인용 전용 Chrome 프로필)
    notebooklm_chrome_bin: str | None = None  # 비우면 설치된 Google Chrome 을 자동 탐색한다
    notebooklm_login_timeout_seconds: int = Field(default=600, ge=60, le=1800)
    # 계정당 동시 실행 수(프로세스 안). 무중단 배포 중 두 세대가 겹쳐도 합쳐서 이 값의 2배를 넘지 않는다.
    notebooklm_concurrency: int = Field(default=2, ge=1, le=8)
    notebooklm_request_timeout_seconds: int = Field(default=30, ge=5, le=120)
    notebooklm_query_timeout_seconds: int = Field(default=120, ge=10, le=600)
    notebooklm_source_wait_seconds: int = Field(default=180, ge=10, le=900)
    notebooklm_read_retry_max: int = Field(default=3, ge=0, le=3)
    notebooklm_read_retry_base_seconds: float = Field(default=1.0, ge=0.0, le=10.0)
    notebooklm_read_retry_cap_seconds: float = Field(default=8.0, ge=0.0, le=60.0)
    # 429 에 Retry-After 가 없을 때 쿨다운. 이 시간 동안 Google 에 요청을 보내지 않는다.
    notebooklm_default_cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    notebooklm_verify_interval_seconds: int = Field(default=30, ge=5)
    notebooklm_job_stale_seconds: int = Field(default=120, ge=30)
    notebooklm_upload_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)

    @property
    def notebooklm_dir(self) -> Path:
        return self.home / "notebooklm"

    @property
    def notebooklm_profile_dir(self) -> Path:
        if self.notebooklm_profile is not None:
            return Path(self.notebooklm_profile).expanduser().resolve()
        return self.home / "notebooklm-chrome"

    # Google 계정 공통 모듈(ADR 0051). 로그인·세션은 NotebookLM 같은 개별 기능이 아니라 Google 계정 단위로 둔다.
    # 세션·전용 Chrome 프로필은 <home>/google-accounts/<account_id>/ 에 소유자 전용 권한으로 둔다.
    # 아래 둘을 비우면 기존 notebooklm_* 값을 이어서 쓴다(이미 설정해 둔 환경을 바꾸지 않기 위해).
    google_accounts_enabled: bool = True
    google_account_chrome_bin: str | None = None
    google_account_login_timeout_seconds: int | None = Field(default=None, ge=60, le=1800)

    @property
    def google_accounts_dir(self) -> Path:
        return self.home / "google-accounts"

    @property
    def google_chrome_bin(self) -> str | None:
        return self.google_account_chrome_bin or self.notebooklm_chrome_bin

    @property
    def google_login_timeout(self) -> int:
        return self.google_account_login_timeout_seconds or self.notebooklm_login_timeout_seconds

    # 게임 스튜디오(ADR 0046, docs/design/game-studio.md). 게임 프로젝트는 각자 git 저장소이며 이 아래 <game_slug>/ 에 둔다.
    # allowed_roots(~/development) 안이어야 워크스페이스로 등록된다. 빌드 스테이징·불변 스냅샷은 에이전트가 쓸 수 없는 data_dir 에 둔다.
    games_root: Path = HOME / "development/projects/games"
    # 게임 worker Run 은 게임 저장소 밖 기록(작업 기록 등)을 쓰지 않는다(ADR 0058). 이 경로는 guard 보호 영역에 더한다.
    game_worker_protected_paths: list[str] = Field(default_factory=lambda: [
        "~/work-history", "~/development/context-work-history", "~/development/share-work-history"])
    # 게임 worker 의 claude 는 사용자 전역 설정(Stop 훅·CLAUDE.md)을 읽지 않는다. 전역 Stop 훅이 작업 기록을 강제해 저장소 밖에 쓰게 만들기 때문이다.
    game_worker_claude_setting_sources: str = "project,local"
    game_auto_build: bool = True  # 개발·수정 요청 Run 이 성공하면 자동 빌드(ADR 0054). 요청마다 auto_build=false 로 끌 수 있다
    game_build_runtime: str = "docker"  # OrbStack 이 제공하는 docker CLI
    game_build_image: str = "node:20-alpine"  # vite 7 은 Node ^20.19 필요(20.20.2 확인)
    game_build_memory_mb: int = Field(default=2048, ge=256, le=16384)
    game_build_cpus: float = Field(default=2.0, gt=0, le=16)
    game_build_pids: int = Field(default=512, ge=32, le=8192)
    game_build_install_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    # 자동 빌드가 타입 검사에서 실패하면 오류를 담아 같은 게임에 수리 요청을 이 횟수까지 자동으로 보낸다(0 이면 끔, ADR 0058)
    game_auto_repair_attempts: int = Field(default=1, ge=0, le=3)
    game_build_typecheck: bool = True  # 빌드 전에 격리 러너에서 tsc --noEmit 을 돌린다(ADR 0058). tsconfig.json 이 있는 게임만
    game_build_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    game_build_max_source_mb: int = Field(default=200, ge=1, le=4096)
    game_build_max_output_mb: int = Field(default=200, ge=1, le=4096)
    game_registry_hosts: list[str] = ["registry.npmjs.org"]
    # 별도 게임 origin(games/origin.py, ADR 0049). host 가 비어 있으면 API 프로세스는 게임을 어떤 origin 으로도 서빙하지 않는다.
    # host 는 따까리 API 와 다른 hostname(가능하면 다른 registrable domain)이어야 하고, url 은 PWA 가 iframe 에 넣을 공개 기준 주소다.
    game_origin_host: str = ""
    game_origin_url: str = ""
    game_play_token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    # iframe 으로 게임을 끼울 수 있는 PWA origin. 비우면 cors_origins 를 쓴다.
    game_frame_ancestors: list[str] = []

    @property
    def game_builds_dir(self) -> Path:
        return self.data_dir / "game-builds"

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
    login_verify_concurrency: int = Field(default=1, ge=1, le=8)
    login_max_inflight: int = Field(default=4, ge=1, le=32)
    login_verify_burst: int = Field(default=8, ge=1, le=64)
    login_verify_rate_per_second: float = Field(default=1.0, gt=0.0, le=50.0)
    login_queue_timeout_seconds: float = Field(default=1.0, gt=0.0, le=10.0)

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

    # 리버스 엔지니어링 원격 수집 허용 호스트(기본 github.com). https 전용이다.
    reverse_allowed_hosts: list[str] = []
    # 리버스 엔지니어링에서 개인 계정 모델로 보내지 않는 회사 범위(간단히 거부한다).
    company_roots: list[Path] = [Path.home() / "development" / "hyperez"]

    @property
    def reverse_root(self) -> Path:
        return self.data_dir / "reverse"

    @property
    def guard_log(self) -> Path:
        return self.data_dir / "guard.log"

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @model_validator(mode="after")
    def _fail_fast_on_unsafe_operational_settings(self) -> Settings:
        # 모든 기동 경로(API·CLI·워커)가 get_settings/Settings 생성을 거치므로 여기서 fail-fast 한다.
        validate_operational_settings(self)
        return self

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
        s.jwt_secret = DEV_INSECURE_JWT_SECRET
    return s
