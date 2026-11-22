"""장면 요구에서 managed Comfy 프로필 순서를 고른다. 순수 함수이며 아무것도 실행하지 않는다.

장면이 필요로 하는 것은 둘 중 하나다.
- still: 정지 키프레임 한 장(FLUX klein). 카메라 움직임은 렌더러가 입힌다.
- video: 카메라가 움직이는 클립. 생성형 영상 프로필을 먼저 시도하고, 모두 쓸 수 없을 때만 키프레임 이미지에
  카메라 움직임을 입힌 클립으로 내려간다. 내려간 사유는 호출자가 route 기록에 남긴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ttakkari import managed_comfy

KEYFRAME_PROFILE = "flux2-klein-t2i-16x9"
I2V_PROFILE = "wan22-5b-i2v-16x9"
T2V_LONG_PROFILE = "wan22-5b-t2v-long-16x9"
T2V_DRAFT_PROFILE = "wan22-5b-draft"
MUSIC_PROFILE = "acestep15-music"

# 사다리 단계 이름. 각 단계가 쓰는 프로필과 생성형 여부.
STEP_PROFILES: dict[str, tuple[str | None, bool]] = {
    "keyframe": (KEYFRAME_PROFILE, False),
    "keyframe_i2v": (I2V_PROFILE, True),
    "t2v": (T2V_LONG_PROFILE, True),
    "t2v_draft": (T2V_DRAFT_PROFILE, True),
    "camera_motion": (KEYFRAME_PROFILE, False),
}
_VIDEO_CAPABILITIES = {"gen.video_i2v", "gen.video_t2v"}


@dataclass(frozen=True)
class SceneRoute:
    scene_id: str
    need: str  # "still" | "video"
    ladder: tuple[str, ...]
    clip_seconds: float | None
    reason: str

    def record(self) -> dict[str, Any]:
        return {"scene_id": self.scene_id, "need": self.need, "ladder": list(self.ladder),
                "clip_seconds": self.clip_seconds, "reason": self.reason}


def scene_need(scene: dict[str, Any]) -> tuple[str, str]:
    """(need, 사유). 계획이 명시한 것만 보고, 명시가 없으면 기존 자동 생성처럼 움직이는 클립으로 본다."""
    if scene.get("strategy") == "generative_video":
        return "video", "strategy=generative_video"
    kinds: list[str] = []
    capabilities: set[str] = set()
    for shot in scene.get("shots") or []:
        contract = shot.get("output_contract") or {}
        if contract.get("kind"):
            kinds.append(contract["kind"])
        capabilities.update(shot.get("required_capabilities") or [])
    if "video" in kinds or capabilities & _VIDEO_CAPABILITIES:
        return "video", "shot 이 영상 출력이나 영상 생성 능력을 요구"
    if kinds and set(kinds) <= {"image"}:
        return "still", "모든 shot 의 output_contract.kind 가 image"
    if "gen.image" in capabilities:
        return "still", "shot 이 이미지 생성 능력만 요구"
    return "video", "정지 이미지 요구가 없어 움직이는 클립을 기본으로 함"


def route_scene(scene: dict[str, Any]) -> SceneRoute:
    scene_id = str(scene["scene_id"])
    need, reason = scene_need(scene)
    if need == "still":
        return SceneRoute(scene_id, "still", ("keyframe",), None, reason)
    i2v_max = _max_seconds(I2V_PROFILE)
    seconds = float(scene.get("seconds") or 0) or None
    if seconds is not None and seconds > i2v_max:
        # 이미지→영상은 3초 안팎이 한계다. 더 긴 장면은 텍스트→영상 장시간 변형을 먼저 쓴다.
        ladder = ("t2v", "keyframe_i2v", "t2v_draft", "camera_motion")
        clip = min(seconds, _max_seconds(T2V_LONG_PROFILE))
        why = f"{reason}; 장면 {seconds:g}초가 이미지→영상 한도 {i2v_max:g}초보다 길어 텍스트→영상 장시간을 먼저 사용"
    else:
        ladder = ("keyframe_i2v", "t2v", "t2v_draft", "camera_motion")
        clip = seconds if seconds is not None else None
        why = f"{reason}; 키프레임에서 이미지→영상을 먼저 사용"
    return SceneRoute(scene_id, "video", ladder, clip, why)


def _profile(profile_id: str) -> managed_comfy.ComfyProfile:
    return managed_comfy.PROFILES[profile_id]


def _max_seconds(profile_id: str) -> float:
    profile = _profile(profile_id)
    assert profile.frames and profile.fps
    return profile.frames[2] / profile.fps


def clip_options(profile_id: str, seconds: float | None) -> dict[str, Any]:
    """프로필 길이 범위로 자른 duration_seconds. 장면 길이를 모르면 프로필 기본값을 쓴다(옵션 생략)."""
    profile = _profile(profile_id)
    if seconds is None or not profile.frames or not profile.fps:
        return {}
    low, high = profile.frames[0] / profile.fps, profile.frames[2] / profile.fps
    return {"duration_seconds": min(max(seconds, low), high)}


def profile_blockers(profile_id: str, *, models_root, extra_files=()) -> list[str]:
    """프로필이 요구하는 파일이 디스크에 없으면 사유 목록. 모델을 로드하거나 서버를 띄우지 않는다."""
    from ttakkari.managed_comfy_receipt import model_entry

    profile = managed_comfy.PROFILES.get(profile_id)
    if profile is None:
        return [f"프로필 {profile_id} 이 등록돼 있지 않다"]
    if profile.engine == "acestep":
        missing = [str(p) for p in (managed_comfy.ACESTEP_RUNNER, managed_comfy.ACESTEP_PYTHON,
                                    managed_comfy.ACESTEP_TURBO_CHECKPOINT, *extra_files) if not p.is_file()]
        return [f"ACE-Step 파일이 없다: {', '.join(missing)}"] if missing else []
    missing_models = [m["name"] for m in (model_entry(n, models_root, hash_limit=0) for n in profile.models)
                      if not m.get("found")]
    return [f"모델 파일이 없다: {', '.join(missing_models)}"] if missing_models else []
