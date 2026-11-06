"""HyperFrames 엔진 어댑터와 렌더 모듈.

HTML/CSS/GSAP 기반의 타임라인 애니메이션을 무헤드 Chrome 및 FFmpeg 파이프라인으로
결정론적(deterministic) 렌더링하는 공식 HyperFrames 런타임 연동 모듈이다.

소유권: 신규 src/ttakkari/creative/v2/hyperframes.py
(engines.py/dispatch.py는 총괄 통합 담당자 소유이며, 이 모듈은 EngineAdapter 프로토콜 및
표준 세그먼트 디스패치 계약을 제공한다).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from ttakkari.agents.browser_preflight import inspect_permissions
from ttakkari.creative.render import ffmpeg
from ttakkari.creative.v2.engines import Limits, Preflight, qa_media

log = logging.getLogger("ttakkari.creative.v2.hyperframes")

DEFAULT_CHROME_MAC = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def find_browser() -> str | None:
    """HyperFrames 렌더러가 사용할 Chrome/Chromium 무헤드 브라우저 경로."""
    env_path = os.environ.get("HYPERFRAMES_BROWSER_PATH")
    if env_path and Path(env_path).is_file():
        return env_path
    if DEFAULT_CHROME_MAC.is_file():
        return str(DEFAULT_CHROME_MAC)
    which_chrome = shutil.which("google-chrome") or shutil.which("chromium")
    return which_chrome


def find_ffprobe() -> str | None:
    """ffprobe 실행 파일 경로."""
    env_path = os.environ.get("HYPERFRAMES_FFPROBE_PATH")
    if env_path and Path(env_path).is_file():
        return env_path
    user_bin = Path("/Users/winterholic/bin/ffprobe")
    if user_bin.is_file() and os.access(user_bin, os.X_OK):
        return str(user_bin)
    return shutil.which("ffprobe")


class HyperFramesAdapter:
    """HyperFrames v0.9.x 공식 엔진 어댑터 (EngineAdapter 프로토콜 구현)."""

    id = "skill.hyperframes"
    providers = frozenset({"skill.hyperframes", "tool.hyperframes", "tool.html_motion"})

    limits = Limits(
        aspects=("16:9",),
        min_scenes=3,
        max_scenes=3,
        max_scene_seconds=60.0,
        max_segment_seconds=300.0,
        min_segment_seconds=1.0,
    )

    style_params: ClassVar[dict[str, tuple[str, ...]]] = {
        # Discovery mirrors the Kafka template's enforced defaults. The
        # template does not implement the additional values previously listed.
        "theme": ("dark",),
        "animation": ("technical",),
        "transition": ("cut",),
        "caption_style": ("bottom_card",),
    }

    def validate_style_dna(self, style_dna: dict[str, Any] | None) -> dict[str, Any]:
        """Core가 선택/dispatch 전에 호출할 Kafka 템플릿 전용 Style DNA 검사."""
        return _style_dna_contract(style_dna, self.style_params)

    def preflight(self) -> Preflight:
        """HyperFrames 렌더링에 필요한 바이너리 및 환경 사전 점검."""
        missing: list[str] = []
        version_str: str | None = None

        # 1. Node.js check
        node = shutil.which("node")
        if not node:
            missing.append("Node.js (>=20)가 PATH에 없습니다.")
        else:
            try:
                res = subprocess.run([node, "--version"], capture_output=True, text=True, check=False)
                node_ver = res.stdout.strip()
                major = int(re.search(r"v(\d+)", node_ver).group(1)) if re.search(r"v(\d+)", node_ver) else 0
                if major < 20:
                    missing.append(f"Node.js 버전({node_ver})이 권장(>=20)보다 낮습니다.")
            except Exception as e:
                missing.append(f"Node.js 실행 실패: {e}")

        # 2. HyperFrames CLI doctor check
        env = {
            **os.environ,
            "HYPERFRAMES_NO_TELEMETRY": "1",
            "HYPERFRAMES_SKIP_SKILLS": "1",
        }
        browser_exe = find_browser()
        if browser_exe:
            env["HYPERFRAMES_BROWSER_PATH"] = browser_exe
        else:
            missing.append("렌더링용 Chrome 브라우저를 찾을 수 없습니다.")

        ffprobe_exe = find_ffprobe()
        if ffprobe_exe:
            env["HYPERFRAMES_FFPROBE_PATH"] = ffprobe_exe

        try:
            ipc = inspect_permissions()
        except Exception as exc:
            ipc = {"status": "unknown", "error": str(exc)}
        if ipc.get("status") not in {"permissions_allowed", "not_applicable"}:
            missing.append(f"HyperFrames browser IPC {ipc.get('status')}; CLI doctor/render not attempted")
        else:
            try:
                res = subprocess.run(
                    ["npx", "-y", "hyperframes", "doctor", "--json"],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=15,
                    check=False,
                )
                if res.returncode == 0:
                    doc = json.loads(res.stdout)
                    version_str = f"hyperframes {doc.get('_meta', {}).get('version', 'unknown')}"
                    essential = {"Version", "CLI binary", "Node.js", "FFmpeg", "Chrome"}
                    failed_essential = [
                        c["name"] for c in doc.get("checks", [])
                        if c["name"] in essential and not c.get("ok")
                    ]
                    if failed_essential:
                        missing.append(f"HyperFrames 필수 검사 실패: {', '.join(failed_essential)}")
                else:
                    missing.append(f"HyperFrames doctor 응답 오류 (code {res.returncode}): {res.stderr[:200]}")
            except Exception as e:
                missing.append(f"HyperFrames doctor 실행 불가: {e}")

        # 3. FFmpeg check
        try:
            exe = ffmpeg()
            res = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10, check=False)
            if res.returncode != 0:
                missing.append("FFmpeg 실행 파일이 정상 동작하지 않습니다.")
        except Exception as e:
            missing.append(f"FFmpeg 로드 실패: {e}")

        return Preflight(ok=len(missing) == 0, version=version_str, missing=missing)

    def render_segment(
        self,
        spec: dict[str, Any],
        *,
        output_path: Path | str,
        workdir: Path | str,
    ) -> dict[str, Any]:
        """Spec의 콘텐츠를 HyperFrames 프로젝트로 반영한 뒤 실제 CLI 렌더를 요청한다."""
        output = Path(output_path)
        work = Path(workdir)
        if output.exists():
            return _failure("validate", f"출력 파일이 이미 있습니다: {output}")
        try:
            project_dir = _prepare_spec_project(spec, work)
            style_dna_contract = self.validate_style_dna(spec.get("style_dna"))
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return _failure("prepare", str(exc))

        pre = self.preflight()
        if not pre.ok:
            return _failure("preflight", "; ".join(pre.missing), version=pre.version)

        lint = run_lint(project_dir)
        if not lint.get("ok") or lint.get("errorCount", 0):
            return _failure("lint", "HyperFrames lint에서 오류가 발견됐습니다.", lint=lint)

        duration = float(spec["seconds"])
        fps = int(spec.get("fps", 30))
        width = int(spec.get("width", 1920))
        height = int(spec.get("height", 1080))
        options = HyperFramesRenderOptions(quality="delivery", fps=fps, width=width, height=height)
        execution = render_project(project_dir, output, options)
        command = [
            "npx", "-y", "hyperframes", "render", "--quality", options.quality,
            "--fps", str(fps), "--output", str(output),
        ]
        if not execution.get("success"):
            return _failure(
                "render",
                f"HyperFrames 렌더 실패 (exit {execution.get('returncode')}).",
                execution=execution,
                version=pre.version,
                command=command,
            )

        return {
            "success": True,
            "output_path": str(output),
            "duration": duration,
            "fps": fps,
            "width": width,
            "height": height,
            "engine": "hyperframes",
            "version": pre.version,
            "real_execution": True,
            "sha256": _sha256(output) if output.is_file() else None,
            "metrics": {"elapsed_seconds": execution.get("elapsed_seconds")},
            "receipt": {
                "commands": [command],
                "stdout": execution.get("stdout", ""),
                "stderr": execution.get("stderr", ""),
                "lint": lint,
                "project_dir": str(project_dir),
                "scenes": [scene.get("scene_id") for scene in spec["scenes"]],
                "style_dna_contract": style_dna_contract,
            },
        }


ADAPTER = HyperFramesAdapter()


def _failure(stage: str, reason: str, **details: Any) -> dict[str, Any]:
    """자동 대체 없이 실패 단계와 원인을 호출자에게 보존한다."""
    return {"success": False, "engine": "hyperframes", "stage": stage, "error": reason, **details}


def _style_dna_contract(
    style_dna: dict[str, Any] | None,
    style_params: dict[str, tuple[str, ...]],
) -> dict[str, Any] | None:
    """Validate only Style DNA controls with a fixed match in the Kafka template.

    DNA prose and other facets are reported as unapplied. This module does not
    claim that those values affected the rendered composition.
    """
    if style_dna is None:
        return None
    if not isinstance(style_dna, dict):
        raise ValueError("style_dna는 Core ComposeResult object여야 합니다.")
    status = style_dna.get("status")
    if status not in {"resolved", "resolved_pending_confirmation"}:
        raise ValueError(f"Kafka flow는 resolved Style DNA만 받습니다(status={status!r}).")
    dna = style_dna.get("dna")
    if not isinstance(dna, dict):
        raise ValueError("style_dna.dna가 object가 아닙니다.")

    sources = {
        "theme": ("visual_identity", "theme"),
        "animation": ("motion_language", "animation"),
        "transition": ("motion_language", "transition"),
        "caption_style": ("visual_identity", "caption_style"),
    }
    matched: dict[str, str] = {}
    for name, (facet_name, param_name) in sources.items():
        facet = dna.get(facet_name) or {}
        if not isinstance(facet, dict):
            raise ValueError(f"style_dna.dna.{facet_name}는 object여야 합니다.")
        params = facet.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"style_dna.dna.{facet_name}.params는 object여야 합니다.")
        value = params.get(param_name)
        if value is None:
            continue
        allowed = style_params.get(name, ())
        if value not in allowed:
            raise ValueError(
                f"Kafka flow가 소비할 수 없는 Style DNA {facet_name}.params.{param_name}={value!r}; "
                f"지원값: {', '.join(allowed) or '(없음)'}"
            )
        matched[name] = value

    mapped_paths = {
        "visual_identity": {"theme", "caption_style"},
        "motion_language": {"animation", "transition"},
    }
    unapplied: dict[str, Any] = {}
    for facet_name in ("visual_identity", "motion_language"):
        facet = dna.get(facet_name) or {}
        if not isinstance(facet, dict):
            continue
        if facet.get("text"):
            unapplied[f"{facet_name}.text"] = facet["text"]
        params = facet.get("params") or {}
        if isinstance(params, dict):
            for key, value in params.items():
                if key not in mapped_paths[facet_name] and value is not None:
                    unapplied[f"{facet_name}.params.{key}"] = value
    for key, value in dna.items():
        if key not in {*mapped_paths, "style_id", "version"} and value not in (None, "", [], {}):
            unapplied[key] = value

    return {
        "status": "partial_match",
        "sha256": style_dna.get("sha256"),
        "matched_params": matched,
        "unapplied_fields": [
            {"path": path, "value": unapplied[path]} for path in sorted(unapplied)
        ],
    }


def _prepare_spec_project(spec: dict[str, Any], workdir: Path) -> Path:
    """소유 템플릿을 복사하고 spec의 콘텐츠/출력 설정을 실제 DOM에 주입한다."""
    scenes = spec.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != 3:
        raise ValueError("Kafka flow 템플릿은 producer, broker, consumer의 3개 scenes가 필요합니다.")
    seconds = float(spec.get("seconds", 16))
    if not 1 <= seconds <= 300:
        raise ValueError("seconds는 1~300 범위여야 합니다.")
    width, height = int(spec.get("width", 1920)), int(spec.get("height", 1080))
    fps = int(spec.get("fps", 30))
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("width, height, fps는 양수여야 합니다.")
    if width * 9 != height * 16:
        raise ValueError("Kafka flow 템플릿은 16:9 출력만 지원합니다.")
    if any(not isinstance(scene, dict) for scene in scenes):
        raise ValueError("각 scene은 object여야 합니다.")
    supported_style = {"theme": "dark", "animation": "technical", "transition": "cut", "caption_style": "bottom_card"}
    style = spec.get("style") or {}
    if not isinstance(style, dict):
        raise ValueError("style은 object여야 합니다.")
    unsupported_style = [f"{key}={value}" for key, value in style.items() if supported_style.get(key) != value]
    if unsupported_style:
        raise ValueError("Kafka flow 템플릿이 적용하지 않는 style: " + ", ".join(unsupported_style))
    _style_dna_contract(spec.get("style_dna"), ADAPTER.style_params)
    unsupported_scene_fields = {"shots", "camera", "motion", "asset_path", "voice_path", "caption_segments", "strategy"}
    unsupported_scene_fields = sorted({key for scene in scenes for key in unsupported_scene_fields if scene.get(key)})
    if unsupported_scene_fields:
        raise ValueError("Kafka flow 템플릿이 소비하지 않는 scene 입력: " + ", ".join(unsupported_scene_fields))

    repo_root = Path(__file__).resolve().parents[4]
    template_dir = repo_root / "templates" / "hyperframes" / "kafka_message_flow"
    if not (template_dir / "index.html").is_file():
        raise FileNotFoundError(f"HyperFrames template missing: {template_dir}")
    project_dir = workdir / "project"
    if (project_dir / "index.html").exists():
        raise FileExistsError(f"workdir에 기존 HyperFrames 프로젝트가 있습니다: {project_dir}")
    project_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_dir / "index.html", project_dir / "index.html")
    shutil.copytree(template_dir / "vendor", project_dir / "vendor", dirs_exist_ok=True)
    shutil.copy2(template_dir / "audio.wav", project_dir / "audio.wav")

    clean_scenes = []
    for scene in scenes:
        title = str(scene.get("title") or scene.get("caption") or "").strip()
        narration = str(scene.get("narration") or scene.get("caption") or scene.get("prompt") or "").strip()
        if not title or not narration:
            raise ValueError("각 scene에 title/caption 및 narration/caption/prompt가 필요합니다.")
        clean_scenes.append({"title": title, "narration": narration})
    payload = {
        "title": str(spec.get("title") or clean_scenes[0]["title"]),
        "seconds": seconds,
        "width": width,
        "height": height,
        "scenes": clean_scenes,
    }
    encoded = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    html_path = project_dir / "index.html"
    html = html_path.read_text(encoding="utf-8")
    html = html.replace('data-width="1920"', f'data-width="{width}"', 1)
    html = html.replace('data-height="1080"', f'data-height="{height}"', 1)
    html = html.replace('data-duration="16"', f'data-duration="{seconds:g}"', 1)
    html = html.replace('data-fps="30"', f'data-fps="{fps}"', 1)
    html = html.replace(
        '    const tl = gsap.timeline({ paused: true });',
        f'''    const hfSpec = {encoded};
    document.querySelector("#root").style.transform = "scale(" + (hfSpec.width / 1920) + ")";
    document.querySelectorAll(".clip, #bgm").forEach((el) => {{ el.dataset.duration = String(hfSpec.seconds); }});
    document.querySelector(".main-title").textContent = hfSpec.title;
    document.querySelectorAll(".caption-banner").forEach((el, index) => {{
      el.querySelector(".caption-title").textContent = hfSpec.scenes[index].title;
      el.querySelector(".caption-body").textContent = hfSpec.scenes[index].narration;
    }});
    const tl = gsap.timeline({{ paused: true }});''',
        1,
    )
    html = html.replace('    window.__timelines["main"] = tl;', '    tl.timeScale(hfSpec.seconds / 16);\n    window.__timelines["main"] = tl;', 1)
    html_path.write_text(html, encoding="utf-8")

    music_path = spec.get("music_path")
    if music_path:
        music = Path(music_path)
        if not music.is_file():
            raise ValueError(f"music_path 파일이 없습니다: {music}")
        shutil.copy2(music, project_dir / "audio.wav")
    return project_dir


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preflight() -> Preflight:
    return ADAPTER.preflight()


def render(
    spec: dict[str, Any],
    *,
    output_path: Path | str | None = None,
    workdir: Path | str | None = None,
) -> dict[str, Any]:
    if output_path is None:
        return _failure("validate", "output_path는 필수입니다.")
    output = Path(output_path)
    work = Path(workdir) if workdir else output.parent / f"{output.stem}_hyperframes"
    return ADAPTER.render_segment(spec, output_path=output, workdir=work)


render_segment = render


def run_lint(project_dir: Path) -> dict[str, Any]:
    """npx hyperframes lint --json 실행."""
    env = {
        **os.environ,
        "HYPERFRAMES_NO_TELEMETRY": "1",
        "HYPERFRAMES_SKIP_SKILLS": "1",
    }
    res = subprocess.run(
        ["npx", "-y", "hyperframes", "lint", "--json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"ok": res.returncode == 0, "stdout": res.stdout, "stderr": res.stderr}


def run_inspect(project_dir: Path) -> dict[str, Any]:
    """npx hyperframes inspect --json 실행."""
    env = {
        **os.environ,
        "HYPERFRAMES_NO_TELEMETRY": "1",
        "HYPERFRAMES_SKIP_SKILLS": "1",
    }
    res = subprocess.run(
        ["npx", "-y", "hyperframes", "inspect", "--json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"ok": res.returncode == 0, "stdout": res.stdout, "stderr": res.stderr}


def run_snapshot(
    project_dir: Path,
    timestamps: list[float],
    output_dir: Path | None = None,
) -> list[Path]:
    """지정된 타임스탬프에서 스냅샷 프레임 캡처."""
    output_dir = output_dir or (project_dir / "snapshots")
    output_dir.mkdir(parents=True, exist_ok=True)

    browser = find_browser()
    env = {
        **os.environ,
        "HYPERFRAMES_NO_TELEMETRY": "1",
        "HYPERFRAMES_SKIP_SKILLS": "1",
    }
    if browser:
        env["HYPERFRAMES_BROWSER_PATH"] = browser
    ffprobe_exe = find_ffprobe()
    if ffprobe_exe:
        env["HYPERFRAMES_FFPROBE_PATH"] = ffprobe_exe

    ts_arg = ",".join(f"{t:.2f}" for t in timestamps)
    cmd = ["npx", "-y", "hyperframes", "snapshot", "--at", ts_arg, "--output-dir", str(output_dir)]
    subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, env=env, timeout=120, check=False)
    return sorted(list(output_dir.glob("*.png")))


@dataclass
class HyperFramesRenderOptions:
    quality: Literal["draft", "looks", "delivery", "standard", "high"] = "delivery"
    fps: int = 30
    width: int = 1920
    height: int = 1080
    timeout: int = 300
    extra_args: list[str] = field(default_factory=list)


def render_project(
    project_dir: Path,
    output_mp4: Path,
    options: HyperFramesRenderOptions | None = None,
) -> dict[str, Any]:
    """HyperFrames 컴포지션을 MP4로 렌더링하고 실행 메트릭을 반환한다."""
    opt = options or HyperFramesRenderOptions()
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    browser = find_browser()
    env = {
        **os.environ,
        "HYPERFRAMES_NO_TELEMETRY": "1",
        "HYPERFRAMES_SKIP_SKILLS": "1",
    }
    if browser:
        env["HYPERFRAMES_BROWSER_PATH"] = browser
    ffprobe_exe = find_ffprobe()
    if ffprobe_exe:
        env["HYPERFRAMES_FFPROBE_PATH"] = ffprobe_exe

    cmd = [
        "npx", "-y", "hyperframes", "render",
        "--quality", opt.quality,
        "--fps", str(opt.fps),
        "--output", str(output_mp4),
        *opt.extra_args,
    ]

    t0 = time.time()
    res = subprocess.run(
        cmd,
        cwd=project_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=opt.timeout,
        check=False,
    )
    elapsed = time.time() - t0

    success = (res.returncode == 0) and output_mp4.is_file() and (output_mp4.stat().st_size > 0)
    return {
        "success": success,
        "returncode": res.returncode,
        "output_file": str(output_mp4),
        "size_bytes": output_mp4.stat().st_size if output_mp4.is_file() else 0,
        "elapsed_seconds": round(elapsed, 3),
        "stdout": res.stdout,
        "stderr": res.stderr,
    }
