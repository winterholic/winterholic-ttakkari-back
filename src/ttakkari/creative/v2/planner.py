"""Reviewed director storyboard → deterministic execution decisions.

LLM statements are input provenance, never execution evidence or approval. Rich
registry capabilities keep their existing gate; these choices do not promote them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from ttakkari.creative.v2.capabilities import CAPABILITIES, Strict

Strategy = Literal[
    "slideshow", "generative_video", "frame_animation_2d", "scene_3d", "code_motion"
]
ENGINE_IDS = {
    "slideshow": "skill.v1_ffmpeg_slideshow",
    "generative_video": "skill.comfy_video_renderer",
    "frame_animation_2d": "skill.frame_animation_2d",
    "scene_3d": "skill.studio-3d-cinematic",
    "code_motion": "skill.hyperframes_motion",
}
# These describe the currently connected surface, not every claim of a producer.
SURFACES = {
    "slideshow": "uploaded image/video composition; captions and master audio",
    "generative_video": "existing managed-generated video playback; no new generation here",
    "frame_animation_2d": "fixed character walk cycle only; custom rig/action unsupported",
    "scene_3d": "fixed Blender crystal/orbit only; custom world/acting unsupported",
    "code_motion": "three-scene Kafka flow template only; browser IPC preflight required",
}


class ShotDirection(Strict):
    shot_id: str = Field(min_length=1, max_length=60)
    action: str = Field(min_length=1, max_length=1000)
    layers: list[Literal["visual", "captions", "audio"]] = Field(
        default_factory=lambda: ["visual"], max_length=3
    )
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def known_capabilities(self):
        if any(c not in CAPABILITIES for c in self.required_capabilities):
            raise ValueError("unknown required capability")
        return self


class StrategyAlternative(Strict):
    strategy: Strategy
    rationale: str = Field(min_length=1, max_length=1500)


class SceneDirection(Strict):
    scene_id: str = Field(min_length=1, max_length=50)
    strategy: Strategy
    preset: Literal["walk_cycle", "crystal_orbit", "kafka_flow"] | None = None
    interpolation_mode: Literal["hold", "linear"] | None = None
    rationale: str = Field(min_length=1, max_length=1500)
    alternatives: list[StrategyAlternative] = Field(default_factory=list, max_length=5)
    shots: list[ShotDirection] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def supported_interpolation(self):
        if self.interpolation_mode is not None and self.strategy != "frame_animation_2d":
            raise ValueError("interpolation_mode requires frame_animation_2d")
        return self


class ProductionDirection(Strict):
    source: Literal["llm", "user_structured"]
    model: str | None = Field(default=None, max_length=120)
    reviewed: bool = False
    run_id: str | None = None
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    intent: str = Field(min_length=1, max_length=3000)
    domain_knowledge: list[str] = Field(default_factory=list, max_length=30)
    scenes: list[SceneDirection] = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def reviewed_input(self):
        if not self.reviewed or (self.source == "llm" and not self.model):
            raise ValueError(
                "director input must be reviewed; LLM source requires model provenance"
            )
        ids = [s.scene_id for s in self.scenes]
        shots = [shot.shot_id for s in self.scenes for shot in s.shots]
        if len(set(ids)) != len(ids) or len(set(shots)) != len(shots):
            raise ValueError("duplicate scene/shot id")
        return self


def apply_direction(
    scenes: list[dict[str, Any]], direction: ProductionDirection
) -> None:
    """Only explicit reviewed choices, with no keyword guessing or implicit fallback."""
    by_id = {s.scene_id: s for s in direction.scenes}
    if set(by_id) != {s["scene_id"] for s in scenes}:
        raise ValueError("director scenes must exactly match plan scenes")
    for scene in scenes:
        selected = by_id[scene["scene_id"]]
        # Omission keeps legacy hold inputs and removes a previous explicit choice.
        scene.pop("interpolation_mode", None)
        if selected.interpolation_mode is not None:
            scene["interpolation_mode"] = selected.interpolation_mode
        engine = ENGINE_IDS[selected.strategy]
        alternatives = [
            {
                "strategy": strategy,
                "engine": eid,
                "scope": SURFACES[strategy],
                "automatic": False,
            }
            for strategy, eid in ENGINE_IDS.items()
            if strategy != selected.strategy
        ]
        if selected.alternatives:
            alternatives = [{"strategy": a.strategy, "engine": ENGINE_IDS[a.strategy],
                             "reason": a.rationale, "scope": SURFACES[a.strategy], "automatic": False}
                            for a in selected.alternatives]
        estimates = {
            "measured": False,
            "cost": "no new paid purchase; generation cost not estimated",
            "latency": "unmeasured; offline 3D/frame rendering may dominate",
            "quality": SURFACES[selected.strategy],
        }
        scene.update(
            strategy=selected.strategy,
            selected_engine=engine,
            preset=selected.preset,
            strategy_rationale={
                "reason": selected.rationale,
                "source": direction.source,
                "model": direction.model,
                "scope": SURFACES[selected.strategy],
                "feasibility": "requires registry gate, engine preflight and spec validation",
                "alternatives": alternatives,
                "cost_latency_quality": estimates,
            },
            shots=[
                {
                    **shot.model_dump(mode="json"),
                    "scene_id": scene["scene_id"],
                    "index": i,
                    "strategy": selected.strategy,
                    "selected_providers": [engine],
                    "camera": scene["camera"],
                    "motion": scene["motion"],
                    "seconds": scene["seconds"],
                    "start_time": scene["start_time"],
                    "end_time": scene["end_time"],
                    "fallback_alternatives": alternatives,
                    "cost_latency_quality": estimates,
                    "layer_tools": {
                        layer: engine if layer == "visual" else "tool.ffmpeg"
                        for layer in shot.layers
                    },
                }
                for i, shot in enumerate(selected.shots)
            ],
        )
