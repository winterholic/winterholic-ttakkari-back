"""FFmpeg 어댑터: 단색·테스트 패턴 샷 생성, 소스 컨폼, 최종 조립과 오디오 합성.

모든 명령은 검증된 프로필 매개변수로 argv를 조립한다. 출력 프레임 수는 항상 `-frames:v`로 고정해 타임라인의 반열림 구간과 맞춘다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from ttakkari.creative.engines import media
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
_BITEXACT = [
    "-fflags",
    "+bitexact",
    "-flags:v",
    "+bitexact",
    "-flags:a",
    "+bitexact",
    "-map_metadata",
    "-1",
]

PROFILES = {
    "solid-color": ProfileSpec(
        "solid-color",
        frozenset({"color-fill", "placeholder", "conform"}),
        "단색 배경 영상. 선택적으로 무음 또는 사인파 오디오를 붙인다.",
        "video",
        params={
            "color": {"type": "str", "default": "#101418", "max_len": 7},
            "audio": {
                "type": "enum",
                "default": "none",
                "values": ["none", "silence", "tone"],
            },
            "tone_hz": {"type": "int", "default": 440, "min": 50, "max": 4000},
            "tone_gain": {"type": "float", "default": 0.2, "min": 0.01, "max": 0.9},
        },
        has_audio=True,
    ),
    "test-pattern": ProfileSpec(
        "test-pattern",
        frozenset({"test-pattern", "placeholder", "motion"}),
        "움직임이 있는 lavfi 테스트 패턴. 모션 극값·키프레임 표본 시험에 쓴다.",
        "video",
        params={
            "pattern": {
                "type": "enum",
                "default": "testsrc2",
                "values": ["testsrc2", "smptebars", "mandelbrot"],
            }
        },
    ),
    "conform": ProfileSpec(
        "conform",
        frozenset({"conform", "footage"}),
        "입력 영상 또는 이미지를 프로젝트 fps·해상도·프레임 수에 맞춘다. 짧으면 마지막 프레임을 복제한다.",
        "video",
        params={"fit": {"type": "enum", "default": "pad", "values": ["pad", "crop"]}},
        needs_assets=True,
    ),
    "assemble": ProfileSpec(
        "assemble",
        frozenset({"assemble", "concat", "mux"}),
        "샷 클립을 순서대로 이어 붙이고 선택적으로 오디오 한 트랙을 입힌다(재인코딩).",
        "video",
        params={
            "audio_gain_db": {"type": "float", "default": 0.0, "min": -30.0, "max": 6.0}
        },
        needs_assets=True,
        has_audio=True,
    ),
}


def _fps_text(spec: dict[str, Any]) -> str:
    return f"{spec['fps']['num']}/{spec['fps']['den']}"


class FfmpegAdapter(EngineAdapter):
    engine_id = "ffmpeg"
    display_name = "FFmpeg 편집·합성"
    profiles = PROFILES
    rights: ClassVar[dict[str, str]] = {
        "license": "FFmpeg 7.1 (imageio-ffmpeg 번들): 배포 조건은 L 작업에서 별도 확인",
        "use_scope": "personal",
    }

    def probe(self) -> ProbeResult:
        try:
            exe = media.ffmpeg_path()
            version = media.ffmpeg_version()
            encoders = subprocess.run(
                [exe, "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                "probe_failed",
                "backend-venv",
                None,
                None,
                now_iso(),
                error=f"{type(exc).__name__}: {exc}",
            )
        missing = [name for name in ("libx264", "aac") if name not in encoders]
        if missing:
            return ProbeResult(
                "probe_failed",
                "backend-venv",
                exe,
                version,
                now_iso(),
                {"missing_encoders": missing},
                error="필수 인코더가 없습니다: " + ", ".join(missing),
            )
        return ProbeResult(
            "available",
            "backend-venv",
            exe,
            version,
            now_iso(),
            {"encoders": ["libx264", "aac"]},
        )

    def validate(self, spec: dict[str, Any]) -> list[str]:
        profile = self.profiles.get(spec.get("profile", ""))
        if profile is None:
            return ["등록되지 않은 프로필입니다."]
        params = spec.get("params") or {}
        errors = validate_params(profile.params, params)
        if profile.profile_id == "solid-color" and not _HEX.match(
            str(params.get("color", "#101418"))
        ):
            errors.append("color는 #RRGGBB 형식이어야 합니다.")
        fmt = spec.get("format") or {}
        if (
            not (
                isinstance(fmt.get("width"), int) and isinstance(fmt.get("height"), int)
            )
            or fmt["width"] % 2
            or fmt["height"] % 2
        ):
            errors.append("해상도는 짝수 정수여야 합니다(yuv420p).")
        if (
            not isinstance(spec.get("range", {}).get("frames"), int)
            or spec["range"]["frames"] < 1
        ):
            errors.append("프레임 수가 잘못됐습니다.")
        assets = spec.get("input_assets") or []
        if profile.needs_assets and not assets:
            errors.append(f"{profile.profile_id} 프로필에는 입력 자산이 필요합니다.")
        if profile.profile_id == "conform" and len(assets) != 1:
            errors.append("conform은 입력 자산이 정확히 하나여야 합니다.")
        if profile.profile_id == "assemble":
            roles = [a.get("role") for a in assets]
            if (
                roles.count("audio") > 1
                or "clip" not in roles
                or any(r not in {"clip", "audio"} for r in roles)
            ):
                errors.append(
                    "assemble 입력은 clip 하나 이상과 audio 최대 하나여야 합니다."
                )
            counts = [a.get("frames") for a in assets if a.get("role") == "clip"]
            if any(not isinstance(n, int) or n < 1 for n in counts):
                errors.append("assemble clip마다 프레임 수(frames)가 필요합니다.")
            elif sum(counts) != spec.get("range", {}).get("frames"):
                errors.append("clip 프레임 수의 합이 최종 프레임 수와 다릅니다.")
        return errors

    def execute(self, ctx: ExecutionContext) -> EngineRun:
        spec = ctx.spec
        errors = self.validate(spec)
        if errors:
            raise EngineError("invalid_spec", "; ".join(errors))
        profile = self.profiles[spec["profile"]]
        params = apply_defaults(profile.params, spec.get("params") or {})
        frames = spec["range"]["frames"]
        fmt = spec["format"]
        out = ctx.workdir / "out.mp4"
        exe = media.ffmpeg_path()
        base = [exe, "-hide_banner", "-nostdin", "-v", "error", "-y"]
        width, height = fmt["width"], fmt["height"]
        size = f"{width}x{height}"
        fps = _fps_text(spec)
        sample_rate = int(spec.get("sample_rate", 48_000))
        argv: list[str]
        if profile.profile_id == "solid-color":
            argv = [
                *base,
                "-f",
                "lavfi",
                "-i",
                f"color=c={params['color'].lstrip('#')}:s={size}:r={fps}",
            ]
            seconds = frames * spec["fps"]["den"] / spec["fps"]["num"]
            if params["audio"] == "tone":
                argv += [
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={params['tone_hz']}:sample_rate={sample_rate}",
                ]
            elif params["audio"] == "silence":
                argv += ["-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono"]
            argv += ["-frames:v", str(frames), *_X264]
            if params["audio"] != "none":
                filt = (
                    f"volume={params['tone_gain']},"
                    if params["audio"] == "tone"
                    else ""
                )
                argv += [
                    "-af",
                    f"{filt}atrim=0:{seconds:.6f},asetpts=N/SR/TB",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    str(sample_rate),
                ]
            argv += [*_BITEXACT, str(out)]
        elif profile.profile_id == "test-pattern":
            argv = [
                *base,
                "-f",
                "lavfi",
                "-i",
                f"{params['pattern']}=s={size}:r={fps}",
                "-frames:v",
                str(frames),
                *_X264,
                *_BITEXACT,
                str(out),
            ]
        elif profile.profile_id == "conform":
            source = self._write_asset(
                ctx, spec["input_assets"][0]["sha256"], "source.bin"
            )
            scaling = (
                f"scale={size}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                if params["fit"] == "pad"
                else f"scale={size}:force_original_aspect_ratio=increase,crop={width}:{height}"
            )
            vf = f"fps={fps},{scaling},setsar=1,tpad=stop_mode=clone:stop={frames}"
            still = _is_still_image(source)
            argv = [
                *base,
                *(["-loop", "1", "-framerate", fps] if still else []),
                "-i",
                str(source),
                "-an",
                "-vf",
                vf,
                "-fps_mode",
                "cfr",
                "-r",
                fps,
                "-frames:v",
                str(frames),
                *_X264,
                *_BITEXACT,
                str(out),
            ]
        else:
            argv = self._assemble_argv(ctx, spec, params, base, out)
        result = run_process(argv, ctx=ctx, cwd=ctx.workdir)
        if result["returncode"] != 0:
            raise EngineError(
                "ffmpeg_failed",
                f"ffmpeg 종료 코드 {result['returncode']}: {result['log_tail'][-400:]}",
            )
        if not out.is_file() or out.stat().st_size == 0:
            raise EngineEmptyOutput("ffmpeg가 빈 출력 파일을 만들었습니다.")
        info = media.probe_media(out)
        return EngineRun(
            [
                EngineOutput(
                    f"{spec['shot_id']}.mp4", "video/mp4", out.read_bytes(), "primary"
                )
            ],
            metrics={
                "wall_seconds": result["wall_seconds"],
                "rss_peak_mb": result["rss_peak_mb"],
                "probe": info,
            },
            provenance={
                "ffmpeg": media.ffmpeg_version(),
                "profile": profile.profile_id,
            },
        )

    @staticmethod
    def _write_asset(ctx: ExecutionContext, sha: str, name: str) -> Path:
        path = ctx.workdir / name
        path.write_bytes(ctx.read_asset(sha))
        return path

    def _assemble_argv(self, ctx, spec, params, base, out) -> list[str]:
        fmt = spec["format"]
        width, height = fmt["width"], fmt["height"]
        size = f"{width}x{height}"
        fps = _fps_text(spec)
        clips: list[Path] = []
        audio_path: Path | None = None
        for index, asset in enumerate(spec["input_assets"]):
            path = self._write_asset(ctx, asset["sha256"], f"in_{index}.bin")
            if asset["role"] == "audio":
                audio_path = path
            else:
                clips.append(path)
        argv = list(base)
        for clip in clips:
            argv += ["-i", str(clip)]
        if audio_path is not None:
            argv += ["-i", str(audio_path)]
        frame_counts = [
            a["frames"] for a in spec["input_assets"] if a["role"] == "clip"
        ]
        # 클립마다 정확히 샷 프레임 수가 되게 자르거나 마지막 프레임을 복제한다(생성형 클립의 길이 차이 흡수).
        chains = [
            f"[{i}:v]fps={fps},scale={size}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
            f"tpad=stop_mode=clone:stop={frame_counts[i]},trim=end_frame={frame_counts[i]},setpts=PTS-STARTPTS[v{i}]"
            for i in range(len(clips))
        ]
        joined = "".join(f"[v{i}]" for i in range(len(clips)))
        graph = ";".join([*chains, f"{joined}concat=n={len(clips)}:v=1:a=0[outv]"])
        frames = spec["range"]["frames"]
        argv += ["-filter_complex", graph, "-map", "[outv]"]
        if audio_path is not None:
            seconds = frames * spec["fps"]["den"] / spec["fps"]["num"]
            argv += [
                "-map",
                f"{len(clips)}:a:0",
                "-af",
                f"volume={params['audio_gain_db']}dB,apad,atrim=0:{seconds:.6f},asetpts=N/SR/TB",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                str(int(spec.get("sample_rate", 48_000))),
            ]
        argv += [
            "-fps_mode",
            "cfr",
            "-r",
            fps,
            "-frames:v",
            str(frames),
            *_X264,
            *_BITEXACT,
            str(out),
        ]
        return argv

    def release(self, ctx: ExecutionContext) -> None:
        shutil.rmtree(ctx.workdir, ignore_errors=True)

    def smoke(self) -> dict[str, Any]:
        import tempfile

        spec = {
            "shot_id": "smoke",
            "profile": "solid-color",
            "params": {"color": "#00f0b5", "audio": "tone"},
            "format": {"width": 320, "height": 180},
            "fps": {"num": 24, "den": 1},
            "sample_rate": 48_000,
            "range": {"frames": 24},
        }
        with tempfile.TemporaryDirectory(prefix="ttk-ffmpeg-smoke-") as tmp:
            ctx = ExecutionContext(spec=spec, workdir=Path(tmp))
            run = self.execute(ctx)
            out = Path(tmp) / "smoke.mp4"
            out.write_bytes(run.outputs[0].data)
            scan = media.decode_scan(out)
            return {
                "profile": "solid-color+tone",
                "wall_seconds": run.metrics["wall_seconds"],
                "rss_peak_mb": run.metrics["rss_peak_mb"],
                "probe": run.metrics["probe"],
                "decoded_frames": scan["frames"],
                "decode_ok": scan["ok"],
                "bytes": len(run.outputs[0].data),
            }


def _is_still_image(path: Path) -> bool:
    head = path.read_bytes()[:12]
    return (
        head.startswith(b"\x89PNG")
        or head[:3] == b"\xff\xd8\xff"
        or head[:4] == b"RIFF"
        and head[8:12] == b"WEBP"
    )
