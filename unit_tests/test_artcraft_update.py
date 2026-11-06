"""ArtCraft 앱별 업데이트 백엔드 단위 및 보안 검증 테스트.

로컬 fixture 로 checksum 불일치, zip traversal/symlink, 잘못된 자산, busy guard,
실제 job 경로의 자기 자신 제외(self exclusion), 동일 버전 재설치 보존,
MCP 핸드셰이크 사후 검증 및 실패 시 자동 원복, 수동 롤백, refresh 효과 및 GUI busy,
재시작 시 거짓 성공 방지를 검증한다.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from ttakkari import artcraft
from ttakkari.artcraft import ArtcraftError


def _make_fake_cli_script(version: str, mcp_ok: bool = True, crash: bool = False) -> str:
    """--version 및 MCP initialize 핸드셰이크를 처리하는 모의 바이너리 스크립트."""
    if crash:
        return "#!/bin/sh\necho 'Crash!' >&2\nexit 1\n"

    mcp_resp = (
        'print(json.dumps({"jsonrpc": "2.0", "id": data.get("id", 1), '
        f'"result": {{"protocolVersion": "2024-11-05", "serverInfo": {{"name": "mock", "version": "{version}"}}}}}}), flush=True)\n'
        if mcp_ok
        else 'print(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "mcp init error"}}), flush=True)\n'
    )

    return (
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        'if "--version" in sys.argv:\n'
        f'    print("photocraft-cli {version}")\n'
        "    sys.exit(0)\n"
        'elif "mcp" in sys.argv:\n'
        "    try:\n"
        "        line = sys.stdin.readline()\n"
        "        data = json.loads(line) if line else {}\n"
        f"        {mcp_resp}"
        "        sys.exit(0)\n"
        "    except Exception:\n"
        "        sys.exit(1)\n"
        f'print("photocraft-cli {version}")\n'
        "sys.exit(0)\n"
    )


def _make_zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o755 << 16  # rwxr-xr-x
            zf.writestr(info, content)
    return buf.getvalue()


@pytest.fixture
def fake_artcraft_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """테스트용 가짜 artcraft root 생성."""
    root = tmp_path / "artcraft"
    bin_dir = root / "bin"
    dist_dir = root / "dist" / "cli" / "photocraft"
    downloads_dir = root / "downloads" / "photocraft"
    data_dir = tmp_path / "data"

    bin_dir.mkdir(parents=True)
    dist_dir.mkdir(parents=True)
    downloads_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    # registry.json
    registry_data = {
        "apps": {
            "photocraft": {
                "title": "PhotoCraft",
                "domain": "image",
                "site_status": "production",
                "control_port": 7878,
                "control_auth": "token-file",
                "ver": "0.9.0",
            },
            "vectorcraft": {
                "title": "VectorCraft",
                "domain": "vector",
                "site_status": "production",
                "control_port": 7979,
                "control_auth": "none-documented",
                "ver": "0.13.0",
            },
        }
    }
    (root / "registry.json").write_text(json.dumps(registry_data), encoding="utf-8")

    # 기존 0.9.0 설치 파일 및 바이너리
    old_cli_dir = dist_dir / "photocraft-cli-0.9.0-macos-universal"
    old_cli_dir.mkdir(parents=True)
    old_bin = old_cli_dir / "photocraft-cli"
    old_bin.write_text(_make_fake_cli_script("0.9.0"), encoding="utf-8")
    old_bin.chmod(0o755)

    # bin/photocraft-cli 심링크
    (bin_dir / "photocraft-cli").symlink_to(old_bin)

    # bin/artcraft 가짜 스크립트 (status 호출 시 올바른 json 출력)
    artcraft_bin = bin_dir / "artcraft"
    artcraft_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:2] == ['status']:\n"
        "    print(json.dumps({'generated_at': 'now', 'apps': [\n"
        "        {'app': 'photocraft', 'gui': {'running': False, 'listening': False}},\n"
        "        {'app': 'vectorcraft', 'gui': {'running': False, 'listening': False}}\n"
        "    ]}))\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    artcraft_bin.chmod(0o755)

    # settings monkeypatch
    fake_settings = SimpleNamespace(
        artcraft_root=str(root),
        data_dir=data_dir,
        artcraft_auto_remote_check=False,
    )
    monkeypatch.setattr("ttakkari.artcraft.get_settings", lambda: fake_settings)
    artcraft.reset_caches()

    return root


# ---------------------------------------------------------------- 안전성/경계 테스트

def test_app_allowlist(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import validate_app

    assert validate_app("photocraft") == "photocraft"
    with pytest.raises(ArtcraftError) as exc:
        validate_app("malicious_app")
    assert exc.value.status == 404
    assert exc.value.code == "unknown_app"


def test_version_validation():
    from ttakkari.artcraft_update import validate_version

    assert validate_version("v0.10.0") == "0.10.0"
    assert validate_version("0.10.0") == "0.10.0"
    assert validate_version("v1.2.3-beta.1") == "1.2.3-beta.1"

    with pytest.raises(ArtcraftError) as exc:
        validate_version("0.10.0; rm -rf /")
    assert exc.value.status == 422
    assert exc.value.code == "validation"


def test_checksum_verification_fails_on_mismatch(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import verify_checksum

    data = b"sample binary zip content"
    sha = hashlib.sha256(data).hexdigest()
    sums_text = f"{sha}  photocraft-cli-0.10.0-macos-universal.zip\n"

    # 정상 통과
    assert verify_checksum(data, "photocraft-cli-0.10.0-macos-universal.zip", sums_text) is True

    # 변조된 데이터 -> checksum_mismatch
    with pytest.raises(ArtcraftError) as exc:
        verify_checksum(b"tampered content", "photocraft-cli-0.10.0-macos-universal.zip", sums_text)
    assert exc.value.status == 422
    assert exc.value.code == "checksum_mismatch"


def test_zip_traversal_detection(tmp_path: Path):
    from ttakkari.artcraft_update import safe_extract_zip

    # 상위 경로 traversal 압축 생성
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/passwd", "root:x:0:0")
    bad_zip = buf.getvalue()

    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(ArtcraftError) as exc:
        safe_extract_zip(bad_zip, dest)
    assert exc.value.status == 422
    assert exc.value.code == "insecure_archive"


def test_zip_absolute_path_detection(tmp_path: Path):
    from ttakkari.artcraft_update import safe_extract_zip

    # 절대 경로 압축 생성
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/var/tmp/bad", "exploit")
    bad_zip = buf.getvalue()

    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(ArtcraftError) as exc:
        safe_extract_zip(bad_zip, dest)
    assert exc.value.status == 422
    assert exc.value.code == "insecure_archive"


# ---------------------------------------------------------------- Busy Guard 테스트

@pytest.mark.asyncio
async def test_busy_guard_blocks_when_gui_running(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    from ttakkari.artcraft_update import check_busy

    # GUI 실행 중 모킹
    async def fake_raw_status(force: bool = False):
        return [
            {"app": "photocraft", "gui": {"running": True, "listening": True}},
        ], None, False

    monkeypatch.setattr("ttakkari.artcraft.raw_status", fake_raw_status)

    with pytest.raises(ArtcraftError) as exc:
        await check_busy("photocraft")
    assert exc.value.status == 409
    assert exc.value.code == "app_running"


@pytest.mark.asyncio
async def test_busy_guard_blocks_when_job_active(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import check_busy

    job = artcraft.Job(id="job_active_1", kind="smoke", action="smoke-full", apps=["photocraft"], state="running", mode="full")
    artcraft.JOBS["job_active_1"] = job

    with pytest.raises(ArtcraftError) as exc:
        await check_busy("photocraft")
    assert exc.value.status == 409
    assert exc.value.code == "busy"


# ---------------------------------------------------------------- 원자적 교체 및 롤백 테스트

@pytest.mark.asyncio
async def test_apply_update_and_atomic_swap(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    from ttakkari.artcraft_update import apply_update, get_app_update_info

    new_script = _make_fake_cli_script("0.10.0")
    zip_bytes = _make_zip({
        "photocraft-cli-0.10.0-macos-universal/photocraft-cli": new_script,
        "photocraft-cli-0.10.0-macos-universal/README.md": "PhotoCraft 0.10.0",
    })
    sha = hashlib.sha256(zip_bytes).hexdigest()
    sums = f"{sha}  photocraft-cli-0.10.0-macos-universal.zip\n"

    async def fake_fetch_assets(app: str, tag: str | None = None):
        return {
            "asset_name": "photocraft-cli-0.10.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums,
            "version": "0.10.0",
            "tag": "v0.10.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch_assets)

    result = await apply_update("photocraft", tag="v0.10.0", confirm=True)
    assert result["success"] is True
    assert result["version"] == "0.10.0"
    assert result["previous_version"] == "0.9.0"

    symlink = fake_artcraft_root / "bin" / "photocraft-cli"
    assert symlink.is_symlink()
    target = symlink.resolve()
    assert "0.10.0" in str(target)

    info = await get_app_update_info("photocraft")
    assert info["can_rollback"] is True
    assert info["previous_version"] == "0.9.0"


@pytest.mark.asyncio
async def test_apply_update_auto_rollback_on_verify_fail(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    from ttakkari.artcraft_update import apply_update

    # 깨진 0.10.0 바이너리 (사후 검증 실패)
    broken_script = _make_fake_cli_script("0.10.0", crash=True)
    zip_bytes = _make_zip({
        "photocraft-cli-0.10.0-macos-universal/photocraft-cli": broken_script,
    })
    sha = hashlib.sha256(zip_bytes).hexdigest()
    sums = f"{sha}  photocraft-cli-0.10.0-macos-universal.zip\n"

    async def fake_fetch_assets(app: str, tag: str | None = None):
        return {
            "asset_name": "photocraft-cli-0.10.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums,
            "version": "0.10.0",
            "tag": "v0.10.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch_assets)

    with pytest.raises(ArtcraftError) as exc:
        await apply_update("photocraft", tag="v0.10.0", confirm=True)
    assert exc.value.status == 500
    assert exc.value.code == "verification_failed"

    # 원래 0.9.0 심링크로 자동 복원되었는지 확인!
    symlink = fake_artcraft_root / "bin" / "photocraft-cli"
    target = symlink.resolve()
    assert "0.9.0" in str(target)


@pytest.mark.asyncio
async def test_manual_rollback(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    from ttakkari.artcraft_update import apply_update, rollback_update

    # 1. 0.10.0 업데이트 성공
    zip_bytes = _make_zip({
        "photocraft-cli-0.10.0-macos-universal/photocraft-cli": _make_fake_cli_script("0.10.0"),
    })
    sha = hashlib.sha256(zip_bytes).hexdigest()
    sums = f"{sha}  photocraft-cli-0.10.0-macos-universal.zip\n"

    async def fake_fetch_assets(app: str, tag: str | None = None):
        return {
            "asset_name": "photocraft-cli-0.10.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums,
            "version": "0.10.0",
            "tag": "v0.10.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch_assets)

    await apply_update("photocraft", tag="v0.10.0", confirm=True)

    # 2. 수동 롤백 실행
    res = await rollback_update("photocraft", confirm=True)
    assert res["success"] is True
    assert res["restored_version"] == "0.9.0"

    # 심링크가 0.9.0으로 복구됨
    symlink = fake_artcraft_root / "bin" / "photocraft-cli"
    target = symlink.resolve()
    assert "0.9.0" in str(target)


# ---------------------------------------------------------------- 완료 기준 1: 실제 Job 경로 자기 자신 제외 회귀 테스트

@pytest.mark.asyncio
async def test_start_update_job_actual_path_self_exclusion(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    """실제 start_update_job -> _start -> runner -> apply_update -> check_busy 경로에서 자기 자신을 제외하여 성공해야 함."""
    from ttakkari.artcraft_update import start_update_job, start_rollback_job

    new_bin_script = _make_fake_cli_script("0.10.0")
    zip_bytes = _make_zip({"photocraft-cli": new_bin_script})
    sums_content = f"{hashlib.sha256(zip_bytes).hexdigest()}  photocraft-cli-0.10.0-macos-universal.zip\n"

    async def fake_fetch(app: str, tag: str | None = None):
        return {
            "asset_name": "photocraft-cli-0.10.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums_content,
            "version": "0.10.0",
            "tag": "v0.10.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch)

    # 1. 실제 start_update_job 실행 및 완료 대기
    job = start_update_job("photocraft", tag="v0.10.0", confirm=True)
    assert job.id in artcraft.JOBS

    if job.task:
        await job.task

    assert job.state == "succeeded"
    assert "0.10.0" in job.summary

    # 2. 실제 start_rollback_job 실행 및 완료 대기
    rb_job = start_rollback_job("photocraft", confirm=True)
    if rb_job.task:
        await rb_job.task

    assert rb_job.state == "succeeded"
    assert "0.9.0" in rb_job.summary


# ---------------------------------------------------------------- 완료 기준 2: 동일 버전 재설치 및 실패 시 기존 바이너리 보존

@pytest.mark.asyncio
async def test_reinstall_same_version_preserves_binary(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    """동일 버전(0.9.0) 재설치 시 기존 바이너리가 삭제되지 않고 안전하게 스왑되는지 검증."""
    from ttakkari.artcraft_update import apply_update

    # 0.9.0 동일 버전 패키지
    same_ver_script = _make_fake_cli_script("0.9.0")
    zip_bytes = _make_zip({
        "photocraft-cli-0.9.0-macos-universal/photocraft-cli": same_ver_script,
    })
    sums_content = f"{hashlib.sha256(zip_bytes).hexdigest()}  photocraft-cli-0.9.0-macos-universal.zip\n"

    async def fake_fetch(app: str, tag: str | None = None):
        return {
            "asset_name": "photocraft-cli-0.9.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums_content,
            "version": "0.9.0",
            "tag": "v0.9.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch)

    # 재설치 실행
    res = await apply_update("photocraft", tag="v0.9.0", confirm=True)
    assert res["success"] is True

    symlink = fake_artcraft_root / "bin" / "photocraft-cli"
    assert symlink.exists()
    assert symlink.resolve().is_file()


@pytest.mark.asyncio
async def test_same_version_verify_fail_preserves_existing_installation(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    """동일 버전 재설치 중 사후 검증이 실패하더라도 기존 설치 바이너리가 삭제되지 않고 완벽하게 보존되는지 검증."""
    from ttakkari.artcraft_update import apply_update

    broken_same_ver = _make_fake_cli_script("0.9.0", crash=True)
    zip_bytes = _make_zip({
        "photocraft-cli-0.9.0-macos-universal/photocraft-cli": broken_same_ver,
    })
    sums_content = f"{hashlib.sha256(zip_bytes).hexdigest()}  photocraft-cli-0.9.0-macos-universal.zip\n"

    async def fake_fetch(app: str, tag: str | None = None):
        return {
            "asset_name": "photocraft-cli-0.9.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums_content,
            "version": "0.9.0",
            "tag": "v0.9.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch)

    with pytest.raises(ArtcraftError) as exc:
        await apply_update("photocraft", tag="v0.9.0", confirm=True)
    assert exc.value.status == 500

    # 기존 0.9.0 바이너리와 심링크가 여전히 정상 작동하는 상태로 보존되어 있어야 함
    symlink = fake_artcraft_root / "bin" / "photocraft-cli"
    assert symlink.exists()
    assert symlink.resolve().is_file()


# ---------------------------------------------------------------- 완료 기준 3: MCP 핸드셰이크 검증

@pytest.mark.asyncio
async def test_mcp_handshake_failure_triggers_auto_rollback(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    """--version은 성공하지만 MCP initialize 핸드셰이크가 실패하면 사후 검증 실패로 원복되는지 검증."""
    from ttakkari.artcraft_update import apply_update

    # --version은 성공하나 MCP에서 에러를 내는 바이너리
    bad_mcp_script = _make_fake_cli_script("0.10.0", mcp_ok=False)
    zip_bytes = _make_zip({
        "photocraft-cli-0.10.0-macos-universal/photocraft-cli": bad_mcp_script,
    })
    sums_content = f"{hashlib.sha256(zip_bytes).hexdigest()}  photocraft-cli-0.10.0-macos-universal.zip\n"

    async def fake_fetch(app: str, tag: str | None = None):
        return {
            "asset_name": "photocraft-cli-0.10.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums_content,
            "version": "0.10.0",
            "tag": "v0.10.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch)

    with pytest.raises(ArtcraftError) as exc:
        await apply_update("photocraft", tag="v0.10.0", confirm=True)
    assert exc.value.status == 500
    assert "MCP 핸드셰이크 실패" in exc.value.message

    # 원래 버전으로 원복됨
    symlink = fake_artcraft_root / "bin" / "photocraft-cli"
    assert "0.9.0" in str(symlink.resolve())


# ---------------------------------------------------------------- 완료 기준 4: 미리보기 refresh 실제 효과 및 GUI busy, status 규격

@pytest.mark.asyncio
async def test_preview_refresh_actual_effect_and_gui_busy(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    """refresh=True의 실제 원격 갱신 효과 및 GUI 실행 중일 때 busy=True, can_update=False 반영 검증."""
    from ttakkari.artcraft_update import get_app_update_info

    # 1. refresh=True 시 GitHub 릴리스 취득 모킹
    async def fake_fetch(app: str, tag: str | None = None):
        return {
            "version": "0.11.0",
            "tag": "v0.11.0",
            "published_at": "2026-11-07T00:00:00Z",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch)

    # refresh 호출
    info = await get_app_update_info("photocraft", refresh=True)
    assert info["latest_release"]["tag"] == "v0.11.0"
    assert info["can_update"] is True
    assert info["busy"] is False

    # 2. GUI가 실행 중일 때 busy=True, can_update=False
    async def fake_status_gui_running(force: bool = False):
        return [{"app": "photocraft", "gui": {"running": True, "listening": False}}], None, False

    monkeypatch.setattr("ttakkari.artcraft.raw_status", fake_status_gui_running)

    busy_info = await get_app_update_info("photocraft", refresh=False)
    assert busy_info["busy"] is True
    assert "실행 중" in busy_info["busy_reason"]
    assert busy_info["can_update"] is False


def test_status_response_contract_compliance(fake_artcraft_root: Path):
    """get_update_state 응답이 API 계약 스키마(app, current_version, previous_version, state, last_status, history)를 만족하는지 검증."""
    from ttakkari.artcraft_update import get_update_state

    status = get_update_state("photocraft")
    assert status["app"] == "photocraft"
    assert "current_version" in status
    assert "previous_version" in status
    assert status["state"] == "idle"
    assert "last_status" in status
    assert isinstance(status["history"], list)


def test_interrupted_updates_reconciled_on_startup(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import get_update_state, reconcile_stale_updates

    # 의도적으로 'running' 상태로 남겨진 과거 작업 데이터 주입
    def mutate(state: dict):
        updates = state.setdefault("updates", {})
        updates["photocraft"] = {
            "state": "running",
            "current_job_id": "stale_job_1",
            "action": "update-apply",
            "target_version": "0.10.0",
            "started_at": "2026-11-06T00:00:00Z",
        }

    artcraft.update_state(mutate)

    # 서버 재시작 시점 reconcile 호출
    reconcile_stale_updates()

    # 'running'이 아니라 'interrupted'로 복구되었는지 확인 (거짓 성공 방지!)
    state = get_update_state("photocraft")
    assert state["state"] == "interrupted"
    assert "서버 재시작" in state.get("error", "")


# ---------------------------------------------------------------- FastAPI 라우터 통합 테스트

@pytest.fixture
def app_with_routes(fake_artcraft_root: Path):
    from ttakkari.api.artcraft import router
    from ttakkari.db import get_db
    from ttakkari.security.auth import require_user

    test_app = FastAPI()
    test_app.include_router(router)

    class FakeDb:
        def add(self, obj):
            pass

        async def commit(self):
            pass

    test_app.dependency_overrides[require_user] = lambda: "test-user"
    test_app.dependency_overrides[get_db] = lambda: FakeDb()
    return test_app


@pytest.mark.asyncio
async def test_update_preview_api(app_with_routes: FastAPI):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_with_routes), base_url="http://test") as c:
        r = await c.get("/api/artcraft/apps/photocraft/update")
        assert r.status_code == 200
        data = r.json()
        assert data["app"] == "photocraft"
        assert "installed_version" in data
        assert "can_update" in data
        assert "can_rollback" in data


@pytest.mark.asyncio
async def test_update_apply_requires_confirm(app_with_routes: FastAPI):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_with_routes), base_url="http://test") as c:
        r = await c.post("/api/artcraft/apps/photocraft/update", json={"confirm": False})
        assert r.status_code == 428


@pytest.mark.asyncio
async def test_update_apply_and_rollback_api_success(app_with_routes: FastAPI, monkeypatch: pytest.MonkeyPatch):
    async def fake_apply(app: str, tag: str | None = None, confirm: bool = False, current_job_id: str | None = None):
        return {"success": True, "app": app, "version": "0.10.0", "previous_version": "0.9.0"}

    async def fake_rollback(app: str, confirm: bool = False, current_job_id: str | None = None):
        return {"success": True, "app": app, "restored_version": "0.9.0"}

    monkeypatch.setattr("ttakkari.artcraft_update.apply_update", fake_apply)
    monkeypatch.setattr("ttakkari.artcraft_update.rollback_update", fake_rollback)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_with_routes), base_url="http://test") as c:
        # 1. apply 요청 (202 Accepted)
        r = await c.post("/api/artcraft/apps/photocraft/update", json={"confirm": True, "tag": "v0.10.0"})
        assert r.status_code == 202
        job_data = r.json()
        assert job_data["kind"] == "update"
        assert job_data["action"] == "update-apply"

        # 2. rollback 요청 (confirm=False -> 428)
        r_rb_fail = await c.post("/api/artcraft/apps/photocraft/update/rollback", json={"confirm": False})
        assert r_rb_fail.status_code == 428

        # 3. status 조회
        r_st = await c.get("/api/artcraft/apps/photocraft/update/status")
        assert r_st.status_code == 200
        st_data = r_st.json()
        assert st_data["app"] == "photocraft"
        assert "history" in st_data


@pytest.mark.asyncio
async def test_update_conflict_blocks_overlapping_job(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import start_update_job

    job = artcraft.Job(id="job_upd_1", kind="update", action="update-apply", apps=["photocraft"], state="running")
    artcraft.JOBS["job_upd_1"] = job

    with pytest.raises(ArtcraftError) as exc:
        start_update_job("photocraft", tag=None, confirm=True)
    assert exc.value.status == 409
    assert exc.value.code == "busy"
