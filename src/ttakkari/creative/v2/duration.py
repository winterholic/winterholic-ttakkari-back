"""통합 길이 정책: AI 자동 결정 + 선택적 희망 러닝타임.

세 모드 설계는 폐기됐다. 기본은 자동이고, 희망 시간은 soft target이다.
strict는 사용자가 명시한 정확한 길이(exact)와 플랫폼 상한(platform_cap)뿐이다.
필요 길이는 발화 추정, 비트별 장면 길이, 음악 길이에서 먼저 계산하고, 그 범위 안에서만 목표에 맞춘다.
범위를 벗어나는 목표는 장면을 채우거나 정보를 깎아서 맞추지 않고 설명과 대안으로 돌려준다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MIN_SECONDS = 1.0
MAX_SECONDS = 900.0
PRESET_SECONDS: tuple[int, ...] = (15, 30, 60, 120, 180, 300, 600, 900)
DEFAULT_FPS = 30
POLICY_VERSION = "2026-10-22.1"

# 근거: lab 기록 three-character-talk-v1-v2 (Supertonic 3, 58음절 / 13.299초 = 4.36음절/초). 단일 표본이라
# 실제 오디오가 생기면 measured_speech_seconds가 이 추정을 대체한다.
SPEECH_SYLLABLES_PER_SECOND = 4.4
LATIN_WORDS_PER_SECOND = 2.4
DIGITS_PER_SECOND = 3.0
PAUSE_COMMA = 0.15
PAUSE_SENTENCE = 0.35
SPEECH_LEAD_TAIL_SECONDS = 0.4
SPEED_UP_LIMIT = 1.15
SOFT_TOLERANCE_RATIO = 0.25

# 플랫폼 상한은 시효가 있다. 값마다 근거와 확인일을 함께 둔다.
PLATFORM_CAPS: dict[str, dict[str, Any]] = {
    "youtube_shorts": {
        "label": "YouTube Shorts",
        "max_seconds": 180.0,
        "source": "https://support.google.com/youtube/answer/15424877",
        "as_of": "2026-10-22",
        "note": "정사각·세로 비율에서 3분 이하만 Shorts로 분류된다(2024-10-15 이후 업로드).",
    },
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _reject_bool(value: Any) -> Any:
    # JSON true가 float 1.0으로 조용히 바뀌어 1초 요청이 되는 것을 막는다.
    if isinstance(value, bool):
        raise ValueError("숫자를 입력해 주세요.")  # noqa: TRY004 - pydantic은 ValueError만 검증 오류로 바꾼다
    return value


class DurationRequest(Strict):
    """희망 길이. auto는 값이 없고, 나머지는 1~900초 유한 실수만 받는다."""

    mode: Literal["auto", "preferred", "exact", "platform_cap"] = "auto"
    seconds: float | None = None
    platform: str | None = Field(default=None, max_length=40)
    source: str | None = Field(default=None, max_length=300)
    as_of: str | None = Field(default=None, max_length=20)

    @field_validator("seconds", mode="before")
    @classmethod
    def _no_bool(cls, value):
        return _reject_bool(value)

    @model_validator(mode="after")
    def _check(self):
        if self.mode == "platform_cap" and self.seconds is None and self.platform:
            cap = PLATFORM_CAPS.get(self.platform)
            if cap is None:
                raise ValueError(
                    "알 수 없는 플랫폼입니다. 상한 초와 근거를 직접 입력해 주세요."
                )
            self.seconds = cap["max_seconds"]
            self.source = self.source or cap["source"]
            self.as_of = self.as_of or cap["as_of"]
        if self.mode == "auto":
            if self.seconds is not None:
                raise ValueError("자동 모드에는 초를 지정할 수 없습니다.")
            return self
        if self.seconds is None:
            raise ValueError("희망 시간을 입력해 주세요.")
        if not MIN_SECONDS <= self.seconds <= MAX_SECONDS:
            raise ValueError(
                f"길이는 {MIN_SECONDS:g}~{MAX_SECONDS:g}초 범위여야 합니다."
            )
        if self.mode == "platform_cap" and not (self.source and self.as_of):
            raise ValueError("플랫폼 상한은 근거(source)와 확인일(as_of)이 필요합니다.")
        return self

    @property
    def strict(self) -> bool:
        return self.mode in ("exact", "platform_cap")


class Beat(Strict):
    """장면 하나가 전달해야 하는 내용 단위. 길이의 바닥은 실제 발화가 정한다."""

    id: str = Field(min_length=1, max_length=50)
    title: str = Field(default="", max_length=160)
    narration: str = Field(default="", max_length=4000)
    info_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    essential: bool = True
    min_seconds: float | None = Field(default=None, ge=0.0, le=900.0)
    ideal_seconds: float | None = Field(default=None, ge=0.0, le=900.0)
    max_seconds: float | None = Field(default=None, ge=0.0, le=900.0)
    measured_speech_seconds: float | None = Field(default=None, ge=0.0, le=900.0)

    @field_validator(
        "min_seconds",
        "ideal_seconds",
        "max_seconds",
        "measured_speech_seconds",
        mode="before",
    )
    @classmethod
    def _no_bool(cls, value):
        return _reject_bool(value)


class ContentProfile(Strict):
    beats: list[Beat] = Field(min_length=1, max_length=400)
    music_seconds: float | None = Field(default=None, gt=0.0, le=900.0)
    fps: int = Field(default=DEFAULT_FPS, ge=12, le=120)
    min_scene_seconds: float = Field(default=1.0, ge=0.5, le=30.0)

    @field_validator("music_seconds", mode="before")
    @classmethod
    def _no_bool(cls, value):
        return _reject_bool(value)

    @model_validator(mode="after")
    def _unique(self):
        if len({b.id for b in self.beats}) != len(self.beats):
            raise ValueError("비트 ID가 중복됐습니다.")
        return self


_HANGUL = re.compile(r"[가-힣]")
_LATIN_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_DIGIT = re.compile(r"[0-9]")
_SENTENCE_END = re.compile(r"[.!?。！？…]+")
_COMMA = re.compile(r"[,;:、，]")


def estimate_speech_seconds(text: str) -> float:
    """발화 시간 추정. 한글은 음절, 영문은 단어, 숫자는 자리수로 센다."""
    if not text.strip():
        return 0.0
    seconds = len(_HANGUL.findall(text)) / SPEECH_SYLLABLES_PER_SECOND
    seconds += len(_LATIN_WORD.findall(text)) / LATIN_WORDS_PER_SECOND
    seconds += len(_DIGIT.findall(text)) / DIGITS_PER_SECOND
    seconds += len(_SENTENCE_END.findall(text)) * PAUSE_SENTENCE
    seconds += len(_COMMA.findall(text)) * PAUSE_COMMA
    return round(seconds, 3)


class BeatNeed(Strict):
    beat_id: str
    speech_seconds: float
    speech_source: Literal["none", "estimate", "measured"]
    floor: float
    ideal: float
    ceiling: float
    essential: bool


class NeededRuntime(Strict):
    """콘텐츠가 요구하는 길이. floor 미만은 정보 손실, ceiling 초과는 반복·정지 채우기다."""

    min_seconds: float
    ideal_seconds: float
    max_seconds: float
    speech_seconds: float
    music_seconds: float | None
    beats: list[BeatNeed]
    reasons: list[str]


def _beat_need(beat: Beat, profile: ContentProfile) -> BeatNeed:
    if beat.measured_speech_seconds is not None:
        speech, source = beat.measured_speech_seconds, "measured"
    elif beat.narration.strip():
        speech, source = estimate_speech_seconds(beat.narration), "estimate"
    else:
        speech, source = 0.0, "none"
    spoken_floor = speech + SPEECH_LEAD_TAIL_SECONDS if speech > 0 else 0.0
    floor = max(beat.min_seconds or 0.0, profile.min_scene_seconds, spoken_floor)
    visual_ideal = (
        beat.ideal_seconds
        if beat.ideal_seconds is not None
        else 3.0 + 2.0 * beat.info_weight
    )
    ideal = max(floor, visual_ideal, spoken_floor + 0.8 if speech > 0 else 0.0)
    ceiling = (
        beat.max_seconds
        if beat.max_seconds is not None
        else max(ideal * 1.6, floor + 3.0)
    )
    ceiling = max(ceiling, ideal)
    return BeatNeed(
        beat_id=beat.id,
        speech_seconds=round(speech, 3),
        speech_source=source,
        floor=round(floor, 3),
        ideal=round(ideal, 3),
        ceiling=round(ceiling, 3),
        essential=beat.essential,
    )


def _frames_ceil(seconds: float, fps: int) -> int:
    return math.ceil(seconds * fps - 1e-9)


def _frames_floor(seconds: float, fps: int) -> int:
    return math.floor(seconds * fps + 1e-9)


def compute_needed(profile: ContentProfile) -> NeededRuntime:
    needs = [_beat_need(b, profile) for b in profile.beats]
    fps = profile.fps
    # 합계는 프레임 정수로 맞춘다. 초 단위 합과 프레임 합이 어긋나는 것을 막기 위해서다.
    lo = sum(_frames_ceil(n.floor, fps) for n in needs) / fps
    ideal = (
        sum(max(_frames_ceil(n.floor, fps), round(n.ideal * fps)) for n in needs) / fps
    )
    hi = (
        sum(
            max(_frames_ceil(n.floor, fps), _frames_floor(n.ceiling, fps))
            for n in needs
        )
        / fps
    )
    reasons = [
        f"장면 {len(needs)}개: 발화 바닥 합 {lo:.1f}초, 이상 {ideal:.1f}초, 반복 없이 늘릴 수 있는 한계 {hi:.1f}초.",
    ]
    speech_total = round(sum(n.speech_seconds for n in needs), 3)
    if speech_total > 0:
        measured = sum(1 for n in needs if n.speech_source == "measured")
        reasons.append(
            f"발화 {speech_total:.1f}초({measured}/{sum(1 for n in needs if n.speech_source != 'none')}개 실측, "
            f"나머지는 {SPEECH_SYLLABLES_PER_SECOND}음절/초 추정)."
        )
    music = profile.music_seconds
    if music is not None:
        music_frames = round(music * fps)
        lo_f, ideal_f, hi_f = round(lo * fps), round(ideal * fps), round(hi * fps)
        if lo_f > music_frames:
            reasons.append(
                f"음악 {music:.1f}초보다 발화·장면 바닥 합({lo:.1f}초)이 길다. 음악을 늘리거나 내용을 나눠야 한다."
            )
            ideal_f = hi_f = lo_f
        else:
            lo_f = ideal_f = hi_f = music_frames
            reasons.append(f"음악 {music:.1f}초가 전체 길이를 정한다.")
        lo, ideal, hi = lo_f / fps, ideal_f / fps, hi_f / fps
    return NeededRuntime(
        min_seconds=round(lo, 3),
        ideal_seconds=round(ideal, 3),
        max_seconds=round(hi, 3),
        speech_seconds=speech_total,
        music_seconds=music,
        beats=needs,
        reasons=reasons,
    )


class SceneAllocation(Strict):
    beat_id: str
    start_frame: int
    end_frame: int
    seconds: float
    floor: float
    ideal: float
    ceiling: float
    speech_seconds: float
    state: Literal["ok", "compressed", "stretched"]


class Alternative(Strict):
    kind: Literal[
        "relax_to_feasible",
        "drop_nonessential",
        "speed_up_speech",
        "split_parts",
        "extend_content",
        "raise_cap",
    ]
    label: str
    seconds: float | None = None
    detail: str
    requires_user_approval: bool = True
    feasible: bool = True


class DurationPlan(Strict):
    policy_version: str = POLICY_VERSION
    status: Literal["auto", "target_met", "deviated", "strict_met", "conflict"]
    deviation_level: Literal["none", "minor", "excessive"] = "none"
    request: DurationRequest
    fps: int
    needed: NeededRuntime
    planned_seconds: float | None
    planned_frames: int | None
    scenes: list[SceneAllocation]
    reasons: list[str]
    warnings: list[str] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    estimator: dict[str, float]
    basis_sha256: str


def _estimator_params() -> dict[str, float]:
    return {
        "speech_syllables_per_second": SPEECH_SYLLABLES_PER_SECOND,
        "latin_words_per_second": LATIN_WORDS_PER_SECOND,
        "digits_per_second": DIGITS_PER_SECOND,
        "lead_tail_seconds": SPEECH_LEAD_TAIL_SECONDS,
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def allocate_frames(
    needs: list[BeatNeed],
    fps: int,
    total_frames: int,
    locked: dict[str, int] | None = None,
) -> list[int]:
    """총 프레임을 장면별 [floor, ceiling] 안에서 배분한다. 합이 total_frames와 정확히 같다.

    floor→ideal→ceiling 순으로 구간 선형 보간하고, 남는 프레임은 소수부가 큰 장면부터 준다.
    """
    locked = locked or {}
    lo = [_frames_ceil(n.floor, fps) for n in needs]
    hi = [max(lo[i], _frames_floor(n.ceiling, fps)) for i, n in enumerate(needs)]
    mid = [min(max(round(n.ideal * fps), lo[i]), hi[i]) for i, n in enumerate(needs)]
    free = [i for i, n in enumerate(needs) if n.beat_id not in locked]
    fixed = sum(locked.get(n.beat_id, 0) for n in needs)
    budget = total_frames - fixed
    values = [float(locked.get(n.beat_id, 0)) for n in needs]
    lo_sum, mid_sum, hi_sum = (sum(arr[i] for i in free) for arr in (lo, mid, hi))
    if budget < lo_sum:
        raise ValueError("목표 프레임이 장면 최소 길이보다 짧습니다.")
    if budget > hi_sum:
        # 음악 길이가 콘텐츠 한계보다 길 때뿐이다. 한계 비율로 늘리고 호출부가 경고한다.
        for i in free:
            values[i] = hi[i] * budget / hi_sum
    elif budget <= mid_sum:
        t = 1.0 if mid_sum == lo_sum else (budget - lo_sum) / (mid_sum - lo_sum)
        for i in free:
            values[i] = lo[i] + t * (mid[i] - lo[i])
    else:
        t = 1.0 if hi_sum == mid_sum else (budget - mid_sum) / (hi_sum - mid_sum)
        for i in free:
            values[i] = mid[i] + t * (hi[i] - mid[i])
    base = [math.floor(v + 1e-9) for v in values]
    leftover = total_frames - sum(base)
    order = sorted(free, key=lambda i: (-(values[i] - base[i]), i))
    for i in order[: max(leftover, 0)]:
        base[i] += 1
    return base


def _scene_allocations(
    needs: list[BeatNeed], frames: list[int], fps: int
) -> list[SceneAllocation]:
    out, cursor = [], 0
    for need, count in zip(needs, frames, strict=True):
        seconds = count / fps
        state: Literal["ok", "compressed", "stretched"] = "ok"
        if seconds < need.ideal * 0.85:
            state = "compressed"
        elif seconds > need.ideal * 1.3:
            state = "stretched"
        out.append(
            SceneAllocation(
                beat_id=need.beat_id,
                start_frame=cursor,
                end_frame=cursor + count,
                seconds=round(seconds, 3),
                floor=need.floor,
                ideal=need.ideal,
                ceiling=need.ceiling,
                speech_seconds=need.speech_seconds,
                state=state,
            )
        )
        cursor += count
    return out


def _split_parts(needs: list[BeatNeed], limit_seconds: float) -> int | None:
    """순서를 유지한 채 각 편의 바닥 합이 limit 이하가 되도록 나눈 최소 편 수. 한 장면이 limit보다 길면 불가."""
    if any(n.floor > limit_seconds for n in needs):
        return None
    parts, acc = 1, 0.0
    for n in needs:
        if acc + n.floor > limit_seconds + 1e-9:
            parts, acc = parts + 1, 0.0
        acc += n.floor
    return parts


def _conflict_alternatives(
    profile: ContentProfile, needed: NeededRuntime, target: float, *, above: bool
) -> list[Alternative]:
    alts: list[Alternative] = []
    fps = profile.fps
    if not above:
        floor_total = needed.min_seconds
        feasible = _frames_ceil(floor_total, fps) / fps
        alts.append(
            Alternative(
                kind="relax_to_feasible",
                label="필요한 최소 길이로 조정",
                seconds=round(feasible, 3),
                detail=f"정보를 지우지 않고 담을 수 있는 가장 짧은 길이는 {feasible:.1f}초입니다.",
            )
        )
        optional = [n for n in needed.beats if not n.essential]
        optional_floor = sum(_frames_ceil(n.floor, fps) for n in optional) / fps
        if optional and floor_total - optional_floor <= target + 1e-9:
            alts.append(
                Alternative(
                    kind="drop_nonessential",
                    label="선택 장면 제외",
                    seconds=round(floor_total - optional_floor, 3),
                    detail="필수가 아닌 장면 "
                    + ", ".join(n.beat_id for n in optional)
                    + "을 빼면 목표 안에 들어갑니다.",
                )
            )
        speech_floor = sum(n.floor for n in needed.beats if n.speech_seconds > 0)
        silent_floor = floor_total - speech_floor
        spoken_budget = target - silent_floor
        if speech_floor > 0 and spoken_budget > 0:
            factor = speech_floor / spoken_budget
            alts.append(
                Alternative(
                    kind="speed_up_speech",
                    label=f"발화 {factor:.2f}배속",
                    seconds=round(target, 3),
                    detail=f"발화 구간을 {factor:.2f}배로 빠르게 하면 맞출 수 있습니다. 명료도 한계로 {SPEED_UP_LIMIT}배까지만 권합니다.",
                    feasible=factor <= SPEED_UP_LIMIT,
                )
            )
        parts = _split_parts(needed.beats, target)
        alts.append(
            Alternative(
                kind="split_parts",
                label="여러 편으로 분할",
                seconds=round(target, 3),
                detail=(
                    f"장면 순서를 유지한 채 {parts}편으로 나누면 편마다 {target:.0f}초 이하입니다."
                    if parts
                    else "한 장면의 필요 길이가 목표보다 길어 분할로도 맞출 수 없습니다."
                ),
                feasible=parts is not None,
            )
        )
    else:
        extra = target - needed.max_seconds
        alts.append(
            Alternative(
                kind="extend_content",
                label="내용 추가",
                seconds=round(target, 3),
                detail=f"반복 없이 담을 수 있는 한계가 {needed.max_seconds:.1f}초입니다. {extra:.1f}초만큼 새 정보·장면이 필요합니다.",
            )
        )
        alts.append(
            Alternative(
                kind="relax_to_feasible",
                label="콘텐츠가 담을 수 있는 최대 길이로 조정",
                seconds=needed.max_seconds,
                detail=f"{needed.max_seconds:.1f}초까지는 정지·반복 없이 늘릴 수 있습니다.",
            )
        )
    return alts


def plan_duration(
    profile: ContentProfile, request: DurationRequest | None = None
) -> DurationPlan:
    request = request or DurationRequest()
    fps = profile.fps
    needed = compute_needed(profile)
    needs = needed.beats
    lo_f = round(needed.min_seconds * fps)
    ideal_f = round(needed.ideal_seconds * fps)
    hi_f = round(needed.max_seconds * fps)
    reasons = list(needed.reasons)
    warnings: list[str] = []
    alternatives: list[Alternative] = []
    level: Literal["none", "minor", "excessive"] = "none"
    basis = _digest(
        {
            "profile": profile.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "policy": POLICY_VERSION,
        }
    )

    def finish(status, frames: int | None) -> DurationPlan:
        scenes: list[SceneAllocation] = []
        if frames is not None and frames > sum(
            max(_frames_ceil(n.floor, fps), _frames_floor(n.ceiling, fps))
            for n in needs
        ):
            warnings.append(
                "음악 길이가 콘텐츠가 반복 없이 담을 수 있는 한계보다 길어 장면이 늘어납니다. 내용을 추가하는 편이 좋습니다."
            )
        if frames is not None:
            scenes = _scene_allocations(needs, _allocate_exact(needs, fps, frames), fps)
        return DurationPlan(
            status=status,
            deviation_level=level,
            request=request,
            fps=fps,
            needed=needed,
            planned_seconds=None if frames is None else round(frames / fps, 3),
            planned_frames=frames,
            scenes=scenes,
            reasons=reasons,
            warnings=warnings,
            alternatives=alternatives,
            estimator=_estimator_params(),
            basis_sha256=basis,
        )

    if request.mode == "auto":
        reasons.append(
            f"희망 시간이 없어 콘텐츠 기준 {ideal_f / fps:.1f}초로 정했습니다."
        )
        return finish("auto", ideal_f)

    target_f = round(request.seconds * fps)  # type: ignore[operator]
    target_s = target_f / fps

    if request.mode == "preferred":
        frames = min(max(target_f, lo_f), hi_f)
        if frames == target_f:
            reasons.append(
                f"희망 {request.seconds:g}초는 콘텐츠 범위({lo_f / fps:.1f}~{hi_f / fps:.1f}초) 안이라 장면 길이를 맞췄습니다."
            )
            drift = abs(target_f - ideal_f) / max(ideal_f, 1)
            if drift > SOFT_TOLERANCE_RATIO:
                warnings.append(
                    f"이상 길이({ideal_f / fps:.1f}초)와 {drift:.0%} 차이라 일부 장면이 "
                    + ("압축" if target_f < ideal_f else "늘려")
                    + "졌습니다. 장면별 상태를 확인해 주세요."
                )
            return finish("target_met", frames)
        ratio = abs(frames - target_f) / target_f
        level = "excessive" if ratio > SOFT_TOLERANCE_RATIO else "minor"
        below = target_f < lo_f
        reasons.append(
            f"희망 {request.seconds:g}초는 희망값일 뿐이라 "
            + (
                f"정보를 지우지 않는 최소 {frames / fps:.1f}초로 정했습니다."
                if below
                else f"반복 없이 담을 수 있는 최대 {frames / fps:.1f}초로 정했습니다."
            )
            + f" 목표와 {ratio:.0%} 차이입니다."
        )
        alternatives = _conflict_alternatives(
            profile, needed, target_s, above=not below
        )
        return finish("deviated", frames)

    if request.mode == "exact":
        if lo_f <= target_f <= hi_f:
            reasons.append(
                f"정확히 {request.seconds:g}초({target_f}프레임)가 콘텐츠 범위 안이라 맞췄습니다."
            )
            return finish("strict_met", target_f)
        below = target_f < lo_f
        reasons.append(
            f"정확히 {request.seconds:g}초로 지정됐지만 콘텐츠는 {lo_f / fps:.1f}~{hi_f / fps:.1f}초가 필요합니다. "
            "정보 삭제나 채우기로 맞추지 않고 선택지를 제시합니다."
        )
        alternatives = _conflict_alternatives(
            profile, needed, target_s, above=not below
        )
        return finish("conflict", None)

    # platform_cap: 상한 이하이기만 하면 된다.
    source = f"{request.platform or '사용자 지정'} 상한 {request.seconds:g}초(근거 {request.source}, 확인 {request.as_of})"
    if lo_f > target_f:
        reasons.append(f"{source}보다 콘텐츠 최소 길이 {lo_f / fps:.1f}초가 깁니다.")
        alternatives = _conflict_alternatives(profile, needed, target_s, above=False)
        return finish("conflict", None)
    frames = min(ideal_f, target_f)
    if frames < ideal_f:
        reasons.append(
            f"{source}에 맞춰 이상 길이 {ideal_f / fps:.1f}초를 {frames / fps:.1f}초로 압축했습니다. 필수 정보는 유지됩니다."
        )
    else:
        reasons.append(f"{source} 안에서 이상 길이 {ideal_f / fps:.1f}초로 정했습니다.")
    return finish("strict_met", frames)


def _allocate_exact(needs: list[BeatNeed], fps: int, frames: int) -> list[int]:
    return allocate_frames(needs, fps, frames)


class SceneEdit(Strict):
    """사용자가 편집한 장면. seconds는 사용자가 정한 값이며 재계산이 바꾸지 않는다."""

    id: str = Field(min_length=1, max_length=50)
    title: str = Field(default="", max_length=160)
    narration: str = Field(default="", max_length=4000)
    seconds: float = Field(ge=0.0, le=900.0)
    info_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    essential: bool = True
    min_seconds: float | None = Field(default=None, ge=0.0, le=900.0)
    max_seconds: float | None = Field(default=None, ge=0.0, le=900.0)
    measured_speech_seconds: float | None = Field(default=None, ge=0.0, le=900.0)

    @field_validator(
        "seconds",
        "min_seconds",
        "max_seconds",
        "measured_speech_seconds",
        mode="before",
    )
    @classmethod
    def _no_bool(cls, value):
        return _reject_bool(value)


class DurationCheck(Strict):
    status: Literal["ok", "violation"]
    total_frames: int
    total_seconds: float
    fps: int
    needed: NeededRuntime
    request: DurationRequest
    target_delta_seconds: float | None
    scenes: list[SceneAllocation]
    errors: list[str]
    warnings: list[str]
    fit_suggestion: DurationPlan | None = None


def recheck_duration(
    edits: list[SceneEdit],
    request: DurationRequest,
    *,
    fps: int = DEFAULT_FPS,
    music_seconds: float | None = None,
    min_scene_seconds: float = 1.0,
) -> DurationCheck:
    """장면 편집 뒤 전체 길이를 프레임 단위로 다시 계산한다.

    편집한 길이는 보존하고, strict 위반·발화 잘림·반복 위험만 알린다. 위반이면 편집 장면을 고정한 맞춤안을 함께 준다.
    """
    if not edits:
        raise ValueError("장면이 하나 이상 필요합니다.")
    beats = [
        Beat(
            id=e.id,
            title=e.title,
            narration=e.narration,
            info_weight=e.info_weight,
            essential=e.essential,
            min_seconds=e.min_seconds,
            max_seconds=e.max_seconds,
            measured_speech_seconds=e.measured_speech_seconds,
        )
        for e in edits
    ]
    profile = ContentProfile(
        beats=beats,
        fps=fps,
        music_seconds=music_seconds,
        min_scene_seconds=min_scene_seconds,
    )
    needed = compute_needed(profile)
    frames = [round(e.seconds * fps) for e in edits]
    errors: list[str] = []
    warnings: list[str] = []
    for need, count in zip(needed.beats, frames, strict=True):
        floor_f = _frames_ceil(need.floor, fps)
        ceil_f = max(floor_f, _frames_floor(need.ceiling, fps))
        if count < floor_f:
            errors.append(
                f"장면 {need.beat_id}: {count / fps:.2f}초는 발화·최소 길이 {need.floor:.2f}초보다 짧아 내용이 잘립니다."
            )
        elif count > ceil_f:
            warnings.append(
                f"장면 {need.beat_id}: {count / fps:.1f}초는 반복 없이 유지할 수 있는 {need.ceiling:.1f}초를 넘어 정지·반복 위험이 있습니다."
            )
    total = sum(frames)
    total_s = total / fps
    delta = None
    if request.seconds is not None:
        delta = round(total_s - request.seconds, 3)
        if request.mode == "exact" and total != round(request.seconds * fps):
            errors.append(
                f"정확한 길이 {request.seconds:g}초와 달라 {total_s:.2f}초입니다(차이 {delta:+.2f}초)."
            )
        elif request.mode == "platform_cap" and total > round(request.seconds * fps):
            errors.append(
                f"플랫폼 상한 {request.seconds:g}초를 {delta:.2f}초 초과했습니다."
            )
        elif (
            request.mode == "preferred"
            and request.seconds
            and abs(delta) / request.seconds > SOFT_TOLERANCE_RATIO
        ):
            warnings.append(
                f"희망 {request.seconds:g}초와 {abs(delta) / request.seconds:.0%} 차이({total_s:.1f}초)입니다."
            )
    if music_seconds is not None and abs(total_s - music_seconds) > 1.0 / fps:
        warnings.append(
            f"음악 {music_seconds:.1f}초와 장면 합 {total_s:.1f}초가 다릅니다."
        )
    suggestion = None
    if errors:
        try:
            suggestion = plan_duration(profile, request)
        except ValueError:
            suggestion = None
    return DurationCheck(
        status="violation" if errors else "ok",
        total_frames=total,
        total_seconds=round(total_s, 3),
        fps=fps,
        needed=needed,
        request=request,
        target_delta_seconds=delta,
        scenes=_scene_allocations(needed.beats, frames, fps),
        errors=errors,
        warnings=warnings,
        fit_suggestion=suggestion,
    )


def policy_summary() -> dict[str, Any]:
    return {
        "version": POLICY_VERSION,
        "default_mode": "auto",
        "presets_seconds": list(PRESET_SECONDS),
        "min_seconds": MIN_SECONDS,
        "max_seconds": MAX_SECONDS,
        "default_fps": DEFAULT_FPS,
        "soft_tolerance_ratio": SOFT_TOLERANCE_RATIO,
        "strict_modes": ["exact", "platform_cap"],
        "estimator": _estimator_params(),
        "estimator_basis": "lab 기록 three-character-talk-v1-v2: Supertonic 3 한국어 58음절 13.299초(단일 표본)",
        "platform_caps": PLATFORM_CAPS,
        "note": "희망 시간은 정확한 출력 보증이 아니다. 정확한 길이와 플랫폼 상한만 strict다.",
    }
