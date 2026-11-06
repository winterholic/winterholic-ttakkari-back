"""ArtCraft 앱별 업데이트 백엔드.

공식 Storytold 릴리스의 확인, SHA256 검증, 안전한 압축 해제, 원자적 스왑,
사후 검증(--version 및 MCP initialize), 실패 시 자동 원복(롤백), 수동 롤백 및 상태 관리를 담당한다.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ttakkari import artcraft
from ttakkari.artcraft import ArtcraftError

logger = logging.getLogger(__name__)

STORYTOLD_ORG = "storytold"
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?)$")
ASSET_CLI_RE = re.compile(r"^[a-z0-9_-]+-cli-\d+\.\d+\.\d+.*macos.*\.zip$")

DOWNLOAD_TIMEOUT = 120.0
VERIFY_TIMEOUT = 15.0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 검증 및 Allowlist

def validate_app(app: str) -> str:
    """13개 공식 ArtCraft 앱 allowlist 검증."""
    reg = artcraft.registry()
    if app not in reg or not artcraft._APP_NAME.match(app):
        raise ArtcraftError(404, "unknown_app", f"등록되지 않은 앱입니다: {app}")
    return app


def validate_version(version: str) -> str:
    """릴리스 버전 태그 검증."""
    match = VERSION_RE.match(version.strip())
    if not match:
        raise ArtcraftError(422, "validation", f"올바르지 않은 버전 형식입니다: {version}")
    return match.group(1)


def verify_checksum(data: bytes, filename: str, sha256_sums: str) -> bool:
    """SHA256SUMS.txt 파일과 다운로드 데이터의 SHA256 해시 검증."""
    calculated = hashlib.sha256(data).hexdigest().lower()
    for line in sha256_sums.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            expected_hash = parts[0].strip().lower()
            expected_file = parts[-1].strip().lstrip("*")
            if expected_file == filename:
                if calculated != expected_hash:
                    raise ArtcraftError(
                        422,
                        "checksum_mismatch",
                        f"{filename} 해시 불일치 (예상: {expected_hash}, 실제: {calculated})",
                    )
                return True
    raise ArtcraftError(422, "checksum_missing", f"{filename}의 해시를 SHA256SUMS 에서 찾을 수 없습니다.")


def safe_extract_zip(zip_bytes: bytes, dest_dir: Path) -> list[str]:
    """Zip Traversal 및 위험한 심링크를 방어하는 안전한 압축 해제."""
    dest_dir = dest_dir.resolve()
    extracted_paths: list[str] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.infolist():
            # 1. 절대 경로 거부
            if member.filename.startswith(("/", "\\")):
                raise ArtcraftError(422, "insecure_archive", f"절대 경로가 포함된 압축 파일입니다: {member.filename}")

            target_path = (dest_dir / member.filename).resolve()

            # 2. Path Traversal (commonpath 탈출) 거부
            try:
                common = os.path.commonpath([str(dest_dir), str(target_path)])
                if common != str(dest_dir):
                    raise ArtcraftError(422, "insecure_archive", f"압축 경로 탈출 시도가 감지되었습니다: {member.filename}")
            except ValueError as exc:
                raise ArtcraftError(422, "insecure_archive", f"유효하지 않은 경로입니다: {member.filename}") from exc

            # 3. 심링크인 경우 대상 경로가 대상 디렉터리 내부에 머무는지 검증
            if stat.S_ISLNK(member.external_attr >> 16):
                link_target = zf.read(member).decode("utf-8", "replace").strip()
                resolved_link = (target_path.parent / link_target).resolve()
                try:
                    common_link = os.path.commonpath([str(dest_dir), str(resolved_link)])
                    if common_link != str(dest_dir):
                        raise ArtcraftError(
                            422,
                            "insecure_archive",
                            f"압축 파일 내부 심볼릭 링크가 외부를 가리킵니다: {member.filename} -> {link_target}",
                        )
                except ValueError as exc:
                    raise ArtcraftError(422, "insecure_archive", f"잘못된 심볼릭 링크입니다: {member.filename}") from exc

            zf.extract(member, dest_dir)
            extracted_paths.append(str(target_path))

    return extracted_paths


# ---------------------------------------------------------------- Busy Guard

async def check_busy(app: str, exclude_job_id: str | None = None) -> None:
    """앱 GUI 실행 여부 및 Job 활성 상태를 점검하여 충돌 방지.
    
    현재 실행 중인 자기 자신의 job_id는 검사에서 제외한다.
    """
    validate_app(app)

    # 1. 진행 중인 Job 검사 (스모크, GUI 조작, 타 업데이트)
    for job in artcraft.JOBS.values():
        if not job.active:
            continue
        if exclude_job_id and job.id == exclude_job_id:
            continue
        # 전체 스모크가 돌고 있으면 모든 앱 잠금
        if job.kind == "smoke" and job.mode == "full":
            raise ArtcraftError(409, "busy", "전체 GUI 스모크 작업이 진행 중입니다.")
        # 해당 앱과 연관된 Job이 있으면 잠금
        if app in job.apps:
            raise ArtcraftError(409, "busy", f"{app} 관련 작업({job.action})이 진행 중입니다.")

    # 2. GUI 실행 중 검사
    try:
        status, _, _ = await artcraft.raw_status(force=False)
        for a in status:
            if a["app"] == app:
                gui = a.get("gui") or {}
                if gui.get("running") or gui.get("listening"):
                    raise ArtcraftError(
                        409,
                        "app_running",
                        f"{app} 앱이 현재 실행 중입니다. 업데이트/롤백 전에 앱을 먼저 종료하세요.",
                    )
    except ArtcraftError as exc:
        if exc.code == "app_running":
            raise
        logger.warning("상태 조회 중 오류 발생 (무시하고 계속): %s", exc.message)


# ---------------------------------------------------------------- 릴리스 자산 취득

async def fetch_release_assets(app: str, tag: str | None = None) -> dict[str, Any]:
    """공식 Storytold 릴리스에서 macOS Universal CLI zip 및 SHA256SUMS 취득."""
    validate_app(app)
    repo = f"{STORYTOLD_ORG}/{app}"

    tag_path = f"tags/{tag}" if tag else "latest"
    api_url = f"https://api.github.com/repos/{repo}/releases/{tag_path}"

    headers = {
        "User-Agent": "Ttakkari-ArtCraft-Updater/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    import httpx

    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(api_url, headers=headers)
        if resp.status_code == 404:
            raise ArtcraftError(404, "release_not_found", f"{repo} 릴리스 정보를 찾을 수 없습니다.")
        if resp.status_code != 200:
            raise ArtcraftError(502, "bad_gateway", f"GitHub API 응답 오류 (status={resp.status_code})")

        release_info = resp.json()
        assets = release_info.get("assets", [])
        cli_asset = None
        sums_asset = None

        for asset in assets:
            name = asset.get("name", "")
            if "macos" in name and name.endswith(".zip") and "-cli-" in name:
                cli_asset = asset
            elif name in ("SHA256SUMS.txt", "SHA256SUMS"):
                sums_asset = asset

        if not cli_asset:
            raise ArtcraftError(404, "asset_not_found", f"{repo} 릴리스에서 macOS CLI zip 자산을 찾을 수 없습니다.")
        if not sums_asset:
            raise ArtcraftError(404, "asset_not_found", f"{repo} 릴리스에서 SHA256SUMS 자산을 찾을 수 없습니다.")

        dl_headers = dict(headers)
        dl_headers["Accept"] = "application/octet-stream"

        zip_resp = await client.get(cli_asset["url"], headers=dl_headers)
        if zip_resp.status_code != 200:
            zip_resp = await client.get(cli_asset["browser_download_url"])
            if zip_resp.status_code != 200:
                raise ArtcraftError(502, "download_failed", f"CLI 자산 다운로드 실패 ({zip_resp.status_code})")

        sums_resp = await client.get(sums_asset["url"], headers=dl_headers)
        if sums_resp.status_code != 200:
            sums_resp = await client.get(sums_asset["browser_download_url"])
            if sums_resp.status_code != 200:
                raise ArtcraftError(502, "download_failed", f"SHA256SUMS 자산 다운로드 실패 ({sums_resp.status_code})")

        zip_bytes = zip_resp.content
        sums_text = sums_resp.text

        verify_checksum(zip_bytes, cli_asset["name"], sums_text)

        version = validate_version(release_info.get("tag_name", ""))
        return {
            "asset_name": cli_asset["name"],
            "zip_bytes": zip_bytes,
            "sha256_sums": sums_text,
            "version": version,
            "tag": release_info.get("tag_name", f"v{version}"),
            "published_at": release_info.get("published_at"),
        }


# ---------------------------------------------------------------- 설치 후 사후 검증

async def verify_installation(app: str, bin_path: Path) -> tuple[bool, str]:
    """설치 후 --version 및 MCP initialize 핸드셰이크 사후 검증."""
    if not bin_path.is_file() or not os.access(bin_path, os.X_OK):
        return False, f"바이너리 실행 권한이 없습니다: {bin_path}"

    # 1. --version 검증
    try:
        proc = await asyncio.create_subprocess_exec(
            str(bin_path), "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=artcraft._env(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=VERIFY_TIMEOUT)
        output = stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            return False, f"CLI --version 종료 코드 오류 (rc={proc.returncode}): {output}"
    except TimeoutError:
        return False, f"--version 실행 시간 초과 ({int(VERIFY_TIMEOUT)}초)"
    except Exception as exc:  # noqa: BLE001
        return False, f"--version 실행 중 예외: {exc}"

    # 2. MCP stdio initialize 핸드셰이크 검증
    try:
        mcp_proc = await asyncio.create_subprocess_exec(
            str(bin_path), "mcp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=artcraft._env(),
        )
        init_req = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ttakkari-verify", "version": "1.0"},
            },
        }) + "\n"

        async def _handshake() -> tuple[bool, str | None]:
            assert mcp_proc.stdin is not None
            assert mcp_proc.stdout is not None
            try:
                mcp_proc.stdin.write(init_req.encode("utf-8"))
                await mcp_proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return False, "MCP 파이프가 즉시 닫혔습니다."

            while True:
                line = await mcp_proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", "replace").strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                    if data.get("id") == 1 and ("result" in data or "error" not in data):
                        return True, None
                    if "error" in data:
                        return False, f"MCP 에러 응답: {data['error']}"
                except json.JSONDecodeError:
                    continue
            return False, "MCP 서버가 initialize 응답 없이 종료되었습니다."

        try:
            mcp_ok, mcp_err = await asyncio.wait_for(_handshake(), timeout=VERIFY_TIMEOUT)
            if not mcp_ok:
                return False, f"MCP 핸드셰이크 실패: {mcp_err}"
        finally:
            if mcp_proc.returncode is None:
                try:
                    if mcp_proc.stdin and not mcp_proc.stdin.is_closing():
                        mcp_proc.stdin.close()
                    mcp_proc.terminate()
                    await asyncio.wait_for(mcp_proc.wait(), timeout=2.0)
                except (TimeoutError, ProcessLookupError, Exception):
                    try:
                        mcp_proc.kill()
                    except Exception:
                        pass
    except Exception as exc:  # noqa: BLE001
        return False, f"MCP 핸드셰이크 예외: {exc}"

    return True, output


# ---------------------------------------------------------------- 원자적 교체 및 롤백

def _get_backup_dir(app: str) -> Path:
    return artcraft.root() / "dist" / "cli" / app / ".backup"


def _get_backup_meta(app: str) -> dict[str, Any] | None:
    meta_path = _get_backup_dir(app) / "backup_meta.json"
    if meta_path.is_file():
        try:
            return json.loads(meta_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
    return None


async def apply_update(
    app: str,
    tag: str | None = None,
    confirm: bool = False,
    current_job_id: str | None = None,
) -> dict[str, Any]:
    """앱별 업데이트 적용: Busy 검사 -> 다운로드 -> 해제 -> 백업 보존 -> 원자적 스왑 -> 사후 검증 -> 실패 시 자동 원복."""
    validate_app(app)
    if not confirm:
        raise ArtcraftError(428, "confirm_required", "업데이트 적용을 확인해야 합니다 (confirm=true).")

    await check_busy(app, exclude_job_id=current_job_id)

    release_data = await fetch_release_assets(app, tag)
    new_version = release_data["version"]
    zip_bytes = release_data["zip_bytes"]

    root = artcraft.root()
    app_dist_dir = root / "dist" / "cli" / app
    app_dist_dir.mkdir(parents=True, exist_ok=True)
    symlink_path = root / "bin" / f"{app}-cli"

    previous_version = None
    previous_target: Path | None = None
    if symlink_path.is_symlink():
        try:
            previous_target = symlink_path.resolve()
            match = re.search(r"\d+\.\d+\.\d+", str(previous_target))
            if match:
                previous_version = match.group(0)
        except OSError:
            pass

    # 1. 이전 바이너리 영구 백업 보존 (동일 버전 재설치나 경로 덮어쓰기 시 삭제 방지)
    backup_dir = _get_backup_dir(app)
    backup_dir.mkdir(parents=True, exist_ok=True)
    preserved_backup_bin: Path | None = None

    if previous_target and previous_target.is_file():
        backup_name = f"{app}-cli-{previous_version}" if previous_version else f"{app}-cli-backup"
        preserved_backup_bin = backup_dir / backup_name
        try:
            shutil.copy2(previous_target, preserved_backup_bin)
            preserved_backup_bin.chmod(preserved_backup_bin.stat().st_mode | 0o755)
            meta = {
                "app": app,
                "version": previous_version,
                "target_binary": str(preserved_backup_bin),
                "original_target": str(previous_target),
                "backed_up_at": _now(),
            }
            (backup_dir / "backup_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("백업 바이너리 복사 실패: %s", exc)

    # 2. Staging 디렉터리에 압축 해제
    job_uuid = uuid.uuid4().hex[:8]
    staging_dir = app_dist_dir / f".staging-{job_uuid}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    final_dest: Path | None = None
    backup_old_dest: Path | None = None

    try:
        safe_extract_zip(zip_bytes, staging_dir)

        extracted_dirs = [p for p in staging_dir.iterdir() if p.is_dir()]
        extracted_dir = extracted_dirs[0] if extracted_dirs else staging_dir
        binary_candidates = [
            extracted_dir / f"{app}-cli",
            extracted_dir / app,
        ]
        target_binary = next((b for b in binary_candidates if b.is_file()), None)
        if not target_binary:
            for p in staging_dir.rglob(f"{app}-cli"):
                if p.is_file():
                    target_binary = p
                    break

        if not target_binary:
            raise ArtcraftError(500, "corrupt_package", f"압축 파일 내에서 실행 바이너리({app}-cli)를 찾을 수 없습니다.")

        target_binary.chmod(target_binary.stat().st_mode | 0o755)

        # 3. Staging -> 정규 dist/cli/<app>/... 이동 준비
        final_dir_name = extracted_dir.name
        if final_dir_name == staging_dir.name:
            final_dir_name = f"{app}-cli-{new_version}-macos-universal"
        final_dest = app_dist_dir / final_dir_name

        # 동일 버전 재설치 방어: final_dest가 이미 존재하면 즉시 지우지 않고 임시로 보존 이동
        if final_dest.exists():
            backup_old_dest = app_dist_dir / f".prev-{final_dir_name}-{job_uuid}"
            shutil.move(str(final_dest), str(backup_old_dest))

        if extracted_dir != staging_dir:
            shutil.move(str(extracted_dir), str(final_dest))
        else:
            final_dest.mkdir(parents=True, exist_ok=True)
            for item in staging_dir.iterdir():
                shutil.move(str(item), str(final_dest / item.name))

        new_binary_path = final_dest / f"{app}-cli"
        if not new_binary_path.exists():
            for p in final_dest.rglob(f"{app}-cli"):
                new_binary_path = p
                break
        if not new_binary_path:
            raise ArtcraftError(500, "corrupt_package", "설치 대상 바이너리를 찾을 수 없습니다.")
        new_binary_path.chmod(new_binary_path.stat().st_mode | 0o755)

        # 4. 원자적 심링크 스왑
        tmp_symlink = root / "bin" / f".tmp-symlink-{app}-{job_uuid}"
        if tmp_symlink.exists() or tmp_symlink.is_symlink():
            tmp_symlink.unlink()
        tmp_symlink.symlink_to(new_binary_path)
        os.replace(tmp_symlink, symlink_path)

        # 5. 사후 검증 (--version & MCP 핸드셰이크)
        ok, verify_msg = await verify_installation(app, symlink_path)
        if not ok:
            logger.error("사후 검증 실패로 즉시 원복을 시도합니다: %s", verify_msg)
            # 심링크 즉시 복구 (원래 이전 타깃 우선, 없으면 백업 바이너리)
            restore_target = previous_target if (previous_target and previous_target.exists()) else preserved_backup_bin
            if restore_target and restore_target.exists():
                tmp_rb = root / "bin" / f".tmp-rb-{app}-{job_uuid}"
                if tmp_rb.exists() or tmp_rb.is_symlink():
                    tmp_rb.unlink()
                tmp_rb.symlink_to(restore_target)
                os.replace(tmp_rb, symlink_path)

            # 디렉터리 복원
            if final_dest.exists():
                shutil.rmtree(final_dest, ignore_errors=True)
            if backup_old_dest and backup_old_dest.exists():
                shutil.move(str(backup_old_dest), str(final_dest))
                backup_old_dest = None

            raise ArtcraftError(500, "verification_failed", f"설치 후 검증 실패로 원복되었습니다: {verify_msg}")

        # 검증 통과 완료 시 임시 보존해둔 이전 디렉터리 정리
        if backup_old_dest and backup_old_dest.exists():
            shutil.rmtree(backup_old_dest, ignore_errors=True)
            backup_old_dest = None

        # 상태 영속화 (성공)
        record = {
            "job_id": current_job_id or job_uuid,
            "action": "update-apply",
            "from_version": previous_version,
            "to_version": new_version,
            "version": new_version,
            "status": "succeeded",
            "error": None,
            "at": _now(),
        }

        def mutate_success(state: dict[str, Any]) -> None:
            updates = state.setdefault("updates", {})
            history = updates.setdefault(app, {}).setdefault("history", [])
            history.append(record)
            updates[app].update({
                "state": "idle",
                "current_version": new_version,
                "previous_version": previous_version,
                "last_status": record,
                "last_update": record,
                "current_job_id": None,
            })

        artcraft.update_state(mutate_success)
        artcraft.reset_caches()

        return {
            "success": True,
            "app": app,
            "version": new_version,
            "previous_version": previous_version,
            "verified_output": verify_msg,
        }

    except Exception as exc:
        # 실패 시 기존 설치 보존 복구
        if backup_old_dest and backup_old_dest.exists():
            if final_dest and final_dest.exists():
                shutil.rmtree(final_dest, ignore_errors=True)
            shutil.move(str(backup_old_dest), str(final_dest))

        restore_target = previous_target if (previous_target and previous_target.exists()) else preserved_backup_bin
        if restore_target and restore_target.exists():
            tmp_rb = root / "bin" / f".tmp-rb-{app}-{job_uuid}"
            if tmp_rb.exists() or tmp_rb.is_symlink():
                tmp_rb.unlink()
            tmp_rb.symlink_to(restore_target)
            os.replace(tmp_rb, symlink_path)

        fail_msg = exc.message if isinstance(exc, ArtcraftError) else str(exc)
        fail_record = {
            "job_id": current_job_id or job_uuid,
            "action": "update-apply",
            "from_version": previous_version,
            "to_version": new_version if 'new_version' in locals() else None,
            "status": "failed",
            "error": fail_msg,
            "at": _now(),
        }

        def mutate_failure(state: dict[str, Any]) -> None:
            updates = state.setdefault("updates", {})
            history = updates.setdefault(app, {}).setdefault("history", [])
            history.append(fail_record)
            updates[app].update({
                "state": "idle",
                "last_status": fail_record,
                "last_update": fail_record,
                "current_job_id": None,
            })

        with artcraft.contextlib.suppress(Exception):
            artcraft.update_state(mutate_failure)

        raise

    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if backup_old_dest and backup_old_dest.exists():
            shutil.rmtree(backup_old_dest, ignore_errors=True)


async def rollback_update(
    app: str,
    confirm: bool = False,
    current_job_id: str | None = None,
) -> dict[str, Any]:
    """보존된 이전 버전으로 수동 롤백."""
    validate_app(app)
    if not confirm:
        raise ArtcraftError(428, "confirm_required", "롤백 적용을 확인해야 합니다 (confirm=true).")

    await check_busy(app, exclude_job_id=current_job_id)

    backup_meta = _get_backup_meta(app)
    if not backup_meta or not backup_meta.get("target_binary"):
        raise ArtcraftError(404, "no_backup_available", "복구 가능한 이전 백업이 없습니다.")

    target_binary = None
    orig = backup_meta.get("original_target")
    if orig and Path(orig).is_file():
        target_binary = Path(orig)
    elif backup_meta.get("target_binary") and Path(backup_meta["target_binary"]).is_file():
        target_binary = Path(backup_meta["target_binary"])

    if not target_binary:
        raise ArtcraftError(404, "no_backup_available", "백업된 바이너리가 존재하지 않습니다.")

    root = artcraft.root()
    symlink_path = root / "bin" / f"{app}-cli"
    job_uuid = uuid.uuid4().hex[:8]

    tmp_symlink = root / "bin" / f".tmp-rb-{app}-{job_uuid}"
    if tmp_symlink.exists() or tmp_symlink.is_symlink():
        tmp_symlink.unlink()
    tmp_symlink.symlink_to(target_binary)
    os.replace(tmp_symlink, symlink_path)

    ok, verify_msg = await verify_installation(app, symlink_path)
    if not ok:
        raise ArtcraftError(500, "verification_failed", f"롤백 후 검증 실패: {verify_msg}")

    restored_version = backup_meta.get("version")
    record = {
        "job_id": current_job_id or job_uuid,
        "action": "update-rollback",
        "to_version": restored_version,
        "restored_version": restored_version,
        "status": "succeeded",
        "error": None,
        "at": _now(),
    }

    def mutate_rollback_success(state: dict[str, Any]) -> None:
        updates = state.setdefault("updates", {})
        history = updates.setdefault(app, {}).setdefault("history", [])
        history.append(record)
        updates[app].update({
            "state": "idle",
            "current_version": restored_version,
            "last_status": record,
            "last_update": record,
            "current_job_id": None,
        })

    artcraft.update_state(mutate_rollback_success)
    artcraft.reset_caches()

    return {
        "success": True,
        "app": app,
        "restored_version": restored_version,
        "verified_output": verify_msg,
    }


# ---------------------------------------------------------------- 상태 조회 및 거짓 성공 방지

def reconcile_stale_updates() -> None:
    """서버 재시작 시 비정상 종료된 작업을 감지하여 'interrupted'로 정정 (거짓 성공 방지)."""
    def mutate(state: dict[str, Any]) -> None:
        updates = state.get("updates") or {}
        for app_name, app_state in updates.items():
            if isinstance(app_state, dict) and app_state.get("state") in ("running", "queued"):
                job_id = app_state.get("current_job_id")
                # active JOBS에 해당 job_id가 없으면 서버 재시작으로 유실된 것으로 판단
                if not job_id or job_id not in artcraft.JOBS or not artcraft.JOBS[job_id].active:
                    rec = {
                        "job_id": job_id,
                        "action": app_state.get("action", "update"),
                        "status": "interrupted",
                        "error": "서버 재시작으로 작업이 중단되었습니다.",
                        "at": _now(),
                    }
                    history = app_state.setdefault("history", [])
                    history.append(rec)
                    app_state["state"] = "interrupted"
                    app_state["error"] = "서버 재시작으로 작업이 중단되었습니다."
                    app_state["last_status"] = rec
                    app_state["last_update"] = rec
                    app_state["current_job_id"] = None
                    app_state["finished_at"] = _now()

    artcraft.update_state(mutate)


def get_update_state(app: str) -> dict[str, Any]:
    """앱별 업데이트 영속 상태 조회 (API 계약 준수)."""
    validate_app(app)
    reconcile_stale_updates()

    state = artcraft.load_state()
    app_data = state.get("updates", {}).get(app) or {}

    symlink_path = artcraft.root() / "bin" / f"{app}-cli"
    installed_ver = None
    if symlink_path.is_symlink():
        try:
            target = symlink_path.resolve()
            match = re.search(r"\d+\.\d+\.\d+", str(target))
            if match:
                installed_ver = match.group(0)
        except OSError:
            pass

    last_st = app_data.get("last_status") or app_data.get("last_update")
    return {
        "app": app,
        "current_version": app_data.get("current_version") or installed_ver,
        "previous_version": app_data.get("previous_version"),
        "state": app_data.get("state", "idle"),
        "error": app_data.get("error"),
        "last_status": last_st,
        "history": app_data.get("history", []),
    }


async def get_app_update_info(app: str, refresh: bool = False) -> dict[str, Any]:
    """앱별 업데이트 미리보기 정보 반환.
    
    refresh=True 시 원격 최신 릴리스 조회를 즉시 갱신하고 GUI 실행 여부를 정밀 검사한다.
    """
    validate_app(app)
    reconcile_stale_updates()

    reg = artcraft.registry().get(app, {})

    # refresh=True 시 원격 릴리스 정보 갱신
    if refresh:
        try:
            latest_asset = await fetch_release_assets(app)
            new_tag = latest_asset.get("tag")

            def mutate_remote(s: dict[str, Any]) -> None:
                remote_data = s.setdefault("remote", {})
                remote_data["checked_at"] = _now()
                apps_remote = remote_data.setdefault("apps", {})
                apps_remote[app] = {
                    "tag": new_tag,
                    "published_at": latest_asset.get("published_at"),
                }

            artcraft.update_state(mutate_remote)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 최신 릴리스 갱신 실패 (기존 캐시 사용): %s", app, exc)

    state = artcraft.load_state()
    remote = state.get("remote") or {}
    latest_info = (remote.get("apps") or {}).get(app) or {}
    tag = latest_info.get("tag")

    symlink_path = artcraft.root() / "bin" / f"{app}-cli"
    installed_ver = None
    cli_version_line = None
    if symlink_path.is_symlink():
        try:
            target = symlink_path.resolve()
            match = re.search(r"\d+\.\d+\.\d+", str(target))
            if match:
                installed_ver = match.group(0)
                cli_version_line = f"{app}-cli {installed_ver}"
        except OSError:
            pass
    if not installed_ver:
        installed_ver = reg.get("ver")
        cli_version_line = f"{app}-cli {installed_ver}" if installed_ver else None

    backup_meta = _get_backup_meta(app)
    can_rollback = bool(backup_meta and backup_meta.get("target_binary"))
    prev_version = backup_meta.get("version") if backup_meta else None

    is_busy = False
    busy_reason = None

    # 1. 활성 Job 검사
    for job in artcraft.JOBS.values():
        if job.active and (app in job.apps or (job.kind == "smoke" and job.mode == "full")):
            is_busy = True
            busy_reason = f"작업 진행 중: {job.action}"
            break

    # 2. GUI 실행 중 검사 (raw_status 확인)
    if not is_busy:
        try:
            status, _, _ = await artcraft.raw_status(force=bool(refresh))
            for a in status:
                if a.get("app") == app:
                    gui = a.get("gui") or {}
                    if gui.get("running") or gui.get("listening"):
                        is_busy = True
                        busy_reason = f"{app} 앱이 현재 실행 중입니다."
                        break
        except Exception:
            pass

    target_ver = validate_version(tag) if tag else None
    update_avail = artcraft.update_available(installed_ver, target_ver) if target_ver else False

    latest_job = next(
        (j.public() for j in reversed(list(artcraft.JOBS.values())) if app in j.apps and j.kind == "update"),
        None,
    )

    return {
        "app": app,
        "title": reg.get("title", app),
        "installed_version": installed_ver,
        "cli_version_line": cli_version_line,
        "latest_release": {
            "tag": tag,
            "version": target_ver,
            "published_at": latest_info.get("published_at"),
            "asset_name": f"{app}-cli-{target_ver}-macos-universal.zip" if target_ver else None,
        } if tag else None,
        "update_available": update_avail,
        "can_update": bool(not is_busy and target_ver),
        "can_rollback": can_rollback,
        "previous_version": prev_version,
        "busy": is_busy,
        "busy_reason": busy_reason,
        "latest_job": latest_job,
    }


# ---------------------------------------------------------------- Job 연동

def start_update_job(app: str, tag: str | None, confirm: bool) -> artcraft.Job:
    """비동기 업데이트 작업 등록 및 실행."""
    validate_app(app)
    if not confirm:
        raise ArtcraftError(428, "confirm_required", "업데이트 적용을 확인해야 합니다 (confirm=true).")

    if any(j.active and (app in j.apps or (j.kind == "smoke" and j.mode == "full")) for j in artcraft.JOBS.values()):
        raise ArtcraftError(409, "busy", "다른 작업이 진행 중입니다.")

    async def runner(job: artcraft.Job) -> None:
        # 상태 파일에 running 영속화
        def mark_running(state: dict[str, Any]) -> None:
            updates = state.setdefault("updates", {})
            app_entry = updates.setdefault(app, {})
            app_entry.update({
                "state": "running",
                "current_job_id": job.id,
                "action": job.action,
                "target_version": tag,
                "started_at": _now(),
            })

        artcraft.update_state(mark_running)

        try:
            res = await apply_update(app, tag=tag, confirm=True, current_job_id=job.id)
            job.state = "succeeded"
            job.summary = f"{app} {res['version']} 업데이트 성공"
            job.output = res.get("verified_output", "")
        except ArtcraftError as exc:
            job.state = "failed"
            job.summary = exc.message
            job.output = exc.message
            raise
        except Exception as exc:
            job.state = "failed"
            job.summary = str(exc)
            job.output = str(exc)
            raise

    job = artcraft._start("update", "update-apply", [app], "apply", runner)

    # Job 등록 직후에도 queued 상태 영속화
    def mark_queued(state: dict[str, Any]) -> None:
        updates = state.setdefault("updates", {})
        app_entry = updates.setdefault(app, {})
        if app_entry.get("state") != "running":
            app_entry.update({
                "state": "queued",
                "current_job_id": job.id,
                "action": job.action,
                "target_version": tag,
                "created_at": _now(),
            })

    artcraft.update_state(mark_queued)
    return job


def start_rollback_job(app: str, confirm: bool) -> artcraft.Job:
    """비동기 롤백 작업 등록 및 실행."""
    validate_app(app)
    if not confirm:
        raise ArtcraftError(428, "confirm_required", "롤백 적용을 확인해야 합니다 (confirm=true).")

    if any(j.active and (app in j.apps or (j.kind == "smoke" and job.mode == "full")) for j in artcraft.JOBS.values()):
        raise ArtcraftError(409, "busy", "다른 작업이 진행 중입니다.")

    async def runner(job: artcraft.Job) -> None:
        def mark_running(state: dict[str, Any]) -> None:
            updates = state.setdefault("updates", {})
            app_entry = updates.setdefault(app, {})
            app_entry.update({
                "state": "running",
                "current_job_id": job.id,
                "action": job.action,
                "started_at": _now(),
            })

        artcraft.update_state(mark_running)

        try:
            res = await rollback_update(app, confirm=True, current_job_id=job.id)
            job.state = "succeeded"
            job.summary = f"{app} {res['restored_version']} 롤백 성공"
            job.output = res.get("verified_output", "")
        except ArtcraftError as exc:
            job.state = "failed"
            job.summary = exc.message
            job.output = exc.message
            raise
        except Exception as exc:
            job.state = "failed"
            job.summary = str(exc)
            job.output = str(exc)
            raise

    job = artcraft._start("update", "update-rollback", [app], "rollback", runner)

    def mark_queued(state: dict[str, Any]) -> None:
        updates = state.setdefault("updates", {})
        app_entry = updates.setdefault(app, {})
        if app_entry.get("state") != "running":
            app_entry.update({
                "state": "queued",
                "current_job_id": job.id,
                "action": job.action,
                "created_at": _now(),
            })

    artcraft.update_state(mark_queued)
    return job
