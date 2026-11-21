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


class ShotInput(Strict):
    """샷이 쓰기로 한 자산. 파일 경로·URL은 받지 않고 자산 ID와 내용 해시로만 가리킨다."""

    asset_id: str = Field(min_length=1, max_length=120)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    role: str = Field(default="", max_length=40)


class ShotOutputContract(Strict):
    kind: Literal["video", "image", "audio", "captions"] = "video"
    min_seconds: float | None = Field(default=None, gt=0, le=900)
    max_seconds: float | None = Field(default=None, gt=0, le=900)
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def ordered(self):
        if (
            self.min_seconds is not None
            and self.max_seconds is not None
            and self.min_seconds > self.max_seconds
        ):
            raise ValueError("output_contract min_seconds must not exceed max_seconds")
        return self


class QualityCriterion(Strict):
    id: str = Field(min_length=1, max_length=60)
    text: str = Field(min_length=1, max_length=400)
    hard: bool = False


class RetryPolicy(Strict):
    """실패한 샷을 다시 시도하는 규칙. 값이 없으면 자동 재시도는 없다."""

    max_attempts: int = Field(default=1, ge=1, le=5)
    on_failure: Literal["stop", "retry_same", "ask_user"] = "ask_user"


class ShotDirection(Strict):
    shot_id: str = Field(min_length=1, max_length=60)
    action: str = Field(min_length=1, max_length=1000)
    layers: list[Literal["visual", "captions", "audio"]] = Field(
        default_factory=lambda: ["visual"], max_length=3
    )
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)
    # 아래 다섯 필드는 단위별 제작 계약이다(백엔드 갭 §4). 모두 선택이며 없던 시절의 계획·입력과 같은 뜻이다.
    # 엔진이 지금 소비하는 것은 아니다. 소비하지 못하는 엔진은 validate_spec에서 명시적으로 거부한다.
    inputs: list[ShotInput] = Field(default_factory=list, max_length=20)
    output_contract: ShotOutputContract | None = None
    quality_criteria: list[QualityCriterion] = Field(default_factory=list, max_length=20)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    retry_policy: RetryPolicy | None = None

    @model_validator(mode="after")
    def known_capabilities(self):
        if any(c not in CAPABILITIES for c in self.required_capabilities):
            raise ValueError("unknown required capability")
        if self.shot_id in self.depends_on or len(set(self.depends_on)) != len(
            self.depends_on
        ):
            raise ValueError("depends_on must be unique and exclude the shot itself")
        if len({c.id for c in self.quality_criteria}) != len(self.quality_criteria):
            raise ValueError("duplicate quality criterion id")
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
    # 계약은 장면당 여러 샷을 표현할 수 있지만 엔진은 아직 장면당 한 샷만 실행한다(engines·dispatch의 검증이 막는다).
    shots: list[ShotDirection] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def supported_interpolation(self):
        if self.interpolation_mode is not None and self.strategy != "frame_animation_2d":
            raise ValueError("interpolation_mode requires frame_animation_2d")
        return self


class SequenceDirection(Strict):
    """장면 묶음. 장편 계층 Sequence→Scene→Shot→Layer의 두 번째 단계이며, 장면 순서는 scenes가 정한다."""

    sequence_id: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(default="", max_length=500)
    scene_ids: list[str] = Field(min_length=1, max_length=400)


def check_sequences(sequences: list[SequenceDirection], scene_ids: list[str]) -> None:
    """시퀀스가 있으면 모든 장면이 정확히 한 시퀀스에 속해야 한다. 조용한 누락·중복을 허용하지 않는다."""
    if not sequences:
        return
    if len({q.sequence_id for q in sequences}) != len(sequences):
        raise ValueError("duplicate sequence id")
    members = [sid for q in sequences for sid in q.scene_ids]
    if len(set(members)) != len(members) or set(members) != set(scene_ids):
        raise ValueError("sequences must contain every scene exactly once")


class ProductionDirection(Strict):
    source: Literal["llm", "user_structured"]
    model: str | None = Field(default=None, max_length=120)
    reviewed: bool = False
    run_id: str | None = None
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    intent: str = Field(min_length=1, max_length=3000)
    domain_knowledge: list[str] = Field(default_factory=list, max_length=30)
    sequences: list[SequenceDirection] = Field(default_factory=list, max_length=100)
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
        check_sequences(self.sequences, ids)
        deps = {
            shot.shot_id: shot.depends_on for s in self.scenes for shot in s.shots
        }
        if any(d not in deps for ds in deps.values() for d in ds):
            raise ValueError("depends_on must reference existing shot ids")
        done: set[str] = set()
        pending = dict(deps)
        while pending:
            ready = [k for k, ds in pending.items() if all(d in done for d in ds)]
            if not ready:
                raise ValueError("shot depends_on must not form a cycle")
            for k in ready:
                done.add(k)
                del pending[k]
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
