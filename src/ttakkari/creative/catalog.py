"""Creative Studio Format, Scene Template, and Tool Catalog.

Provides standard definitions and deterministic validation rules for the 3-tier
assembly structure: Format (Recipe) -> Scene Template -> Tool (Effect).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AnimationType = Literal["still", "zoom_in", "zoom_out", "pan", "pan_down", "pulse"]
TransitionType = Literal["fade", "cut"]
CaptionStyleType = Literal["bottom_card", "center_highlight", "title_card", "code_overlay"]
AspectType = Literal["16:9", "9:16", "1:1"]


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["animation", "transition", "caption_style"]
    name: str
    description: str


class SceneTemplateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    description: str
    category: Literal["hook", "title", "content", "code", "ending"]
    default_duration: float
    min_duration: float
    max_duration: float
    default_animation: AnimationType
    allowed_animations: list[AnimationType]
    default_transition: TransitionType
    allowed_transitions: list[TransitionType]
    default_caption_style: CaptionStyleType
    allowed_caption_styles: list[CaptionStyleType]
    max_caption_lines: int
    max_caption_chars: int


class FormatSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    description: str
    allowed_aspects: list[AspectType]
    default_aspect: AspectType
    min_duration: float
    max_duration: float
    default_duration: float
    min_scenes: int
    max_scenes: int
    has_hook: bool
    default_scene_templates: list[str]
    pacing: Literal["fast", "moderate", "dynamic", "slow_moderate", "very_fast"]
    allowed_animations: list[AnimationType] = Field(
        default_factory=lambda: ["still", "zoom_in", "zoom_out", "pan", "pan_down"]
    )
    allowed_transitions: list[TransitionType] = Field(
        default_factory=lambda: ["fade", "cut"]
    )
    allowed_caption_styles: list[CaptionStyleType] = Field(
        default_factory=lambda: ["bottom_card", "center_highlight", "title_card"]
    )
    min_scene_duration: float = 1.0
    max_scene_duration: float = 30.0
    max_caption_lines: int = 3
    max_caption_chars: int = 100


TOOLS: dict[str, ToolSpec] = {
    # Camera motions
    "still": ToolSpec(
        id="still", kind="animation", name="정지 (Still)", description="카메라 움직임 없이 원본 구도 유지"
    ),
    "zoom_in": ToolSpec(
        id="zoom_in", kind="animation", name="천천히 확대 (Zoom In)", description="중심을 향해 1.0에서 1.1배로 부드럽게 확대 (켄번즈)"
    ),
    "zoom_out": ToolSpec(
        id="zoom_out", kind="animation", name="천천히 축소 (Zoom Out)", description="1.1배에서 1.0배로 부드럽게 축소"
    ),
    "pan": ToolSpec(
        id="pan", kind="animation", name="좌우 패닝 (Pan)", description="좌측에서 우측으로 수평 카메라 이동"
    ),
    "pan_down": ToolSpec(
        id="pan_down", kind="animation", name="수직 패닝 (Pan Down)", description="상단에서 하단으로 수직 이동 (코드/문서 훑기)"
    ),
    "pulse": ToolSpec(
        id="pulse", kind="animation", name="비트 펄스 (Pulse)", description="미세하게 맥동하며 시각적 강조 부여"
    ),
    # Transitions
    "fade": ToolSpec(
        id="fade", kind="transition", name="디졸브 페이드 (Fade)", description="0.25초 부드러운 블랙 디졸브 전환"
    ),
    "cut": ToolSpec(
        id="cut", kind="transition", name="즉시 컷 (Cut)", description="전환 지연 없이 즉각적인 씬 교체"
    ),
    # Caption styles
    "bottom_card": ToolSpec(
        id="bottom_card", kind="caption_style", name="하단 자막 카드", description="화면 하단 반투명 박스에 본문 텍스트 표시"
    ),
    "center_highlight": ToolSpec(
        id="center_highlight", kind="caption_style", name="중앙 강조 텍스트", description="화면 중앙에 대형 볼드 폰트로 핵심 단어 강조"
    ),
    "title_card": ToolSpec(
        id="title_card", kind="caption_style", name="타이틀 헤드라인", description="중앙 다크 배경과 큰 헤드라인으로 제목/챕터 알림"
    ),
    "code_overlay": ToolSpec(
        id="code_overlay", kind="caption_style", name="코드 오버레이", description="상단 정보 바 및 다크 패널 고정폭 텍스트 설명"
    ),
}

SCENE_TEMPLATES: dict[str, SceneTemplateSpec] = {
    "intro_hook": SceneTemplateSpec(
        id="intro_hook",
        name="인트로 훅",
        description="시청자의 시선을 1~3초 내에 사로잡는 강력한 오프닝 장면",
        category="hook",
        default_duration=3.0,
        min_duration=1.5,
        max_duration=6.0,
        default_animation="zoom_in",
        allowed_animations=["zoom_in", "pulse", "pan", "still"],
        default_transition="cut",
        allowed_transitions=["cut", "fade"],
        default_caption_style="center_highlight",
        allowed_caption_styles=["center_highlight", "bottom_card"],
        max_caption_lines=2,
        max_caption_chars=50,
    ),
    "title_card": SceneTemplateSpec(
        id="title_card",
        name="타이틀 카드",
        description="영상이나 챕터의 제목과 핵심 테마를 명시하는 도입 장면",
        category="title",
        default_duration=4.0,
        min_duration=2.0,
        max_duration=8.0,
        default_animation="zoom_in",
        allowed_animations=["zoom_in", "still"],
        default_transition="fade",
        allowed_transitions=["fade", "cut"],
        default_caption_style="title_card",
        allowed_caption_styles=["title_card", "center_highlight"],
        max_caption_lines=2,
        max_caption_chars=60,
    ),
    "lyric_scene": SceneTemplateSpec(
        id="lyric_scene",
        name="가사 장면",
        description="음악 반주와 리듬에 맞춰 가사를 전달하는 비주얼 장면",
        category="content",
        default_duration=6.0,
        min_duration=2.0,
        max_duration=15.0,
        default_animation="zoom_in",
        allowed_animations=["zoom_in", "zoom_out", "pan", "still"],
        default_transition="fade",
        allowed_transitions=["fade", "cut"],
        default_caption_style="bottom_card",
        allowed_caption_styles=["bottom_card", "center_highlight"],
        max_caption_lines=2,
        max_caption_chars=80,
    ),
    "ken_burns": SceneTemplateSpec(
        id="ken_burns",
        name="켄번즈 이미지 서사",
        description="정적 이미지에 서정적 카메라 움직임을 주어 깊이 있는 이야기를 전개하는 장면",
        category="content",
        default_duration=8.0,
        min_duration=4.0,
        max_duration=25.0,
        default_animation="zoom_in",
        allowed_animations=["zoom_in", "zoom_out", "pan", "pan_down", "still"],
        default_transition="fade",
        allowed_transitions=["fade", "cut"],
        default_caption_style="bottom_card",
        allowed_caption_styles=["bottom_card"],
        max_caption_lines=3,
        max_caption_chars=120,
    ),
    "code_diagram": SceneTemplateSpec(
        id="code_diagram",
        name="코드 / 다이어그램 설명",
        description="소스 코드나 구조도 위에 단계별 설명을 오버레이하는 기술 장면",
        category="code",
        default_duration=8.0,
        min_duration=4.0,
        max_duration=30.0,
        default_animation="still",
        allowed_animations=["still", "pan_down", "zoom_in"],
        default_transition="cut",
        allowed_transitions=["cut", "fade"],
        default_caption_style="code_overlay",
        allowed_caption_styles=["code_overlay", "bottom_card"],
        max_caption_lines=4,
        max_caption_chars=160,
    ),
    "highlight_text": SceneTemplateSpec(
        id="highlight_text",
        name="핵심 키워드 강조",
        description="요약 포인트나 핵심 문구를 시각적으로 각인시키는 장면",
        category="hook",
        default_duration=3.0,
        min_duration=1.5,
        max_duration=6.0,
        default_animation="pulse",
        allowed_animations=["pulse", "zoom_in", "still"],
        default_transition="cut",
        allowed_transitions=["cut", "fade"],
        default_caption_style="center_highlight",
        allowed_caption_styles=["center_highlight", "bottom_card"],
        max_caption_lines=2,
        max_caption_chars=50,
    ),
    "split_screen": SceneTemplateSpec(
        id="split_screen",
        name="비교 분할 화면",
        description="두 개념이나 비포/애프터를 대조하는 장면",
        category="content",
        default_duration=5.0,
        min_duration=3.0,
        max_duration=12.0,
        default_animation="still",
        allowed_animations=["still", "zoom_in"],
        default_transition="cut",
        allowed_transitions=["cut", "fade"],
        default_caption_style="bottom_card",
        allowed_caption_styles=["bottom_card", "center_highlight"],
        max_caption_lines=2,
        max_caption_chars=80,
    ),
    "outro": SceneTemplateSpec(
        id="outro",
        name="아웃트로 / 마무리",
        description="요약과 마무리, 후속 행동(구독/방문/실행)을 유도하는 클로징 장면",
        category="ending",
        default_duration=4.0,
        min_duration=2.0,
        max_duration=10.0,
        default_animation="zoom_out",
        allowed_animations=["zoom_out", "still"],
        default_transition="fade",
        allowed_transitions=["fade", "cut"],
        default_caption_style="title_card",
        allowed_caption_styles=["title_card", "bottom_card"],
        max_caption_lines=2,
        max_caption_chars=60,
    ),
}

FORMATS: dict[str, FormatSpec] = {
    "shorts_9_16": FormatSpec(
        id="shorts_9_16",
        name="9:16 모바일 쇼츠",
        description="스마트폰 세로 화면에 최적화된 빠른 호흡의 숏폼 영상 (15~60초)",
        allowed_aspects=["9:16"],
        default_aspect="9:16",
        min_duration=10.0,
        max_duration=60.0,
        default_duration=30.0,
        min_scenes=3,
        max_scenes=15,
        has_hook=True,
        default_scene_templates=[
            "intro_hook",
            "highlight_text",
            "ken_burns",
            "highlight_text",
            "outro",
        ],
        pacing="fast",
        allowed_animations=["zoom_in", "zoom_out", "pan", "pulse", "still"],
        allowed_transitions=["cut", "fade"],
        allowed_caption_styles=["center_highlight", "bottom_card", "title_card"],
        min_scene_duration=1.0,
        max_scene_duration=15.0,
        max_caption_lines=2,
        max_caption_chars=80,
    ),
    "tech_explainer": FormatSpec(
        id="tech_explainer",
        name="기술 설명 · 교육 영상",
        description="코드 블록, 아키텍처 다이어그램, 단계별 해설을 담은 16:9 교육 영상 (30~240초)",
        allowed_aspects=["16:9"],
        default_aspect="16:9",
        min_duration=10.0,
        max_duration=240.0,
        default_duration=60.0,
        min_scenes=3,
        max_scenes=30,
        has_hook=False,
        default_scene_templates=[
            "title_card",
            "code_diagram",
            "code_diagram",
            "highlight_text",
            "outro",
        ],
        pacing="moderate",
        allowed_animations=["still", "zoom_in", "zoom_out", "pan", "pan_down"],
        allowed_transitions=["cut", "fade"],
        allowed_caption_styles=["code_overlay", "bottom_card", "title_card", "center_highlight"],
        min_scene_duration=2.0,
        max_scene_duration=30.0,
        max_caption_lines=4,
        max_caption_chars=120,
    ),
    "music_video": FormatSpec(
        id="music_video",
        name="뮤직비디오 · 감성 비주얼",
        description="배경 음악과 가사의 리듬감에 맞춘 시각 영상 (30~240초)",
        allowed_aspects=["16:9", "1:1"],
        default_aspect="16:9",
        min_duration=3.0,
        max_duration=240.0,
        default_duration=30.0,
        min_scenes=3,
        max_scenes=40,
        has_hook=False,
        default_scene_templates=[
            "title_card",
            "lyric_scene",
            "lyric_scene",
            "lyric_scene",
            "outro",
        ],
        pacing="dynamic",
        allowed_animations=["zoom_in", "zoom_out", "pan", "still", "pulse"],
        allowed_transitions=["fade", "cut"],
        allowed_caption_styles=["bottom_card", "center_highlight", "title_card"],
        min_scene_duration=1.0,
        max_scene_duration=30.0,
        max_caption_lines=3,
        max_caption_chars=80,
    ),
    "narration_story": FormatSpec(
        id="narration_story",
        name="내레이션 스토리 · 장편 다큐",
        description="차분한 내레이션과 깊이 있는 시각 전개 중심의 2~4분 장편 영상 (60~240초)",
        allowed_aspects=["16:9"],
        default_aspect="16:9",
        min_duration=30.0,
        max_duration=240.0,
        default_duration=120.0,
        min_scenes=5,
        max_scenes=40,
        has_hook=False,
        default_scene_templates=[
            "title_card",
            "ken_burns",
            "ken_burns",
            "ken_burns",
            "ken_burns",
            "ken_burns",
            "outro",
        ],
        pacing="slow_moderate",
        allowed_animations=["zoom_in", "zoom_out", "pan", "pan_down", "still"],
        allowed_transitions=["fade", "cut"],
        allowed_caption_styles=["bottom_card", "title_card"],
        min_scene_duration=2.0,
        max_scene_duration=30.0,
        max_caption_lines=3,
        max_caption_chars=120,
    ),
    "meme_parody": FormatSpec(
        id="meme_parody",
        name="밈 · 패러디",
        description="짧고 강렬한 밈 또는 패러디 클립 (5~30초)",
        allowed_aspects=["9:16", "1:1", "16:9"],
        default_aspect="9:16",
        min_duration=3.0,
        max_duration=30.0,
        default_duration=15.0,
        min_scenes=2,
        max_scenes=10,
        has_hook=True,
        default_scene_templates=["intro_hook", "highlight_text", "outro"],
        pacing="very_fast",
        allowed_animations=["zoom_in", "pulse", "still", "zoom_out", "pan"],
        allowed_transitions=["cut", "fade"],
        allowed_caption_styles=["center_highlight", "bottom_card", "title_card"],
        min_scene_duration=1.0,
        max_scene_duration=15.0,
        max_caption_lines=2,
        max_caption_chars=60,
    ),
    "image_motion": FormatSpec(
        id="image_motion",
        name="이미지 모션그래픽",
        description="생성 이미지의 디테일을 살린 서정적 모션그래픽 영상 (20~120초)",
        allowed_aspects=["16:9", "9:16"],
        default_aspect="16:9",
        min_duration=10.0,
        max_duration=120.0,
        default_duration=40.0,
        min_scenes=3,
        max_scenes=20,
        has_hook=False,
        default_scene_templates=["title_card", "ken_burns", "ken_burns", "outro"],
        pacing="moderate",
        allowed_animations=["zoom_in", "zoom_out", "pan", "pan_down", "still"],
        allowed_transitions=["fade", "cut"],
        allowed_caption_styles=["bottom_card", "title_card"],
        min_scene_duration=2.0,
        max_scene_duration=30.0,
        max_caption_lines=3,
        max_caption_chars=100,
    ),
}


def get_catalog() -> dict[str, Any]:
    return {
        "formats": [fmt.model_dump() for fmt in FORMATS.values()],
        "templates": [tmpl.model_dump() for tmpl in SCENE_TEMPLATES.values()],
        "tools": [tool.model_dump() for tool in TOOLS.values()],
    }


def validate_project_spec(project_data: dict[str, Any]) -> list[str]:
    """Validate a project and its scenes against catalog rules.

    Returns a list of error strings. An empty list indicates full compliance.
    """
    errors: list[str] = []
    fmt_id = project_data.get("content_type") or "music_video"
    fmt = FORMATS.get(fmt_id)
    if not fmt:
        errors.append(f"알 수 없는 포맷입니다: '{fmt_id}'")
        return errors

    aspect = project_data.get("aspect", "16:9")
    if aspect not in fmt.allowed_aspects:
        errors.append(
            f"포맷 '{fmt.name}'은 화면비 {fmt.allowed_aspects}만 지원합니다. (요청: {aspect})"
        )

    scenes = project_data.get("scenes") or []
    if len(scenes) < fmt.min_scenes:
        errors.append(f"장면 수가 부족합니다. (최소 {fmt.min_scenes}개, 현재 {len(scenes)}개)")
    if len(scenes) > fmt.max_scenes:
        errors.append(f"장면 수가 초과되었습니다. (최대 {fmt.max_scenes}개, 현재 {len(scenes)}개)")

    total_duration = sum(float(s.get("duration", 0)) for s in scenes)
    if total_duration < fmt.min_duration - 0.01:
        errors.append(
            f"영상 길이가 포맷 최소 기준({fmt.min_duration}초)에 미달합니다. (현재: {total_duration:.1f}초)"
        )
    if total_duration > fmt.max_duration + 0.01:
        errors.append(
            f"영상 길이가 포맷 최대 기준({fmt.max_duration}초)을 초과했습니다. (현재: {total_duration:.1f}초)"
        )

    if fmt.has_hook and scenes:
        first_tmpl_id = scenes[0].get("template_id")
        first_tmpl = SCENE_TEMPLATES.get(first_tmpl_id) if first_tmpl_id else None
        if not first_tmpl or first_tmpl.category != "hook":
            errors.append(
                f"포맷 '{fmt.name}'은 첫 장면에 반드시 훅(intro_hook 또는 highlight_text)을 배치해야 합니다."
            )

    for idx, s in enumerate(scenes):
        s_num = idx + 1
        dur = float(s.get("duration", 0))
        tmpl_id = s.get("template_id")
        if tmpl_id:
            tmpl = SCENE_TEMPLATES.get(tmpl_id)
            if not tmpl:
                errors.append(
                    f"장면 {s_num}의 템플릿 '{tmpl_id}'은(는) 등록되지 않은 템플릿입니다."
                )
        else:
            tmpl = None

        if tmpl:
            if dur < tmpl.min_duration - 0.01 or dur > tmpl.max_duration + 0.01:
                errors.append(
                    f"장면 {s_num}({tmpl.name})의 길이({dur}초)가 허용 범위({tmpl.min_duration}~{tmpl.max_duration}초)를 벗어났습니다."
                )

            anim = s.get("animation")
            if anim and anim not in tmpl.allowed_animations:
                errors.append(
                    f"장면 {s_num}({tmpl.name})에서 '{anim}' 애니메이션은 허용되지 않습니다. (허용: {tmpl.allowed_animations})"
                )

            trans = s.get("transition")
            if trans and trans not in tmpl.allowed_transitions:
                errors.append(
                    f"장면 {s_num}({tmpl.name})에서 '{trans}' 전환은 허용되지 않습니다. (허용: {tmpl.allowed_transitions})"
                )

            cap_style = s.get("caption_style")
            if cap_style and cap_style not in tmpl.allowed_caption_styles:
                errors.append(
                    f"장면 {s_num}({tmpl.name})에서 '{cap_style}' 자막 스타일은 허용되지 않습니다. (허용: {tmpl.allowed_caption_styles})"
                )

            caption = s.get("caption", "")
            if caption:
                lines = [l for l in caption.split("\n") if l.strip()]
                if len(lines) > tmpl.max_caption_lines:
                    errors.append(
                        f"장면 {s_num}({tmpl.name})의 자막 줄 수({len(lines)}줄)가 제한({tmpl.max_caption_lines}줄)을 초과했습니다."
                    )
                if len(caption) > tmpl.max_caption_chars:
                    errors.append(
                        f"장면 {s_num}({tmpl.name})의 자막 길이({len(caption)}자)가 제한({tmpl.max_caption_chars}자)을 초과했습니다."
                    )
        else:
            # Fallback checks without template (template_id is None or unregistered)
            if dur < fmt.min_scene_duration - 0.01 or dur > fmt.max_scene_duration + 0.01:
                errors.append(
                    f"장면 {s_num}의 길이({dur}초)가 포맷 허용 범위({fmt.min_scene_duration}~{fmt.max_scene_duration}초)를 벗어났습니다."
                )

            anim = s.get("animation")
            if anim and anim not in fmt.allowed_animations:
                errors.append(
                    f"장면 {s_num}에서 '{anim}' 애니메이션은 포맷 '{fmt.name}'에서 허용되지 않습니다. (허용: {fmt.allowed_animations})"
                )

            trans = s.get("transition")
            if trans and trans not in fmt.allowed_transitions:
                errors.append(
                    f"장면 {s_num}에서 '{trans}' 전환은 포맷 '{fmt.name}'에서 허용되지 않습니다. (허용: {fmt.allowed_transitions})"
                )

            cap_style = s.get("caption_style")
            if cap_style and cap_style not in fmt.allowed_caption_styles:
                errors.append(
                    f"장면 {s_num}에서 '{cap_style}' 자막 스타일은 포맷 '{fmt.name}'에서 허용되지 않습니다. (허용: {fmt.allowed_caption_styles})"
                )

            caption = s.get("caption", "")
            if caption:
                lines = [l for l in caption.split("\n") if l.strip()]
                if len(lines) > fmt.max_caption_lines:
                    errors.append(
                        f"장면 {s_num}의 자막 줄 수({len(lines)}줄)가 포맷 제한({fmt.max_caption_lines}줄)을 초과했습니다."
                    )
                if len(caption) > fmt.max_caption_chars:
                    errors.append(
                        f"장면 {s_num}의 자막 길이({len(caption)}자)가 포맷 제한({fmt.max_caption_chars}자)을 초과했습니다."
                    )

    return errors
