"""호스트 실측 탐침. 설치 여부·버전·lab 기록·스킬 초안 상태를 스냅샷 JSON으로 남긴다.

서버를 띄우거나 모델을 로드하지 않는다. 파일 존재, 패키지 버전, 짧은 버전 명령만 실행한다.
실행: python -m ttakkari.creative.v2.probe --lab-root <lab> --out <snapshot.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COMFY_MODEL_FILES: dict[str, list[str]] = {
    "flux2_klein_4b": [
        "diffusion_models/flux-2-klein-4b.safetensors",
        "text_encoders/qwen_3_4b.safetensors",
        "vae/flux2-vae.safetensors",
    ],
    "qwen_image_edit_2511": [
        "diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors",
        "text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "vae/qwen_image_vae.safetensors",
        "loras/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
    ],
    "wan22_ti2v_5b": [
        "diffusion_models/wan2.2_ti2v_5B_fp16.safetensors",
        "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "vae/wan2.2_vae.safetensors",
    ],
    "wan22_i2v_14b": [
        "diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors",
        "diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors",
        "loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors",
        "loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors",
        "vae/wan_2.1_vae.safetensors",
    ],
    "ltx23": ["checkpoints/ltx-2.3-22b-dev-fp8.safetensors"],
    "hunyuanvideo15": [
        "diffusion_models/hunyuanvideo1.5_720p_t2v_fp16.safetensors",
        "vae/hunyuanvideo15_vae_fp16.safetensors",
    ],
    "seedvr2": [
        "diffusion_models/seedvr2_3b_int8_convrot.safetensors",
        "vae/seedvr2_ema_vae_fp16.safetensors",
    ],
    "film_net": ["frame_interpolation/film_net_fp16.safetensors"],
}

PROBE_IDS: tuple[str, ...] = (
    "node",
    "pnpm",
    "ffmpeg_system",
    "ffmpeg_imageio",
    "blender_app",
    "bpy",
    "godot",
    "remotion",
    "react",
    "three",
    "playwright_core",
    "r3f",
    "chrome",
    "comfyui",
    "rhubarb",
    "artcraft_apps",
    "acestep",
    "supertonic_runtime",
    "local_stt_code",
    "repo_render_py",
    *(f"comfy:{k}" for k in COMFY_MODEL_FILES),
)

CODE_SUFFIXES = {".py", ".mjs", ".js", ".ts", ".sh"}


def _run(cmd: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"
    return r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[-1] if (
        r.stdout or r.stderr
    ).strip() else ""


def _pkg_version(path: Path) -> str | None:
    try:
        return json.loads(path.read_text())["version"]
    except (OSError, ValueError, KeyError):
        return None


def _probe(
    installed: bool, version: str | None = None, detail: str = ""
) -> dict[str, Any]:
    # 홈 경로는 스냅샷에 남기지 않는다.
    return {
        "installed": installed,
        "version": version,
        "detail": detail.replace(str(Path.home()), "~"),
    }


def probe_host(
    lab: Path, repo: Path, app_root: Path, artcraft_root: Path
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    node = shutil.which("node")
    ok, text = _run([node, "-v"]) if node else (False, "node 없음")
    out["node"] = _probe(ok, text.lstrip("v") if ok else None, node or text)
    pnpm = shutil.which("pnpm")
    ok, text = _run([pnpm, "-v"]) if pnpm else (False, "pnpm 없음")
    out["pnpm"] = _probe(ok, text if ok else None, pnpm or text)
    ff = shutil.which("ffmpeg")
    out["ffmpeg_system"] = _probe(bool(ff), None, ff or "PATH에 ffmpeg 없음")
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        ok, text = _run([exe, "-version"], 20)
        out["ffmpeg_imageio"] = _probe(
            ok, (re.search(r"version (\S+)", text) or [None, None])[1], exe
        )
    except Exception as e:  # noqa: BLE001 - 탐침은 어떤 실패도 '미설치' 증거로 기록한다
        out["ffmpeg_imageio"] = _probe(False, None, f"{type(e).__name__}: {e}")
    blender = shutil.which("blender") or next(
        (str(p) for p in Path("/Applications").glob("Blender*.app")), None
    )
    out["blender_app"] = _probe(bool(blender), None, blender or "Blender GUI 앱 없음")
    bpy_python = lab / "3d/.venv-bpy/bin/python"
    if bpy_python.is_file():
        ok, text = _run(
            [str(bpy_python), "-c", "import bpy;print(bpy.app.version_string)"], 120
        )
        out["bpy"] = _probe(ok, text if ok else None, str(bpy_python))
    else:
        out["bpy"] = _probe(False, None, "bpy venv 없음")
    godot = shutil.which("godot") or next(
        (str(p) for p in Path("/Applications").glob("Godot*.app")), None
    )
    out["godot"] = _probe(bool(godot), None, godot or "Godot 없음")
    remotion_modules = lab / "remotion/project/node_modules"
    out["remotion"] = _probe(
        (remotion_modules / "remotion/package.json").is_file(),
        _pkg_version(remotion_modules / "remotion/package.json"),
        str(remotion_modules),
    )
    out["react"] = _probe(
        (remotion_modules / "react/package.json").is_file(),
        _pkg_version(remotion_modules / "react/package.json"),
        "",
    )
    three_modules = lab / "3d/node_modules"
    out["three"] = _probe(
        (three_modules / "three/package.json").is_file(),
        _pkg_version(three_modules / "three/package.json"),
        str(three_modules),
    )
    out["playwright_core"] = _probe(
        (three_modules / "playwright-core/package.json").is_file(),
        _pkg_version(three_modules / "playwright-core/package.json"),
        "",
    )
    r3f = next(
        (
            p
            for p in (
                three_modules / "@react-three/fiber/package.json",
                remotion_modules / "@react-three/fiber/package.json",
            )
            if p.is_file()
        ),
        None,
    )
    out["r3f"] = _probe(
        bool(r3f),
        _pkg_version(r3f) if r3f else None,
        str(r3f or "@react-three/fiber 없음"),
    )
    chrome = Path("/Applications/Google Chrome.app")
    out["chrome"] = _probe(chrome.is_dir(), None, str(chrome))
    version_py = lab / "comfyui/ComfyUI/comfyui_version.py"
    version = None
    if version_py.is_file():
        found = re.search(r'__version__\s*=\s*"([^"]+)"', version_py.read_text())
        version = found.group(1) if found else None
    out["comfyui"] = _probe(version_py.is_file(), version, str(version_py.parent))
    rhubarb = next((lab / "3d/tools").glob("Rhubarb-Lip-Sync-*/rhubarb"), None)
    out["rhubarb"] = _probe(
        bool(rhubarb and rhubarb.is_file()),
        (re.search(r"Sync-(\S+?)-macOS", str(rhubarb)) or [None, None])[1]
        if rhubarb
        else None,
        str(rhubarb or ""),
    )
    apps = (
        sorted(p.name for p in (artcraft_root / "dist/cli").glob("*"))
        if (artcraft_root / "dist/cli").is_dir()
        else []
    )
    out["artcraft_apps"] = _probe(
        bool(apps), None, f"{len(apps)}종: " + ", ".join(apps)
    )
    out["acestep"] = _probe(
        (lab / "audio/refs/ACE-Step-1.5").is_dir(),
        None,
        str(lab / "audio/refs/ACE-Step-1.5"),
    )
    supertonic = app_root / ".tts-runtime/supertonic"
    out["supertonic_runtime"] = _probe(supertonic.is_dir(), None, str(supertonic))
    out["local_stt_code"] = _probe(
        (repo / "src/ttakkari/meetings/local_stt").is_dir(),
        None,
        "src/ttakkari/meetings/local_stt",
    )
    out["repo_render_py"] = _probe(
        (repo / "src/ttakkari/creative/render.py").is_file(),
        None,
        "src/ttakkari/creative/render.py",
    )
    models = lab / "comfyui/ComfyUI/models"
    for key, files in COMFY_MODEL_FILES.items():
        missing = [f for f in files if not (models / f).is_file()]
        out[f"comfy:{key}"] = _probe(
            not missing,
            None,
            "모든 파일 존재" if not missing else "없음: " + ", ".join(missing),
        )
    return out


def probe_records(lab: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(lab.glob("*/records/*/record.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        review = data.get("review") or {}
        records[path.parent.name] = {
            "status": data.get("status"),
            "kind": data.get("kind"),
            "title": data.get("title"),
            "verdict": review.get("verdict"),
            "independent": bool(review.get("independent")),
            "media_count": len(data.get("media") or []),
            "path": str(path.relative_to(lab)),
        }
    return records


def probe_skills(lab: Path) -> dict[str, dict[str, Any]]:
    skills: dict[str, dict[str, Any]] = {}
    for md in sorted(lab.glob("*/skills-draft/*/SKILL.md")):
        folder = md.parent
        files = sorted(
            str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()
        )
        text = md.read_text(errors="replace")
        io = re.search(r"##[^\n]*(?:입력|Input)[^\n]*\n(.{0,500})", text, re.DOTALL)
        skills[folder.name] = {
            "path": str(folder.relative_to(lab)),
            "skill_md_sha256": hashlib.sha256(md.read_bytes()).hexdigest(),
            "file_count": len(files),
            "has_code": any(
                Path(f).suffix in CODE_SUFFIXES or f.endswith("workflow.api.json")
                for f in files
            ),
            "io_excerpt": io.group(1).strip() if io else "",
            "files": files[:30],
        }
    return skills


def build_snapshot(
    lab: Path, repo: Path, app_root: Path, artcraft_root: Path, command: str
) -> dict[str, Any]:
    return {
        "schema": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "command": command.replace(str(Path.home()), "~"),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
        },
        "probes": probe_host(lab, repo, app_root, artcraft_root),
        "records": probe_records(lab),
        "skills": probe_skills(lab),
    }


def main() -> None:
    home = Path.home() / "development/projects"
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab-root", type=Path, default=home / "ttakkari-studio-lab")
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[4]
    )
    parser.add_argument(
        "--app-root", type=Path, default=home / "winterholic-ttakkari/back"
    )
    parser.add_argument("--artcraft-root", type=Path, default=home / "artcraft")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    snapshot = build_snapshot(
        args.lab_root,
        args.repo_root,
        args.app_root,
        args.artcraft_root,
        " ".join(sys.argv),
    )
    args.out.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    installed = sum(1 for p in snapshot["probes"].values() if p["installed"])
    print(
        f"probes {installed}/{len(snapshot['probes'])} installed, records {len(snapshot['records'])}, skills {len(snapshot['skills'])}"
    )


if __name__ == "__main__":
    main()
