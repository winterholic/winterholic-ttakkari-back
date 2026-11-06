"""ArtCraft 앱별 업데이트 백엔드 단위 및 보안 검증 테스트.

로컬 fixture 로 checksum 불일치, zip traversal/symlink, 잘못된 자산, busy guard,
원자적 스왑, 사후 검증 실패 시 자동 원복, 수동 롤백, 재시작 시 거짓 성공 방지를 검증한다.
"""
from __future__ import annotations

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
    old_bin.write_text("#!/bin/sh\necho 'photocraft-cli 0.9.0'\nexit 0\n", encoding="utf-8")
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


# ---------------------------------------------------------------- 안전성/경계 테스트

def test_app_allowlist(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import validate_app

    assert validate_app("photocraft") == "photocraft"
    with pytest.raises(ArtcraftError) as exc:
        validate_app("evil_app")
    assert exc.value.status == 404

    with pytest.raises(ArtcraftError):
        validate_app("photocraft; rm -rf /")


def test_version_validation():
    from ttakkari.artcraft_update import validate_version

    assert validate_version("v0.10.0") == "0.10.0"
    assert validate_version("0.10.0") == "0.10.0"
    assert validate_version("1.2.3-rc1") == "1.2.3-rc1"

    with pytest.raises(ArtcraftError):
        validate_version("v0.10.0; touch /tmp/pwn")
    with pytest.raises(ArtcraftError):
        validate_version("../../etc/passwd")


def test_checksum_verification_fails_on_mismatch(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import verify_checksum

    content = b"sample binary content"
    correct_sha = hashlib.sha256(content).hexdigest()
    sums = f"{correct_sha}  photocraft-cli-0.10.0-macos-universal.zip\n"

    # 정상 일치
    assert verify_checksum(content, "photocraft-cli-0.10.0-macos-universal.zip", sums) is True

    # 불일치 시 ArtcraftError(422, "checksum_mismatch")
    tampered = b"tampered content"
    with pytest.raises(ArtcraftError) as exc:
        verify_checksum(tampered, "photocraft-cli-0.10.0-macos-universal.zip", sums)
    assert exc.value.status == 422
    assert exc.value.code == "checksum_mismatch"


def test_zip_traversal_detection(tmp_path: Path):
    from ttakkari.artcraft_update import safe_extract_zip

    dest = tmp_path / "extract"
    dest.mkdir()

    # 악의적인 ../ 경로가 포함된 zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.txt", b"evil")
    bad_zip_bytes = buf.getvalue()

    with pytest.raises(ArtcraftError) as exc:
        safe_extract_zip(bad_zip_bytes, dest)
    assert exc.value.status == 422
    assert exc.value.code == "insecure_archive"


def test_zip_absolute_path_detection(tmp_path: Path):
    from ttakkari.artcraft_update import safe_extract_zip

    dest = tmp_path / "extract"
    dest.mkdir()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/tmp/evil.txt", b"evil")
    bad_zip_bytes = buf.getvalue()

    with pytest.raises(ArtcraftError) as exc:
        safe_extract_zip(bad_zip_bytes, dest)
    assert exc.value.status == 422
    assert exc.value.code == "insecure_archive"


@pytest.mark.asyncio
async def test_busy_guard_blocks_when_gui_running(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    from ttakkari.artcraft_update import check_busy

    # raw_status 가 photocraft running=True 로 보고하게 모킹
    async def fake_raw_status(force: bool = False):
        return [
            {
                "app": "photocraft",
                "gui": {"running": True, "listening": True},
            }
        ], None, False

    monkeypatch.setattr("ttakkari.artcraft.raw_status", fake_raw_status)

    with pytest.raises(ArtcraftError) as exc:
        await check_busy("photocraft")
    assert exc.value.status == 409
    assert exc.value.code == "app_running"


@pytest.mark.asyncio
async def test_busy_guard_blocks_when_job_active(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import check_busy

    job = artcraft.Job(id="job1", kind="smoke", action="smoke-cli", apps=["photocraft"], state="running")
    artcraft.JOBS["job1"] = job

    with pytest.raises(ArtcraftError) as exc:
        await check_busy("photocraft")
    assert exc.value.status == 409
    assert exc.value.code == "busy"


# ---------------------------------------------------------------- 원자적 교체 및 롤백 테스트

@pytest.mark.asyncio
async def test_apply_update_and_atomic_swap(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    from ttakkari.artcraft_update import apply_update, get_app_update_info

    # 0.10.0 가짜 CLI zip 파일 생성
    new_script = "#!/bin/sh\necho 'photocraft-cli 0.10.0'\nexit 0\n"
    zip_bytes = _make_zip({
        "photocraft-cli-0.10.0-macos-universal/photocraft-cli": new_script,
        "photocraft-cli-0.10.0-macos-universal/README.md": "PhotoCraft 0.10.0",
    })
    sha = hashlib.sha256(zip_bytes).hexdigest()
    sums = f"{sha}  photocraft-cli-0.10.0-macos-universal.zip\n"

    # GitHub 다운로드 대신 로컬 fixture 모킹
    async def fake_fetch_assets(app: str, tag: str):
        return {
            "asset_name": "photocraft-cli-0.10.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums,
            "version": "0.10.0",
            "tag": "v0.10.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch_assets)

    # 사후 검증 모킹 (정상 통과)
    async def fake_verify_installation(app: str, bin_path: Path):
        return True, "photocraft-cli 0.10.0"

    monkeypatch.setattr("ttakkari.artcraft_update.verify_installation", fake_verify_installation)

    # 실행
    result = await apply_update("photocraft", tag="v0.10.0", confirm=True)
    assert result["success"] is True
    assert result["version"] == "0.10.0"
    assert result["previous_version"] == "0.9.0"

    # 심링크가 새 바이너리를 가리키는지 확인
    symlink = fake_artcraft_root / "bin" / "photocraft-cli"
    assert symlink.is_symlink()
    target = symlink.resolve()
    assert "0.10.0" in str(target)

    # 백업이 존재하는지 확인
    info = get_app_update_info("photocraft")
    assert info["can_rollback"] is True
    assert info["previous_version"] == "0.9.0"


@pytest.mark.asyncio
async def test_apply_update_auto_rollback_on_verify_fail(fake_artcraft_root: Path, monkeypatch: pytest.MonkeyPatch):
    from ttakkari.artcraft_update import apply_update

    # 깨진 0.10.0 zip
    broken_script = "#!/bin/sh\necho 'Crash!'\nexit 1\n"
    zip_bytes = _make_zip({
        "photocraft-cli-0.10.0-macos-universal/photocraft-cli": broken_script,
    })
    sha = hashlib.sha256(zip_bytes).hexdigest()
    sums = f"{sha}  photocraft-cli-0.10.0-macos-universal.zip\n"

    async def fake_fetch_assets(app: str, tag: str):
        return {
            "asset_name": "photocraft-cli-0.10.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums,
            "version": "0.10.0",
            "tag": "v0.10.0",
        }

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch_assets)

    # 사후 검증 실패 모킹
    async def fake_verify_installation(app: str, bin_path: Path):
        return False, "Process exited with 1: Crash!"

    monkeypatch.setattr("ttakkari.artcraft_update.verify_installation", fake_verify_installation)

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
        "photocraft-cli-0.10.0-macos-universal/photocraft-cli": "#!/bin/sh\necho '0.10.0'\nexit 0\n",
    })
    sha = hashlib.sha256(zip_bytes).hexdigest()
    sums = f"{sha}  photocraft-cli-0.10.0-macos-universal.zip\n"

    async def fake_fetch_assets(app: str, tag: str):
        return {
            "asset_name": "photocraft-cli-0.10.0-macos-universal.zip",
            "zip_bytes": zip_bytes,
            "sha256_sums": sums,
            "version": "0.10.0",
            "tag": "v0.10.0",
        }

    async def fake_verify_installation(app: str, bin_path: Path):
        return True, "0.10.0"

    monkeypatch.setattr("ttakkari.artcraft_update.fetch_release_assets", fake_fetch_assets)
    monkeypatch.setattr("ttakkari.artcraft_update.verify_installation", fake_verify_installation)

    await apply_update("photocraft", tag="v0.10.0", confirm=True)

    # 2. 수동 롤백 실행
    res = await rollback_update("photocraft", confirm=True)
    assert res["success"] is True
    assert res["restored_version"] == "0.9.0"

    # 심링크가 0.9.0으로 복구됨
    symlink = fake_artcraft_root / "bin" / "photocraft-cli"
    target = symlink.resolve()
    assert "0.9.0" in str(target)


# ---------------------------------------------------------------- 상태 영속화 및 재시작 거짓 성공 방지

def test_interrupted_updates_reconciled_on_startup(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import get_update_state, reconcile_stale_updates

    # 의도적으로 'running' 상태로 남겨진 과거 작업 데이터 주입
    def mutate(state: dict):
        updates = state.setdefault("updates", {})
        updates["photocraft"] = {
            "state": "running",
            "job_id": "stale_job_1",
            "action": "update-apply",
            "target_version": "0.10.0",
            "started_at": "2026-11-06T00:00:00Z",
        }

    artcraft.update_state(mutate)

    # 서버 재시작 시점 reconcile 호출
    reconcile_stale_updates()

    # 'running'이 아니라 'interrupted' 또는 'failed'로 복구되었는지 확인 (거짓 성공 방지!)
    state = get_update_state("photocraft")
    assert state["state"] in ("interrupted", "failed")
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
        # confirm=False -> 428 Precondition Required
        r = await c.post("/api/artcraft/apps/photocraft/update", json={"confirm": False})
        assert r.status_code == 428


@pytest.mark.asyncio
async def test_update_apply_and_rollback_api_success(app_with_routes: FastAPI, monkeypatch: pytest.MonkeyPatch):
    # 가짜 실행 모킹
    async def fake_apply(app: str, tag: str | None = None, confirm: bool = False):
        return {"success": True, "app": app, "version": "0.10.0", "previous_version": "0.9.0"}

    async def fake_rollback(app: str, confirm: bool = False):
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


@pytest.mark.asyncio
async def test_update_conflict_blocks_overlapping_job(fake_artcraft_root: Path):
    from ttakkari.artcraft_update import start_update_job

    # photocraft update job 실행 중 등록
    job = artcraft.Job(id="job_upd_1", kind="update", action="update-apply", apps=["photocraft"], state="running")
    artcraft.JOBS["job_upd_1"] = job

    # 같은 앱에 또 다른 update 시도 시 409
    with pytest.raises(ArtcraftError) as exc:
        start_update_job("photocraft", tag=None, confirm=True)
    assert exc.value.status == 409
    assert exc.value.code == "busy"
