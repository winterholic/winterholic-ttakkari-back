"""독립 Three.js 어댑터: 프레임 결정적 3D 장면(프로필 orbit-scene).

Remotion과 무관하게 정적 서버 + 헤드리스 Chrome으로 `window.renderFrame(i)`를 호출해 PNG 시퀀스를 만들고 FFmpeg로 인코딩한다.
기본 경로는 소프트웨어(SwiftShader) WebGL이라 CPU와 Chrome만 쓰며 GPU lease가 필요 없다. Metal 경로는 등록하지 않았다.
장면은 프레임 번호의 순수 함수라 같은 프레임은 렌더 순서와 무관하게 같은 화면을 만든다(smoke가 이를 대조한다).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from ttakkari.creative.engines import media, node_tools
from ttakkari.creative.engines.base import (
    EngineAdapter,
    EngineEmptyOutput,
    EngineError,
    EngineOutput,
    EngineRun,
    ExecutionContext,
    ProbeResult,
    ProfileSpec,
    apply_defaults,
    now_iso,
    run_process,
    validate_params,
)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_X264 = [
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-crf",
    "18",
    "-preset",
    "veryfast",
    "-movflags",
    "+faststart",
]

PROFILES = {
    "orbit-scene": ProfileSpec(
        "orbit-scene",
        frozenset({"3d", "camera-move", "previz"}),
        "도형 장면 위를 카메라가 공전하거나 다가가는 샷. 오디오 cue에서 화각과 조명이 맥박친다.",
        "video",
        resource_family="chrome",
        params={
            "camera_move": {
                "type": "enum",
                "default": "orbit",
                "values": ["orbit", "dolly"],
            },
            "degrees": {"type": "float", "default": 90.0, "min": 0.0, "max": 360.0},
            "distance": {"type": "float", "default": 9.0, "min": 3.0, "max": 20.0},
            "height": {"type": "float", "default": 3.2, "min": 0.5, "max": 10.0},
            "fov": {"type": "float", "default": 45.0, "min": 20.0, "max": 80.0},
            "objects": {"type": "int", "default": 7, "min": 3, "max": 40},
        },
    )
}


class ThreeAdapter(EngineAdapter):
    engine_id = "three"
    display_name = "Three.js 독립 3D 프레임 렌더"
    profiles = PROFILES
    rights: ClassVar[dict[str, str]] = {
        "license": "three.js(MIT)·playwright-core(Apache-2.0) 표기 확인 전: L 작업에서 원문 확인",
        "use_scope": "personal",
    }

    def __init__(
        self, toolchain: Path | None = None, chrome: Path | None = None
    ) -> None:
        self.toolchain = Path(toolchain) if toolchain else node_tools.LAB_ROOT / "3d"
        self.chrome = Path(chrome) if chrome else node_tools.CHROME_PATH

    @property
    def node_modules(self) -> Path:
        return self.toolchain / "node_modules"

    def probe(self) -> ProbeResult:
        env = "lab-toolchain"
        node = node_tools.find_node()
        if node is None:
            return ProbeResult(
                "absent", env, None, None, now_iso(), error="node를 찾지 못했습니다."
            )
        if (
            not (self.node_modules / "three").is_dir()
            or not (self.node_modules / "playwright-core").is_dir()
        ):
            return ProbeResult(
                "absent",
                env,
                str(self.toolchain),
                None,
                now_iso(),
                error="three 또는 playwright-core 설치가 없습니다.",
            )
        if not self.chrome.is_file():
            return ProbeResult(
                "absent",
                env,
                str(self.toolchain),
                None,
                now_iso(),
                error="Chrome 실행 파일이 없습니다.",
            )
        try:
            three = node_tools.package_version(node, self.node_modules, "three")
            playwright = node_tools.package_version(
                node, self.node_modules, "playwright-core"
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                "probe_failed",
                env,
                str(self.toolchain),
                None,
                now_iso(),
                error=str(exc),
            )
        return ProbeResult(
            "available",
            env,
            str(self.toolchain),
            f"three@{three}",
            now_iso(),
            {
                "node": node_tools.node_version(node),
                "playwright_core": playwright,
                "chrome": str(self.chrome),
                "webgl": "swiftshader(cpu)",
            },
        )

    def validate(self, spec: dict[str, Any]) -> list[str]:
        profile = self.profiles.get(spec.get("profile", ""))
        if profile is None:
            return ["등록되지 않은 프로필입니다."]
        errors = validate_params(profile.params, spec.get("params") or {})
        fps = spec.get("fps") or {}
        if fps.get("den") != 1:
            errors.append("Three 어댑터는 정수 fps(den=1)만 지원합니다.")
        fmt = spec.get("format") or {}
        if not (
            isinstance(fmt.get("width"), int)
            and isinstance(fmt.get("height"), int)
            and fmt["width"] % 2 == 0
            and fmt["height"] % 2 == 0
        ):
            errors.append("해상도는 짝수 정수여야 합니다.")
        elif not (64 <= fmt["width"] <= 1920 and 64 <= fmt["height"] <= 1920):
            errors.append("소프트웨어 WebGL 경로의 해상도 상한은 1920입니다.")
        frames = spec.get("range", {}).get("frames")
        if not isinstance(frames, int) or not 1 <= frames <= 1800:
            errors.append("프레임 수는 1~1800이어야 합니다.")
        palette = (spec.get("bible") or {}).get("palette") or []
        if any(not isinstance(c, str) or not _HEX.match(c) for c in palette[:5]):
            errors.append("팔레트 색은 #RRGGBB여야 합니다.")
        seed = spec.get("seed")
        if seed is not None and (
            isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32
        ):
            errors.append("seed는 32비트 부호 없는 정수여야 합니다.")
        return errors

    def _scene_spec(
        self,
        spec: dict[str, Any],
        params: dict[str, Any],
        render_frames: list[int] | None = None,
    ) -> dict[str, Any]:
        palette = [
            c for c in ((spec.get("bible") or {}).get("palette") or []) if _HEX.match(c)
        ][:5]
        scene = {
            "width": spec["format"]["width"],
            "height": spec["format"]["height"],
            "fps": spec["fps"]["num"],
            "frames": spec["range"]["frames"],
            "seed": spec.get("seed") or 0,
            "palette": palette or ["#101418", "#f4f4ed", "#00f0b5", "#79aae1"],
            "camera": {
                "move": params["camera_move"],
                "degrees": params["degrees"],
                "distance": params["distance"],
                "height": params["height"],
                "fov": params["fov"],
            },
            "objects": params["objects"],
            "cue_frames": [
                c["frame"]
                for c in spec.get("cues", [])
                if 0 <= c["frame"] < spec["range"]["frames"]
            ],
        }
        if render_frames is not None:
            scene["render_frames"] = render_frames
        return scene

    def _capture(
        self, ctx: ExecutionContext, scene: dict[str, Any]
    ) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        node = node_tools.find_node()
        if node is None or not self.node_modules.is_dir() or not self.chrome.is_file():
            raise EngineError(
                "unavailable", "Three 도구 체인 또는 Chrome을 찾지 못했습니다."
            )
        project = node_tools.stage_project("three", ctx.workdir, self.node_modules)
        (project / "spec.json").write_text(json.dumps(scene))
        env = {
            "PATH": f"{Path(node).parent}:/usr/bin:/bin",
            "HOME": str(ctx.workdir),
            "TTAKKARI_CHROME": str(self.chrome),
        }
        result = run_process(
            [node, "render.mjs", str(project)], ctx=ctx, cwd=project, env=env
        )
        tail = result["log_tail"].strip().splitlines()
        summary: dict[str, Any] = {}
        for line in reversed(tail):
            try:
                summary = json.loads(line)
                break
            except ValueError:
                continue
        if result["returncode"] != 0 or not summary.get("ok"):
            raise EngineError(
                "three_failed",
                f"Three 렌더 실패(종료 {result['returncode']}): {summary.get('error') or result['log_tail'][-400:]}",
            )
        return project / "frames", summary, result

    def execute(self, ctx: ExecutionContext) -> EngineRun:
        spec = ctx.spec
        errors = self.validate(spec)
        if errors:
            raise EngineError("invalid_spec", "; ".join(errors))
        profile = self.profiles[spec["profile"]]
        params = apply_defaults(profile.params, spec.get("params") or {})
        scene = self._scene_spec(spec, params)
        frames_dir, summary, result = self._capture(ctx, scene)
        produced = sorted(frames_dir.glob("f_*.png"))
        if len(produced) != spec["range"]["frames"]:
            raise EngineError(
                "incomplete_frames",
                f"PNG {len(produced)}장, 기대 {spec['range']['frames']}장입니다.",
            )
        out = ctx.workdir / "out.mp4"
        encode = run_process(
            [
                media.ffmpeg_path(),
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-framerate",
                str(spec["fps"]["num"]),
                "-i",
                str(frames_dir / "f_%05d.png"),
                "-frames:v",
                str(spec["range"]["frames"]),
                *_X264,
                "-fflags",
                "+bitexact",
                "-flags:v",
                "+bitexact",
                "-map_metadata",
                "-1",
                str(out),
            ],
            ctx=ctx,
            cwd=ctx.workdir,
        )
        if encode["returncode"] != 0:
            raise EngineError(
                "ffmpeg_failed", f"프레임 인코딩 실패: {encode['log_tail'][-300:]}"
            )
        if not out.is_file() or out.stat().st_size == 0:
            raise EngineEmptyOutput("Three 렌더가 빈 영상을 만들었습니다.")
        info = media.probe_media(out)
        return EngineRun(
            [
                EngineOutput(
                    f"{spec['shot_id']}.mp4", "video/mp4", out.read_bytes(), "primary"
                )
            ],
            metrics={
                "wall_seconds": round(
                    result["wall_seconds"] + encode["wall_seconds"], 3
                ),
                "rss_peak_mb": max(result["rss_peak_mb"], encode["rss_peak_mb"]),
                "capture_ms": summary.get("total_ms"),
                "probe": info,
            },
            provenance={
                "three_revision": (summary.get("info") or {}).get("three"),
                "gl_renderer": (summary.get("info") or {}).get("gl_renderer"),
                "scene_sha": hashlib.sha256(
                    json.dumps(scene, sort_keys=True).encode()
                ).hexdigest(),
                "seed": scene["seed"],
                "console_messages": len(summary.get("console") or []),
            },
        )

    def release(self, ctx: ExecutionContext) -> None:
        shutil.rmtree(ctx.workdir, ignore_errors=True)

    def smoke(self) -> dict[str, Any]:
        spec = {
            "shot_id": "smoke",
            "profile": "orbit-scene",
            "params": {"camera_move": "orbit", "degrees": 60.0},
            "format": {"width": 640, "height": 360},
            "fps": {"num": 24, "den": 1},
            "range": {"frames": 24},
            "cues": [{"frame": 6}, {"frame": 12}],
            "seed": 7,
            "bible": {"palette": ["#101418", "#f4f4ed", "#00f0b5", "#79aae1"]},
        }
        params = apply_defaults(PROFILES["orbit-scene"].params, spec["params"])
        with tempfile.TemporaryDirectory(prefix="ttk-three-smoke-") as tmp:
            run = self.execute(
                ExecutionContext(spec=spec, workdir=Path(tmp), timeout_seconds=300)
            )
            clip = Path(tmp) / "smoke.mp4"
            clip.write_bytes(run.outputs[0].data)
            scan = media.decode_scan(clip)
        # 프레임 결정성: 다른 순서·부분 집합으로 같은 프레임을 다시 렌더해 PNG 해시가 같은지 대조한다.
        shas: dict[str, dict[int, str]] = {}
        for label, order in (("forward", [3, 8, 15]), ("reverse", [15, 8, 3])):
            with tempfile.TemporaryDirectory(prefix="ttk-three-det-") as tmp:
                frames_dir, _summary, _ = self._capture(
                    ExecutionContext(spec=spec, workdir=Path(tmp), timeout_seconds=300),
                    self._scene_spec(spec, params, order),
                )
                shas[label] = {
                    int(p.stem.split("_")[1]): hashlib.sha256(
                        p.read_bytes()
                    ).hexdigest()
                    for p in sorted(frames_dir.glob("f_*.png"))
                }
        deterministic = shas["forward"] == shas["reverse"] and len(shas["forward"]) == 3
        return {
            "profile": "orbit-scene",
            "wall_seconds": run.metrics["wall_seconds"],
            "rss_peak_mb": run.metrics["rss_peak_mb"],
            "rss_scope": "프로세스 트리(node+Chrome) 합 RSS를 0.1초 간격으로 표본한 최댓값",
            "probe": run.metrics["probe"],
            "decoded_frames": scan["frames"],
            "decode_ok": scan["ok"],
            "bytes": len(run.outputs[0].data),
            "frame_deterministic_across_order": deterministic,
            "provenance": run.provenance,
        }
