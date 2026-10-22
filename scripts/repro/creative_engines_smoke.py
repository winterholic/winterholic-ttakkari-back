"""3·4단계 재현 스크립트: 실제 Remotion·Three.js·FFmpeg 짧은 렌더 smoke + 감독 흐름 + 결함 샷 재제작.

사용:  PYTHONPATH=src python scripts/repro/creative_engines_smoke.py [--out DIR] [--evidence FILE.json]

- 실제로 실행하는 것: FFmpeg(imageio-ffmpeg 번들), Remotion 4.0.537(Chrome Headless Shell), 독립 Three.js(Chrome, SwiftShader=CPU WebGL).
- 실행하지 않는 것: GPU 모델, Comfy 생성(managed runtime을 시작하지 않는다), 로컬 TTS 합성, 유료 API, 감독 LLM(고정 응답 fake).
- 임시 폴더에서만 쓰며 운영 데이터(`~/.ttakkari`)와 공유 레지스트리를 건드리지 않는다.
- 도구 체인은 lab 프로젝트(TTAKKARI_STUDIO_LAB)의 node_modules를 읽기만 한다.
종료 코드: 모든 검사가 통과하면 0, 하나라도 실패하면 1.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import psutil

from ttakkari.creative.direction import CreativeDirector
from ttakkari.creative.director import DirectorStore
from ttakkari.creative.director_llm import FixedResponseLlm
from ttakkari.creative.engines.comfy_engine import ComfyAdapter
from ttakkari.creative.engines.ffmpeg_engine import FfmpegAdapter
from ttakkari.creative.engines.registry import EngineRegistry
from ttakkari.creative.engines.remotion_engine import RemotionAdapter
from ttakkari.creative.engines.three_engine import ThreeAdapter
from ttakkari.creative.feedback import submit_feedback
from ttakkari.creative.pipeline import ProductionPipeline

LAB_FFPROBE = Path(
    os.environ.get(
        "TTAKKARI_FFPROBE",
        "/Users/winterholic/development/projects/ttakkari-studio-lab/remotion/bin/ffprobe",
    )
)


def click_track(seconds: int = 3, bpm: int = 120, rate: int = 48_000) -> bytes:
    signal = np.zeros(rate * seconds)
    step = 60 / bpm
    for k in range(int(seconds / step)):
        start, n = int(k * step * rate), int(0.04 * rate)
        signal[start : start + n] += np.sin(
            2 * np.pi * 1000 * np.arange(n) / rate
        ) * np.exp(-np.arange(n) / (0.01 * rate))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes((signal * 0.8 * 32767).astype(np.int16).tobytes())
    return buffer.getvalue()


def ffprobe(path: Path) -> dict | None:
    if not LAB_FFPROBE.exists():
        return None
    result = subprocess.run(
        [
            str(LAB_FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,nb_frames,sample_rate,channels,duration:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return (
        json.loads(result.stdout)
        if result.returncode == 0 and result.stdout
        else {"error": result.stderr[-200:]}
    )


def host_memory() -> dict:
    vm, swap = psutil.virtual_memory(), psutil.swap_memory()
    return {
        "available_gb": round(vm.available / 2**30, 2),
        "used_percent": vm.percent,
        "swap_used_gb": round(swap.used / 2**30, 2),
    }


BIBLE = {
    "palette": ["#101418", "#f4f4ed", "#00f0b5", "#79aae1"],
    "typography": {"font_family": "Apple SD Gothic Neo", "title_scale": 0.12},
    "composition": {
        "size": [640, 360],
        "aspect": "16:9",
        "rules": ["중앙 정렬", "강조색은 한 요소에만"],
    },
    "camera": {"language": ["느린 공전", "정면 고정"], "default_move": "orbit"},
    "lut": {"name": "neutral", "intent": "중립 룩(적용 도구 없음)"},
    "negative_rules": ["장면 중간 암전 금지"],
}
TIMELINE = {
    "sections": [
        {"label": "intro", "start_f": 0, "end_f": 24},
        {"label": "body", "start_f": 24, "end_f": 72},
    ],
    "shots": [
        {
            "shot_id": "shot-1",
            "start_f": 0,
            "end_f": 24,
            "cut": "action",
            "reason": "타이틀로 시작",
        },
        {
            "shot_id": "shot-2",
            "start_f": 24,
            "end_f": 48,
            "cut": "beat",
            "reason": "박자에 맞춘 3D 전환",
        },
        {
            "shot_id": "shot-3",
            "start_f": 48,
            "end_f": 72,
            "cut": "beat",
            "reason": "마무리 패턴",
        },
    ],
}


def shots_response(fade: int) -> dict:
    return {
        "shots": [
            {
                "shot_id": "shot-1",
                "intent": "제목 카드",
                "engine": "remotion",
                "profile": "title-card",
                "params": {
                    "title": "심야 도시",
                    "subtitle": "AI Creative Studio",
                    "fade_out_frames": fade,
                },
                "seed": 1,
                "reason": "정확한 타이포가 필요한 도입이라 Remotion",
                "alternatives": [
                    {
                        "engine": "ffmpeg",
                        "profile": "solid-color",
                        "cost": "글자를 못 그림",
                    }
                ],
            },
            {
                "shot_id": "shot-2",
                "intent": "도형 장면 위를 도는 카메라",
                "engine": "three",
                "profile": "orbit-scene",
                "params": {"camera_move": "orbit", "degrees": 80.0},
                "seed": 7,
                "reason": "카메라 이동이 핵심이라 독립 Three",
                "alternatives": [
                    {"engine": "remotion", "profile": "title-card", "cost": "3D 불가"}
                ],
            },
            {
                "shot_id": "shot-3",
                "intent": "움직이는 마무리 패턴",
                "engine": "ffmpeg",
                "profile": "test-pattern",
                "params": {"pattern": "testsrc2"},
                "seed": 3,
                "reason": "저비용 모션 샷",
                "alternatives": [],
            },
        ]
    }


async def main(out_dir: Path, evidence_path: Path) -> int:
    t_start = time.monotonic()
    evidence: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host_memory_before": host_memory(),
        "steps": {},
    }
    failures: list[str] = []

    def check(name: str, ok: bool, detail=None) -> None:
        evidence["steps"].setdefault("checks", []).append(
            {"name": name, "ok": bool(ok), "detail": detail}
        )
        print(
            ("PASS " if ok else "FAIL ")
            + name
            + ("" if detail is None else f"  {detail}")
        )
        if not ok:
            failures.append(name)

    store = DirectorStore(out_dir / "productions")
    adapters = [FfmpegAdapter(), RemotionAdapter(), ThreeAdapter(), ComfyAdapter()]
    registry = EngineRegistry(out_dir / "engine-registry.json", adapters)
    pipeline = ProductionPipeline(store, registry)

    # 1. 실측: probe → 실제 짧은 렌더 smoke. 측정하지 않은 엔진은 후보에서 빠진다.
    check("측정 전에는 자동 선택 후보가 없다", registry.candidates() == [])
    evidence["steps"]["smoke"] = {}
    for engine_id in ("ffmpeg", "remotion", "three"):
        t0 = time.monotonic()
        try:
            smoke = registry.run_smoke(engine_id)
            evidence["steps"]["smoke"][engine_id] = {
                **smoke,
                "total_seconds": round(time.monotonic() - t0, 2),
            }
            check(
                f"{engine_id} 실제 smoke",
                smoke["decode_ok"] and smoke["decoded_frames"] == 24,
                {
                    "wall": smoke["wall_seconds"],
                    "rss_peak_mb": smoke["rss_peak_mb"],
                    "frames": smoke["decoded_frames"],
                },
            )
            registry.set_policy(
                engine_id,
                "auto_eligible",
                "smoke 통과 후 repro 스크립트가 임시 레지스트리에서만 승격",
            )
        except Exception as exc:  # noqa: BLE001 - 실패는 증거로 남긴다
            evidence["steps"]["smoke"][engine_id] = {
                "error": f"{type(exc).__name__}: {exc}"
            }
            check(f"{engine_id} 실제 smoke", False, str(exc)[:200])
    comfy = registry.measure("comfy")
    evidence["steps"]["comfy_probe"] = comfy
    check(
        "comfy는 파일 확인만(available 아님)이고 자동 후보가 아니다",
        comfy["state"] != "available"
        and not any(c["engine"] == "comfy" for c in registry.candidates()),
        comfy["state"],
    )
    evidence["steps"]["candidates"] = registry.candidates()
    if failures:
        evidence["failures"] = failures
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=1, default=str)
        )
        return 1

    # 2. 감독 흐름(고정 응답 fake): 브리프 → 바이블 → 오디오 선행 타임라인 → 샷·엔진 선택. 첫 샷은 일부러 끝이 암전되는 설정(fade 12)이다.
    pid = store.create_production("심야 도시 소개 영상 (smoke)")["manifest"][
        "production_id"
    ]
    audio = store.put_artifact(pid, "click.wav", click_track(), media_type="audio/wav")
    llm = FixedResponseLlm(
        {
            "bible": json.dumps(BIBLE),
            "timeline": json.dumps(TIMELINE),
            "shots": json.dumps(shots_response(fade=12)),
        }
    )
    director = CreativeDirector(pipeline, llm)
    t0 = time.monotonic()
    directed = await director.direct(
        pid,
        "심야 도시 소개 영상",
        audio_sha256=audio["sha256"],
        fps=24,
        size=(640, 360),
    )
    state = store.get_production(pid)
    check(
        "감독 흐름이 G0 데이터로 기록됐다",
        directed["timeline_revision"] == state["timelines"][-1]["revision"]
        and len(state["shots"]) == 3,
        {
            "seconds": round(time.monotonic() - t0, 2),
            "beats": len(state["timelines"][-1]["beats"]),
        },
    )
    snaps = [
        (s["shot_id"], s["start_f"], s["snapped"], s["quantization_error_frames"])
        for s in state["timelines"][-1]["shots"]
    ]
    evidence["steps"]["timeline_cuts"] = snaps
    check(
        "선택된 엔진은 remotion·three·ffmpeg",
        [r[-1]["engine"] for r in state["shots"].values()]
        == ["remotion", "three", "ffmpeg"],
    )

    # 3. 실제 렌더 → 기계 검수. shot-1(fade 12)은 끝 프레임이 암전이라 black_frames로 걸려야 한다.
    evidence["steps"]["render"] = {}
    for shot_id in ("shot-1", "shot-2", "shot-3"):
        t0 = time.monotonic()
        attempt = pipeline.render_shot(pid, shot_id, review=True)
        state = store.get_production(pid)
        receipt = pipeline.attempt_receipt(
            attempt, lambda sha: store.read_artifact(pid, sha)
        )
        match = next(
            r
            for r in state["review_reports"].values()
            if r["attempt_id"] == attempt["attempt_id"]
        )
        evidence["steps"]["render"][shot_id] = {
            "engine": receipt["engine"],
            "wall_seconds": receipt["wall_seconds"],
            "rss_peak_mb": receipt["metrics"].get("rss_peak_mb"),
            "state": attempt["state"],
            "verdict": match["verdict"],
            "criteria": match["criteria"],
            "output_sha256": attempt["outputs"][0]["sha256"],
            "media": receipt["outputs"][0]["media"],
            "elapsed_with_review": round(time.monotonic() - t0, 2),
            "real_execution": receipt["real_execution"],
        }
        check(
            f"{shot_id}({receipt['engine']}) 실제 렌더·receipt",
            attempt["state"] == "review_pending" and receipt["real_execution"] is True,
            {"wall": receipt["wall_seconds"], "verdict": match["verdict"]},
        )
    defects = pipeline.defective_shots(pid)
    evidence["steps"]["defects_before_fix"] = defects
    check(
        "fade-out 암전 샷만 결함으로 잡힌다",
        [d["shot_id"] for d in defects] == ["shot-1"]
        and "black_frames" in defects[0]["detail"],
        defects,
    )

    # 4. 결함 샷만 다시 만든다: 사용자 피드백(덮어쓰기)로 fade_out_frames=0. 주변 샷 hash는 그대로여야 한다.
    before = pipeline.current_outputs(store.get_production(pid))
    revision = store.get_production(pid)["shots"]["shot-1"][-1]["revision"]
    feedback = await submit_feedback(
        pipeline,
        None,
        pid,
        "shot-1",
        expected_revision=revision,
        text="끝에서 화면이 검게 꺼져요. 페이드 없이.",
        idempotency_key="smoke-fix-1",
        overrides={"params": {"fade_out_frames": 0}},
    )
    t0 = time.monotonic()
    fixed = pipeline.execute(pid, feedback["attempt_id"])
    pipeline.review(pid, fixed["attempt_id"])
    after = pipeline.current_outputs(store.get_production(pid))
    evidence["steps"]["rerender"] = {
        "seconds": round(time.monotonic() - t0, 2),
        "before": before,
        "after": after,
        "feedback": feedback,
    }
    check(
        "주변 샷 output hash 불변",
        after["shot-2"] == before["shot-2"] and after["shot-3"] == before["shot-3"],
    )
    check(
        "대상 샷 output hash 변경·결함 해소",
        after["shot-1"] != before["shot-1"] and pipeline.defective_shots(pid) == [],
    )

    # 5. 최종 조립 + 전수 기계 검사(오디오 포함)
    t0 = time.monotonic()
    composition = pipeline.compose_final(pid, audio_sha256=audio["sha256"])
    final = pipeline.review_final(pid, composition["composition_key"])
    final_path = out_dir / "final.mp4"
    final_path.write_bytes(store.read_artifact(pid, composition["output_sha256"]))
    evidence["steps"]["final"] = {
        "seconds": round(time.monotonic() - t0, 2),
        "criteria": final["report"]["criteria"],
        "verdict": final["report"]["verdict"],
        "sha256": composition["output_sha256"],
        "ffprobe": ffprobe(final_path),
        "path": str(final_path),
    }
    check(
        "최종 전수 기계 검사 통과",
        final["report"]["verdict"] == "pass",
        final["report"]["criteria"],
    )
    final_state = store.get_production(pid)
    check(
        "최종 합성 뒤 stale이 정리됐다",
        final_state["stale"]["shots"] == []
        and final_state["stale"]["transition_neighbors"] == [],
    )
    check(
        "승인은 어디에도 만들어지지 않았다",
        all(r["approval_event_id"] is None for r in final_state["reviews"])
        and final_state["bibles"][-1]["approval_event_id"] is None,
    )
    evidence["steps"]["shot_ffprobe"] = {}
    for shot_id, sha in after.items():
        path = out_dir / f"{shot_id}.mp4"
        path.write_bytes(store.read_artifact(pid, sha))
        evidence["steps"]["shot_ffprobe"][shot_id] = ffprobe(path)

    evidence["host_memory_after"] = host_memory()
    evidence["total_seconds"] = round(time.monotonic() - t_start, 2)
    evidence["failures"] = failures
    evidence["scope_note"] = (
        "실제 렌더는 Remotion·Three.js(SwiftShader CPU WebGL)·FFmpeg뿐이다. 감독 LLM은 고정 응답 fake이고 Comfy 생성·TTS 합성·GPU 모델·"
        "사람 시청/청취는 실행하지 않았다. 기계 검수는 의미·미감을 판정하지 않는다."
    )
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=1, default=str)
    )
    print(
        f"\nproduction={pid} total={evidence['total_seconds']}s final={final_path} evidence={evidence_path}"
    )
    print("결과:", "모두 통과" if not failures else f"실패 {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--evidence", type=Path, default=None)
    args = parser.parse_args()
    base = args.out or Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    # 매 실행마다 새 폴더를 쓴다. 이전 실행의 레지스트리 측정이 섞이면 "측정 전" 검사가 무의미해진다.
    out = Path(tempfile.mkdtemp(prefix="creative-engines-smoke-", dir=base))
    sys.exit(asyncio.run(main(out, args.evidence or out / "evidence.json")))
