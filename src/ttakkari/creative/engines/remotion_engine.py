"""Remotion 어댑터: 타이포 카드 샷(프로필 title-card).

CPU와 Remotion 번들 Chrome Headless Shell만 쓰며 GPU 모델은 띄우지 않는다. 렌더 코드는 이 저장소 assets/remotion이고
도구 체인(node_modules)만 lab 프로젝트의 것을 링크한다. 정수 fps만 지원한다(ADR Q9: 유리수 fps는 컨폼에서 처리).
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

PINNED_VERSION = "4.0.537"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_FONT = re.compile(r"^[\w\s,'\"가-힣-]{1,120}$")

PROFILES = {
    "title-card": ProfileSpec(
        "title-card",
        frozenset({"typography", "title", "text-overlay", "motion-graphics"}),
        "그라디언트 배경 위 제목·부제 카드. 스프링 등장과 오디오 cue 맥박.",
        "video",
        resource_family="chrome",
        params={
            "title": {"type": "str", "required": True, "max_len": 80},
            "subtitle": {"type": "str", "default": "", "max_len": 120},
            "font_family": {
                "type": "str",
                "default": '"Apple SD Gothic Neo", sans-serif',
                "max_len": 120,
            },
            "fade_out_frames": {"type": "int", "default": 0, "min": 0, "max": 48},
            "concurrency": {"type": "int", "default": 2, "min": 1, "max": 4},
        },
    )
}


class RemotionAdapter(EngineAdapter):
    engine_id = "remotion"
    display_name = "Remotion 타이포·모션 그래픽"
    profiles = PROFILES
    rights: ClassVar[dict[str, str]] = {
        "license": f"Remotion {PINNED_VERSION} Free License(개인·3인 이하 영리 조직 등 자격 조건, 파생 판매 제한). 회사·다중 사용자 상용 사용은 별도 분석 전 자동 활성화하지 않음",
        "use_scope": "personal",
    }

    def __init__(self, toolchain: Path | None = None) -> None:
        self.toolchain = (
            Path(toolchain)
            if toolchain
            else node_tools.LAB_ROOT / "remotion" / "project"
        )

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
        cli = self.node_modules / ".bin" / "remotion"
        shell = self.node_modules / ".remotion" / "chrome-headless-shell"
        if not cli.exists():
            return ProbeResult(
                "absent",
                env,
                str(self.toolchain),
                None,
                now_iso(),
                error="remotion CLI 설치가 없습니다.",
            )
        try:
            version = node_tools.package_version(node, self.node_modules, "remotion")
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                "probe_failed",
                env,
                str(self.toolchain),
                None,
                now_iso(),
                error=str(exc),
            )
        details = {
            "node": node_tools.node_version(node),
            "chrome_headless_shell": shell.is_dir(),
            "pinned": PINNED_VERSION,
        }
        if version != PINNED_VERSION:
            return ProbeResult(
                "probe_failed",
                env,
                str(self.toolchain),
                version,
                now_iso(),
                details,
                error=f"Remotion 버전 {version}이 고정 버전 {PINNED_VERSION}과 다릅니다(라이선스·동작 재검토 필요).",
            )
        if not shell.is_dir():
            return ProbeResult(
                "present",
                env,
                str(self.toolchain),
                version,
                now_iso(),
                details,
                error="Chrome Headless Shell이 아직 내려받아지지 않았습니다.",
            )
        return ProbeResult(
            "available", env, str(self.toolchain), version, now_iso(), details
        )

    def validate(self, spec: dict[str, Any]) -> list[str]:
        profile = self.profiles.get(spec.get("profile", ""))
        if profile is None:
            return ["등록되지 않은 프로필입니다."]
        params = spec.get("params") or {}
        errors = validate_params(profile.params, params)
        if spec.get("fps", {}).get("den") != 1:
            errors.append("Remotion 어댑터는 정수 fps(den=1)만 지원합니다.")
        fmt = spec.get("format") or {}
        if not (
            isinstance(fmt.get("width"), int)
            and isinstance(fmt.get("height"), int)
            and fmt["width"] % 2 == 0
            and fmt["height"] % 2 == 0
        ):
            errors.append("해상도는 짝수 정수여야 합니다.")
        elif not (64 <= fmt["width"] <= 3840 and 64 <= fmt["height"] <= 3840):
            errors.append("해상도 범위를 벗어났습니다.")
        frames = spec.get("range", {}).get("frames")
        if not isinstance(frames, int) or not 1 <= frames <= 3600:
            errors.append("프레임 수는 1~3600이어야 합니다.")
        palette = (spec.get("bible") or {}).get("palette") or []
        if any(not isinstance(c, str) or not _HEX.match(c) for c in palette[:3]):
            errors.append("팔레트 색은 #RRGGBB여야 합니다.")
        if not _FONT.match(str(params.get("font_family", "sans-serif"))):
            errors.append("글꼴 이름에 허용되지 않은 문자가 있습니다.")
        return errors

    def _props(self, spec: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        palette = [
            c for c in ((spec.get("bible") or {}).get("palette") or []) if _HEX.match(c)
        ][:3]
        return {
            "width": spec["format"]["width"],
            "height": spec["format"]["height"],
            "fps": spec["fps"]["num"],
            "durationInFrames": spec["range"]["frames"],
            "title": params["title"],
            "subtitle": params.get("subtitle", ""),
            "palette": palette or ["#0B0F10", "#F4F4ED", "#00F0B5"],
            "fontFamily": params["font_family"],
            "cueFrames": [
                c["frame"]
                for c in spec.get("cues", [])
                if 0 <= c["frame"] < spec["range"]["frames"]
            ],
            "fadeOutFrames": params["fade_out_frames"],
        }

    def execute(self, ctx: ExecutionContext) -> EngineRun:
        spec = ctx.spec
        errors = self.validate(spec)
        if errors:
            raise EngineError("invalid_spec", "; ".join(errors))
        node = node_tools.find_node()
        if node is None or not self.node_modules.is_dir():
            raise EngineError("unavailable", "Remotion 도구 체인을 찾지 못했습니다.")
        profile = self.profiles[spec["profile"]]
        params = apply_defaults(profile.params, spec.get("params") or {})
        project = node_tools.stage_project("remotion", ctx.workdir, self.node_modules)
        (project / "props.json").write_text(
            json.dumps(self._props(spec, params), ensure_ascii=False)
        )
        out = ctx.workdir / "out.mp4"
        argv = [
            str(project / "node_modules" / ".bin" / "remotion"),
            "render",
            "src/index.ts",
            "ShotText",
            str(out),
            "--props=props.json",
            f"--concurrency={params['concurrency']}",
            "--codec=h264",
            "--log=error",
            "--overwrite",
        ]
        env = {
            "PATH": f"{Path(node).parent}:/usr/bin:/bin",
            "HOME": str(ctx.workdir),
            "REMOTION_DISABLE_TELEMETRY": "1",
            "NO_COLOR": "1",
        }
        result = run_process(argv, ctx=ctx, cwd=project, env=env)
        if result["returncode"] != 0:
            raise EngineError(
                "remotion_failed",
                f"remotion 종료 코드 {result['returncode']}: {result['log_tail'][-500:]}",
            )
        if not out.is_file() or out.stat().st_size == 0:
            raise EngineEmptyOutput("Remotion이 빈 출력 파일을 만들었습니다.")
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
                "remotion": node_tools.package_version(
                    node, self.node_modules, "remotion"
                ),
                "node": node_tools.node_version(node),
                "composition": "ShotText",
                "props_sha": hashlib.sha256(
                    json.dumps(self._props(spec, params), sort_keys=True).encode()
                ).hexdigest(),
            },
        )

    def release(self, ctx: ExecutionContext) -> None:
        shutil.rmtree(ctx.workdir, ignore_errors=True)

    def smoke(self) -> dict[str, Any]:
        spec = {
            "shot_id": "smoke",
            "profile": "title-card",
            "params": {"title": "스모크", "subtitle": "remotion"},
            "format": {"width": 640, "height": 360},
            "fps": {"num": 24, "den": 1},
            "range": {"frames": 24},
            "cues": [{"frame": 6}, {"frame": 12}],
            "bible": {"palette": ["#0B0F10", "#F4F4ED", "#00F0B5"]},
        }
        with tempfile.TemporaryDirectory(prefix="ttk-remotion-smoke-") as tmp:
            run = self.execute(
                ExecutionContext(spec=spec, workdir=Path(tmp), timeout_seconds=300)
            )
            clip = Path(tmp) / "smoke.mp4"
            clip.write_bytes(run.outputs[0].data)
            scan = media.decode_scan(clip)
        return {
            "profile": "title-card",
            "wall_seconds": run.metrics["wall_seconds"],
            "rss_peak_mb": run.metrics["rss_peak_mb"],
            "rss_scope": "프로세스 트리(node+Chrome) 합 RSS를 0.1초 간격으로 표본한 최댓값",
            "probe": run.metrics["probe"],
            "decoded_frames": scan["frames"],
            "decode_ok": scan["ok"],
            "bytes": len(run.outputs[0].data),
            "provenance": run.provenance,
        }
