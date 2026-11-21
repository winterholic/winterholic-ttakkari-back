"""동적 노출 규칙: 조건 조합 → 활성 설정 묶음·질문·숨김 항목·상호 충돌. 계약: docs/design/creative-studio-v2-intent-v2.md §7

`resolve_layout`은 순수 함수다. 입력이 같으면 출력이 같고 저장소·LLM을 부르지 않는다.
규칙은 조건 하나가 아니라 조합에 반응할 수 있다(all_of). 여러 규칙이 같은 묶음·같은 설정을 요구하면
묶음은 하나로 합치고 질문은 설정 하나당 하나만 낸다. 추천값은 우선순위가 높은(숫자가 작은) 규칙이 정한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ttakkari.creative.v2.capabilities import Strict
from ttakkari.creative.v2.catalog_data import ENTRIES
from ttakkari.creative.v2.intent_v2 import (
    TONES,
    Answer,
    Condition,
    ConflictCandidate,
    IntentConflict,
    label_of,
)

FACET_RULES_VERSION = "1.0.0"
Tier = Literal["essential", "advanced"]


class Option(Strict):
    id: str = Field(max_length=80)
    label: str = Field(max_length=80)


class FacetField(Strict):
    id: str = Field(max_length=80)
    group: str = Field(max_length=40)
    dimension: str = Field(max_length=20)
    label: str = Field(max_length=80)
    question: str = Field(max_length=200)
    kind: Literal["single", "multi", "number", "text"] = "single"
    options: list[Option] = Field(default_factory=list)
    tier: Tier = "essential"


class FacetGroup(Strict):
    id: str = Field(max_length=40)
    title: str = Field(max_length=60)
    description: str = Field(max_length=200)
    tier: Tier = "essential"


class Recommend(Strict):
    field: str
    value: str
    reason: str


class RuleConflict(Strict):
    with_: str = Field(alias="with")
    message: str
    blocking: bool = False


class When(Strict):
    """조건 id 집합에 대한 술어. 조건 id 대신 `C01.*`처럼 차원 전체를 쓸 수 있다."""

    all_of: list[str] = Field(default_factory=list)
    any_of: list[str] = Field(default_factory=list)
    none_of: list[str] = Field(default_factory=list)


class FacetRule(Strict):
    id: str
    priority: int = Field(ge=0, le=100)
    when: When
    activate: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    recommend: list[Recommend] = Field(default_factory=list)
    hide: list[str] = Field(default_factory=list)
    conflicts: list[RuleConflict] = Field(default_factory=list)
    reason: str


def _o(*pairs: tuple[str, str]) -> list[Option]:
    return [Option(id=i, label=label) for i, label in pairs]


def _c(*ids: str) -> list[Option]:
    """카탈로그·톤 항목을 선택지로 쓴다. 고르면 그 항목이 사용자 확정 조건이 된다."""
    return [Option(id=i, label=label_of(i)) for i in ids]


GROUPS: dict[str, FacetGroup] = {
    g.id: g
    for g in (
        FacetGroup(
            id="content", title="콘텐츠 유형", description="무엇을 만들지 정합니다."
        ),
        FacetGroup(id="output", title="길이·비율", description="길이 정책과 화면 비율"),
        FacetGroup(
            id="character",
            title="캐릭터·연기",
            description="캐릭터 디자인, 표정, 립싱크",
        ),
        FacetGroup(id="drawing", title="작화", description="선, 음영, 작화 프레임"),
        FacetGroup(
            id="cut_direction",
            title="컷 연출",
            description="컷 리듬, 샷 구성, 임팩트 프레임",
        ),
        FacetGroup(
            id="world", title="세계관·환경", description="시대, 기술 수준, 장소"
        ),
        FacetGroup(
            id="space_camera",
            title="3D 공간·촬영",
            description="카메라 이동과 공간 연속성",
        ),
        FacetGroup(
            id="lighting_vfx", title="조명·VFX", description="조명 분위기와 시각 효과"
        ),
        FacetGroup(
            id="horror", title="공포 연출", description="공포의 종류, 노출 정도, 수위"
        ),
        FacetGroup(id="action", title="액션 연출", description="동작 종류와 속도 연출"),
        FacetGroup(
            id="explain", title="설명 구조", description="대상 수준과 설명 순서"
        ),
        FacetGroup(
            id="diagram",
            title="다이어그램·데이터 시각화",
            description="흐름도, 구조도, 차트",
        ),
        FacetGroup(
            id="code",
            title="코드 표시",
            description="코드 조각을 화면에 보일지",
            tier="advanced",
        ),
        FacetGroup(
            id="motion_graphics",
            title="모션그래픽",
            description="도형·타이포 움직임과 배경",
        ),
        FacetGroup(
            id="narration", title="내레이션·자막", description="해설 음성과 자막"
        ),
        FacetGroup(id="music", title="음악·비트", description="음악 출처와 구성"),
        FacetGroup(id="lyrics", title="가사", description="가사 유무와 표시 방식"),
        FacetGroup(
            id="av_sync",
            title="장면 리듬·AV 동기화",
            description="박자에 맞춘 컷과 전환, 움직임 원천",
        ),
        FacetGroup(
            id="panel", title="컷 구성·말풍선", description="웹툰·모션코믹 칸과 말풍선"
        ),
        FacetGroup(
            id="consistency",
            title="일관성 목표",
            description="캐릭터·스타일 일관성 기준",
            tier="advanced",
        ),
    )
}

_FIELDS = (
    FacetField(
        id="content.type",
        group="content",
        dimension="C01",
        label="콘텐츠 유형",
        question="어떤 콘텐츠를 만들까요?",
        options=_c(
            "C01.story_film",
            "C01.animation",
            "C01.tech_explainer",
            "C01.music_video",
            "C01.motion_comic",
            "C01.advertisement",
            "C01.documentary",
            "C01.shorts",
        ),
    ),
    FacetField(
        id="output.duration",
        group="output",
        dimension="C11",
        label="길이",
        question="길이를 어떻게 정할까요?",
        options=_o(
            ("auto", "내용에 맞춰 자동"),
            ("target", "희망 길이에 가깝게"),
            ("strict", "정확히 지키기"),
        ),
    ),
    FacetField(
        id="output.aspect",
        group="output",
        dimension="C11",
        label="화면 비율",
        question="화면 비율은 무엇으로 할까요?",
        options=_o(("16:9", "가로 16:9"), ("9:16", "세로 9:16"), ("1:1", "정사각 1:1")),
    ),
    FacetField(
        id="character.design",
        group="character",
        dimension="C04",
        label="주요 캐릭터",
        question="주요 캐릭터는 어떤 존재인가요?",
        kind="multi",
        options=_c(
            "C04.human",
            "C04.robot",
            "C04.monster",
            "C04.creature",
            "C04.anthropomorphic",
        ),
    ),
    FacetField(
        id="character.expression",
        group="character",
        dimension="C07",
        label="표정 연기",
        question="표정 연기는 어느 정도로 할까요?",
        options=_o(("subtle", "절제된 표정"), ("exaggerated", "과장된 표정")),
    ),
    FacetField(
        id="character.lipsync",
        group="character",
        dimension="C07",
        label="립싱크",
        question="대사에 입 모양을 맞출까요?",
        tier="advanced",
        options=_o(
            ("none", "맞추지 않음"),
            ("approx", "대략 맞춤"),
            ("precise", "정밀하게 맞춤"),
        ),
    ),
    FacetField(
        id="drawing.shading",
        group="drawing",
        dimension="C03",
        label="음영",
        question="음영은 어떤 방식으로 할까요?",
        options=_o(
            ("cel", "셀 음영"), ("soft", "부드러운 음영"), ("painterly", "회화적 음영")
        ),
    ),
    FacetField(
        id="drawing.line",
        group="drawing",
        dimension="C03",
        label="선",
        question="선 굵기는 어떻게 할까요?",
        tier="advanced",
        options=_o(("thin", "가는 선"), ("bold", "굵은 선"), ("sketchy", "스케치 선")),
    ),
    FacetField(
        id="drawing.frame_rate",
        group="drawing",
        dimension="C07",
        label="작화 프레임",
        question="작화 매수는 어떻게 할까요?",
        tier="advanced",
        options=_o(
            ("on_ones", "1프레임마다"),
            ("on_twos", "2프레임마다"),
            ("on_threes", "3프레임마다"),
        ),
    ),
    FacetField(
        id="cut.tempo",
        group="cut_direction",
        dimension="C10",
        label="전체 템포",
        question="전체 템포는 어떻게 가져갈까요?",
        options=_o(
            ("slow", "느리게 쌓기"),
            ("standard", "보통"),
            ("fast", "빠르게"),
            ("slow_build_burst", "느리게 쌓다가 폭발"),
        ),
    ),
    FacetField(
        id="cut.impact_frames",
        group="cut_direction",
        dimension="C10",
        label="임팩트 프레임",
        question="타격·충격 순간에 임팩트 프레임을 넣을까요?",
        options=_o(("yes", "넣음"), ("no", "넣지 않음")),
    ),
    FacetField(
        id="cut.shots",
        group="cut_direction",
        dimension="C06",
        label="주요 샷",
        question="자주 쓸 샷을 골라 주세요.",
        kind="multi",
        tier="advanced",
        options=_c(
            "C06.closeup", "C06.wide", "C06.pov", "C06.tracking", "C06.handheld"
        ),
    ),
    FacetField(
        id="world.locations",
        group="world",
        dimension="C05",
        label="주요 장소",
        question="주요 장소는 어디인가요?",
        kind="multi",
        options=_c(
            "C05.space", "C05.city", "C05.indoor", "C05.forest", "C05.desert", "C05.sea"
        ),
    ),
    FacetField(
        id="world.tech_design",
        group="world",
        dimension="C02",
        label="기술 디자인",
        question="기계·기술의 디자인 방향은 무엇인가요?",
        options=_o(
            ("mecha", "메카닉"), ("organic", "유기적·생체"), ("retro", "레트로 퓨처")
        ),
    ),
    FacetField(
        id="camera.moves",
        group="space_camera",
        dimension="C06",
        label="카메라 이동",
        question="어떤 카메라 이동을 쓸까요?",
        kind="multi",
        options=_c(
            "C06.dolly",
            "C06.orbit",
            "C06.crane",
            "C06.tracking",
            "C06.aerial",
            "C06.handheld",
        ),
    ),
    FacetField(
        id="light.key",
        group="lighting_vfx",
        dimension="C05",
        label="조명 분위기",
        question="조명 분위기는 어떻게 할까요?",
        options=_c(
            "C05.night",
            "C05.neon",
            "C05.fog",
            "C05.volumetric_light",
            "C05.backlight",
            "C05.dusk",
            "C05.natural_light",
        ),
    ),
    FacetField(
        id="vfx.types",
        group="lighting_vfx",
        dimension="C07",
        label="시각 효과",
        question="필요한 시각 효과를 골라 주세요.",
        kind="multi",
        tier="advanced",
        options=_c(
            "C07.fire", "C07.smoke", "C07.destruction", "C07.magic", "C07.water"
        ),
    ),
    FacetField(
        id="horror.style",
        group="horror",
        dimension="tone",
        label="공포의 종류",
        question="어떤 공포를 원하나요?",
        options=_o(
            ("dread", "서서히 조이는 긴장"),
            ("jump_scare", "갑작스러운 놀람"),
            ("psychological", "심리적 공포"),
            ("body_horror", "신체 변형"),
        ),
    ),
    FacetField(
        id="horror.reveal",
        group="horror",
        dimension="tone",
        label="위협 노출",
        question="위협의 정체를 얼마나 보여 줄까요?",
        tier="advanced",
        options=_o(
            ("hidden", "끝까지 숨김"), ("partial", "일부만"), ("full", "드러냄")
        ),
    ),
    FacetField(
        id="horror.gore",
        group="horror",
        dimension="tone",
        label="수위",
        question="잔혹 표현 수위는 어느 정도인가요?",
        options=_o(("none", "없음"), ("mild", "약함"), ("strong", "강함")),
    ),
    FacetField(
        id="action.kind",
        group="action",
        dimension="C07",
        label="액션 종류",
        question="어떤 액션이 들어가나요?",
        kind="multi",
        options=_c(
            "C07.battle",
            "C07.run",
            "C07.vehicle_move",
            "C07.destruction",
            "C07.collision",
        ),
    ),
    FacetField(
        id="action.speed_ramp",
        group="action",
        dimension="C10",
        label="속도 연출",
        question="결정적 순간에 슬로모션·스피드 램프를 쓸까요?",
        tier="advanced",
        options=_o(("yes", "씀"), ("no", "쓰지 않음")),
    ),
    FacetField(
        id="explain.audience",
        group="explain",
        dimension="C08",
        label="대상 수준",
        question="누구를 위한 설명인가요?",
        options=_o(
            ("beginner", "입문자"), ("intermediate", "실무자"), ("expert", "전문가")
        ),
    ),
    FacetField(
        id="explain.structure",
        group="explain",
        dimension="C08",
        label="설명 구조",
        question="설명은 어떤 순서로 풀까요?",
        options=_c("C08.explainer", "C08.problem_solution", "C08.tutorial"),
    ),
    FacetField(
        id="diagram.type",
        group="diagram",
        dimension="C03",
        label="다이어그램 종류",
        question="어떤 그림으로 보여 줄까요?",
        options=_o(
            ("flow", "데이터 흐름도"),
            ("sequence", "시퀀스 다이어그램"),
            ("architecture", "구성도"),
            ("chart", "차트"),
        ),
    ),
    FacetField(
        id="diagram.motion",
        group="diagram",
        dimension="C07",
        label="다이어그램 움직임",
        question="다이어그램은 어떻게 움직일까요?",
        options=_o(
            ("step_reveal", "단계별로 나타남"),
            ("continuous_flow", "메시지가 흐르는 애니메이션"),
            ("static", "정지"),
        ),
    ),
    FacetField(
        id="code.show",
        group="code",
        dimension="C10",
        label="코드 표시",
        question="코드를 화면에 보일까요?",
        tier="advanced",
        options=_o(("none", "보이지 않음"), ("snippet", "짧은 조각"), ("full", "전체")),
    ),
    FacetField(
        id="mg.style",
        group="motion_graphics",
        dimension="C03",
        label="그래픽 스타일",
        question="그래픽 스타일은 무엇으로 할까요?",
        options=_c("C03.vector_motion_graphics", "C03.infographic", "C03.ui_animation"),
    ),
    FacetField(
        id="mg.background",
        group="motion_graphics",
        dimension="C05",
        label="배경",
        question="배경은 어떻게 할까요?",
        tier="advanced",
        options=_o(
            ("solid", "단색·그리드"),
            ("brand", "브랜드 배경"),
            ("generated_image", "생성 이미지"),
        ),
    ),
    FacetField(
        id="voice.narration",
        group="narration",
        dimension="C09",
        label="내레이션",
        question="해설 음성을 넣을까요?",
        options=_o(("ko", "한국어 내레이션"), ("none", "음성 없이 자막만")),
    ),
    FacetField(
        id="voice.subtitles",
        group="narration",
        dimension="C10",
        label="자막",
        question="자막을 넣을까요?",
        tier="advanced",
        options=_o(("on", "넣음"), ("off", "넣지 않음")),
    ),
    FacetField(
        id="music.source",
        group="music",
        dimension="C09",
        label="음악 출처",
        question="음악은 어디서 가져올까요?",
        options=_o(("upload", "내 음악 업로드"), ("generate", "AI로 생성")),
    ),
    FacetField(
        id="music.bpm",
        group="music",
        dimension="C09",
        label="BPM",
        kind="number",
        question="음악 BPM을 알고 있나요?",
        tier="advanced",
    ),
    FacetField(
        id="lyrics.has",
        group="lyrics",
        dimension="C09",
        label="가사",
        question="가사가 있는 곡인가요?",
        options=_o(("lyrics", "가사 있음"), ("instrumental", "연주곡")),
    ),
    FacetField(
        id="lyrics.display",
        group="lyrics",
        dimension="C10",
        label="가사 표시",
        question="가사를 화면에 어떻게 보일까요?",
        tier="advanced",
        options=_c("C10.subtitles", "C10.kinetic_typography"),
    ),
    FacetField(
        id="sync.cut_on",
        group="av_sync",
        dimension="C10",
        label="컷 기준",
        question="장면 전환을 음악의 어디에 맞출까요?",
        options=_o(("beat", "박자"), ("bar", "마디"), ("section", "곡 구간(절·후렴)")),
    ),
    FacetField(
        id="sync.transition",
        group="av_sync",
        dimension="C10",
        label="전환 방식",
        question="주로 쓸 전환을 골라 주세요.",
        kind="multi",
        options=_c("C10.cut", "C10.dissolve", "C10.match_cut", "C10.speed_ramp"),
    ),
    FacetField(
        id="sync.motion_source",
        group="av_sync",
        dimension="C07",
        label="움직임 원천",
        question="장면의 움직임은 무엇으로 만들까요?",
        options=_o(
            ("generated_video", "영상 생성 모델의 실제 움직임"),
            ("animated_3d", "3D 애니메이션"),
            ("live_footage", "내 영상 소스"),
        ),
    ),
    FacetField(
        id="panel.bubbles",
        group="panel",
        dimension="C10",
        label="말풍선",
        question="대사를 말풍선으로 보일까요?",
        options=_o(("yes", "말풍선"), ("caption", "자막으로"), ("none", "대사 없음")),
    ),
    FacetField(
        id="panel.motion",
        group="panel",
        dimension="C06",
        label="칸 움직임",
        question="칸 안의 움직임은 어떻게 할까요?",
        options=_o(("parallax", "레이어 패럴랙스"), ("limited", "부분 애니메이션")),
    ),
    FacetField(
        id="consistency.character",
        group="consistency",
        dimension="C12",
        label="캐릭터 일관성",
        question="장면마다 캐릭터 외형을 엄격히 고정할까요?",
        tier="advanced",
        options=_o(("strict", "엄격히 고정"), ("loose", "느슨하게")),
    ),
)
FIELDS: dict[str, FacetField] = {f.id: f for f in _FIELDS}

_SF = ["C02.space_sf", "C02.cyberpunk", "C02.near_future", "C02.post_apocalypse"]
_DRAWN = [
    "C03.japanese_anime",
    "C03.korean_webtoon",
    "C03.comic_2d",
    "C03.western_cartoon",
]
_EXPLAIN = [
    "C01.tech_explainer",
    "C01.science_edu",
    "C01.presentation",
    "C08.explainer",
    "C08.tutorial",
]
_MUSIC = ["C01.music_video", "C08.music_video_form", "C09.music_beat"]
_ACTION = ["tone.action", "C07.battle"]


def _r(
    rid,
    priority,
    reason,
    *,
    all_of=(),
    any_of=(),
    none_of=(),
    activate=(),
    ask=(),
    rec=(),
    hide=(),
    conf=(),
):
    return FacetRule(
        id=rid,
        priority=priority,
        reason=reason,
        when=When(all_of=list(all_of), any_of=list(any_of), none_of=list(none_of)),
        activate=list(activate),
        ask=list(ask),
        recommend=[Recommend(field=f, value=v, reason=why) for f, v, why in rec],
        hide=list(hide),
        conflicts=[
            RuleConflict.model_validate({"with": w, "message": m, "blocking": b})
            for w, m, b in conf
        ],
    )


# 우선순위: 조합 규칙 10, 단일 조건 규칙 50, 기본 규칙 90. 숫자가 작을수록 추천값을 정할 권한이 크다.
RULES: tuple[FacetRule, ...] = (
    _r(
        "base.output",
        90,
        "모든 콘텐츠는 길이와 화면 비율이 필요합니다.",
        activate=["output"],
        ask=["output.duration", "output.aspect"],
    ),
    _r(
        "base.content_type",
        90,
        "콘텐츠 유형이 정해지지 않으면 필요한 설정을 고를 수 없습니다.",
        none_of=["C01.*"],
        activate=["content"],
        ask=["content.type"],
    ),
    _r(
        "drawn_style",
        50,
        "그린 그림체는 캐릭터·작화·컷 연출이 품질을 좌우합니다.",
        any_of=_DRAWN,
        activate=["character", "drawing", "cut_direction", "consistency"],
        ask=["character.design", "character.expression", "drawing.shading"],
        rec=[("drawing.shading", "cel", "일본 애니·웹툰 그림체의 기본 음영입니다.")],
    ),
    _r(
        "anime_expression",
        40,
        "일본 애니는 표정 과장이 장르 문법입니다.",
        all_of=["C03.japanese_anime"],
        rec=[
            (
                "character.expression",
                "exaggerated",
                "일본 애니메이션은 감정 표현을 과장하는 문법을 씁니다.",
            ),
            (
                "drawing.frame_rate",
                "on_twos",
                "일본 TV 애니의 일반적인 작화 매수입니다.",
            ),
        ],
    ),
    _r(
        "animation_type",
        50,
        "애니메이션은 캐릭터 시트와 일관성이 필요합니다.",
        any_of=["C01.animation"],
        activate=["character", "consistency"],
        ask=["character.design"],
        rec=[
            (
                "consistency.character",
                "strict",
                "애니메이션은 장면 간 외형 차이가 바로 결함으로 보입니다.",
            )
        ],
    ),
    _r(
        "story_film",
        50,
        "스토리 영화는 세계관·촬영·컷 연출이 필요합니다.",
        any_of=["C01.story_film"],
        activate=["world", "space_camera", "cut_direction", "character"],
        ask=["character.design", "camera.moves"],
    ),
    _r(
        "sf_world",
        50,
        "SF는 세계관·환경·3D 공간·조명·VFX를 정해야 합니다.",
        any_of=_SF,
        activate=["world", "space_camera", "lighting_vfx"],
        ask=["world.locations", "world.tech_design", "light.key", "camera.moves"],
        rec=[
            (
                "light.key",
                "C05.volumetric_light",
                "SF 공간감은 볼류메트릭 조명이 살립니다.",
            ),
            ("world.locations", "C05.space", "우주 SF의 기본 무대입니다."),
        ],
    ),
    _r(
        "horror_tone",
        50,
        "공포는 노출 정도·수위·조명이 분위기를 정합니다.",
        any_of=["tone.horror"],
        activate=["horror", "lighting_vfx", "cut_direction"],
        ask=["horror.style", "horror.gore", "light.key", "cut.tempo"],
        rec=[
            ("light.key", "C05.fog", "안개와 저조도는 보이지 않는 위협을 만듭니다."),
            ("cut.tempo", "slow", "공포는 긴장을 천천히 쌓을 때 효과가 큽니다."),
            ("horror.gore", "mild", "수위를 정하지 않으면 약하게 시작합니다."),
        ],
    ),
    _r(
        "action_tone",
        50,
        "액션은 동작 종류와 컷 리듬을 정해야 합니다.",
        any_of=_ACTION,
        activate=["action", "cut_direction"],
        ask=["action.kind", "cut.tempo"],
        rec=[
            ("cut.tempo", "fast", "액션은 짧은 컷과 빠른 리듬이 기본입니다."),
            ("action.kind", "C07.battle", "액션 장면의 가장 흔한 형태입니다."),
        ],
    ),
    _r(
        "horror_action_combo",
        10,
        "공포와 액션이 함께 있으면 템포를 하나로 설계해야 합니다.",
        all_of=["tone.horror"],
        any_of=_ACTION,
        rec=[
            (
                "cut.tempo",
                "slow_build_burst",
                "공포의 긴장 축적과 액션의 폭발을 한 리듬으로 묶습니다. 느린 템포와 빠른 템포를 따로 묻지 않습니다.",
            )
        ],
    ),
    _r(
        "anime_sf_combo",
        10,
        "일본 애니 SF는 메카닉 디자인 문법이 강합니다.",
        all_of=["C03.japanese_anime"],
        any_of=_SF,
        rec=[
            (
                "world.tech_design",
                "mecha",
                "일본 애니 SF에서 가장 많이 쓰는 기계 디자인입니다.",
            ),
            ("light.key", "C05.neon", "애니 SF의 셀 음영과 네온 대비가 잘 맞습니다."),
        ],
    ),
    _r(
        "anime_action_combo",
        10,
        "일본 애니 액션은 임팩트 프레임으로 타격감을 만듭니다.",
        all_of=["C03.japanese_anime"],
        any_of=_ACTION,
        ask=["cut.impact_frames"],
        rec=[
            ("cut.impact_frames", "yes", "일본 애니 액션의 대표적인 타격 표현입니다.")
        ],
    ),
    _r(
        "explainer",
        50,
        "설명 콘텐츠는 설명 구조·다이어그램·모션그래픽이 핵심입니다.",
        any_of=_EXPLAIN,
        activate=["explain", "diagram", "motion_graphics", "narration", "code"],
        ask=[
            "explain.audience",
            "diagram.type",
            "diagram.motion",
            "mg.style",
            "voice.narration",
        ],
        rec=[
            (
                "diagram.type",
                "flow",
                "구성 요소 사이의 흐름을 보여 주는 가장 직접적인 방법입니다.",
            ),
            (
                "diagram.motion",
                "continuous_flow",
                "메시지가 실제로 움직이는 모습이 정지 그림보다 이해가 빠릅니다.",
            ),
            (
                "mg.style",
                "C03.vector_motion_graphics",
                "도형과 글자가 선명해 깨진 글자 위험이 낮습니다.",
            ),
            (
                "mg.background",
                "solid",
                "내용과 무관한 생성 배경(로봇·도시)이 반복된 과거 결함을 피합니다.",
            ),
            (
                "voice.narration",
                "ko",
                "짧은 설명은 음성과 자막을 함께 쓸 때 전달이 좋습니다.",
            ),
        ],
        hide=[
            "character.lipsync",
            "character.expression",
            "drawing.frame_rate",
            "sync.cut_on",
        ],
        conf=[
            (
                "tone.horror",
                "설명 콘텐츠에 공포 톤이 함께 있습니다. 의도한 조합인지 확인해 주세요.",
                False,
            )
        ],
    ),
    _r(
        "explainer_beginner",
        40,
        "입문자 대상이라는 단서가 있으면 단계별 공개가 낫습니다.",
        all_of=["C01.tech_explainer"],
        ask=[],
        rec=[
            (
                "explain.audience",
                "beginner",
                "설명 콘텐츠는 대상이 정해지지 않으면 입문자를 기준으로 잡습니다.",
            )
        ],
    ),
    _r(
        "music_video",
        50,
        "뮤직비디오는 음악 구성이 장면 길이와 전환을 정합니다.",
        any_of=_MUSIC,
        activate=["music", "lyrics", "av_sync"],
        ask=[
            "music.source",
            "lyrics.has",
            "sync.cut_on",
            "sync.motion_source",
            "sync.transition",
        ],
        rec=[
            (
                "sync.cut_on",
                "section",
                "절·후렴 구간에 장면을 맞추고 박자는 컷 안에서 맞춥니다.",
            ),
            (
                "sync.motion_source",
                "generated_video",
                "정지 이미지 줌·팬만 쓴 과거 결함을 피하려면 실제 움직임이 있는 소스가 필요합니다.",
            ),
        ],
        hide=["voice.narration", "explain.audience", "diagram.type"],
    ),
    _r(
        "emotional_tone",
        50,
        "감성 톤은 조명과 템포로 표현합니다.",
        any_of=["tone.emotional"],
        activate=["lighting_vfx", "cut_direction"],
        ask=["light.key", "cut.tempo"],
        rec=[
            (
                "light.key",
                "C05.dusk",
                "황혼의 따뜻한 역광이 감성 톤의 대표 표현입니다.",
            ),
            ("cut.tempo", "slow", "감성 톤은 긴 컷이 어울립니다."),
        ],
        hide=["vfx.types"],
    ),
    _r(
        "emotional_music_combo",
        10,
        "감성 뮤직비디오는 디졸브와 매치컷이 흐름을 이어 줍니다.",
        all_of=["tone.emotional"],
        any_of=_MUSIC,
        rec=[
            (
                "sync.transition",
                "C10.dissolve",
                "감성적인 곡의 흐름을 끊지 않는 전환입니다.",
            )
        ],
    ),
    _r(
        "panel_comic",
        50,
        "웹툰·모션코믹은 칸 구성과 말풍선이 필요합니다.",
        any_of=["C01.motion_comic", "C03.korean_webtoon"],
        activate=["panel", "character", "consistency"],
        ask=["panel.bubbles", "panel.motion"],
    ),
)


def _matches(token: str, ids: set[str]) -> bool:
    if token.endswith(".*"):
        prefix = token[:-1]
        return any(i.startswith(prefix) for i in ids)
    return token in ids


def _rule_applies(rule: FacetRule, ids: set[str]) -> bool:
    w = rule.when
    if w.all_of and not all(_matches(t, ids) for t in w.all_of):
        return False
    if w.any_of and not any(_matches(t, ids) for t in w.any_of):
        return False
    return not any(_matches(t, ids) for t in w.none_of)


def _triggers(rule: FacetRule, ids: set[str]) -> list[str]:
    out: list[str] = []
    for token in (*rule.when.all_of, *rule.when.any_of):
        if token.endswith(".*"):
            out += [i for i in ids if i.startswith(token[:-1])]
        elif token in ids:
            out.append(token)
    return out


# --- 출력 계약 -------------------------------------------------------------------


class ActiveGroup(Strict):
    id: str
    title: str
    tier: Tier
    triggered_by: list[str]
    rules: list[str]
    reasons: list[str]
    fields: list[str]


class Alternative(Strict):
    value: str
    rule: str
    reason: str


class Question(Strict):
    id: str
    field_id: str
    group: str
    dimension: str
    question: str
    kind: str
    options: list[Option]
    recommended: str | None
    reason: str
    triggered_by: list[str]
    rules: list[str]
    # 사용자가 아직 확인하지 않은 값이 이미 있으면 "이 값이 맞나요" 질문이 된다.
    mode: Literal["choose", "confirm"] = "choose"
    alternatives: list[Alternative] = Field(default_factory=list)
    tier: Tier = "essential"


class HiddenItem(Strict):
    field_id: str
    rules: list[str]
    reason: str


class AnsweredItem(Strict):
    field_id: str
    values: list[str]
    origin: str


class Layout(Strict):
    rules_version: str = FACET_RULES_VERSION
    groups: list[ActiveGroup]
    available_groups: list[str]
    questions: list[Question]
    answered: list[AnsweredItem]
    hidden: list[HiddenItem]
    conflicts: list[IntentConflict]
    applied_rules: list[str]

    @model_validator(mode="after")
    def _no_duplicate_questions(self):
        fields = [q.field_id for q in self.questions]
        if len(set(fields)) != len(fields):
            raise ValueError("같은 설정에 질문이 두 번 생겼습니다.")
        return self


def resolve_layout(
    conditions: list[Condition],
    answers: list[Answer] = (),
    known: dict[str, tuple[str, str]] | None = None,
) -> Layout:
    """활성 조건과 답변으로 화면 구성을 계산한다. 순수 함수.

    known은 조건이 아닌 설정에 이미 정해진 값이다(예: output.duration → ("target", "user_free_text")).
    user_confirmed면 답한 것으로, 아니면 확인 질문으로 바뀐다.
    """
    known = known or {}
    ids = {c.id for c in conditions}
    by_id = {c.id: c for c in conditions}
    applied = sorted(
        (r for r in RULES if _rule_applies(r, ids)), key=lambda r: (r.priority, r.id)
    )

    groups: dict[str, dict] = {}
    asks: dict[str, dict] = {}
    recs: dict[str, list[tuple[FacetRule, Recommend]]] = {}
    hides: dict[str, list[FacetRule]] = {}
    for rule in applied:
        trig = _triggers(rule, ids)
        for gid in rule.activate:
            g = groups.setdefault(
                gid,
                {
                    "rules": [],
                    "triggers": set(),
                    "reasons": [],
                    "priority": rule.priority,
                },
            )
            g["rules"].append(rule.id)
            g["triggers"].update(trig)
            g["reasons"].append(rule.reason)
            g["priority"] = min(g["priority"], rule.priority)
        for fid in rule.ask:
            a = asks.setdefault(fid, {"rules": [], "triggers": set()})
            a["rules"].append(rule.id)
            a["triggers"].update(trig)
        for rec in rule.recommend:
            recs.setdefault(rec.field, []).append((rule, rec))
        for fid in rule.hide:
            hides.setdefault(fid, []).append(rule)

    # 묻는 설정의 묶음은 그 규칙이 직접 켜지 않았어도 함께 켠다(질문만 있고 묶음이 없는 상태 방지).
    for fid, a in asks.items():
        gid = FIELDS[fid].group
        g = groups.setdefault(
            gid, {"rules": [], "triggers": set(), "reasons": [], "priority": 100}
        )
        g["rules"] += [r for r in a["rules"] if r not in g["rules"]]
        g["triggers"].update(a["triggers"])

    # 질문이 숨김보다 우선한다. 어떤 규칙이 묻는 설정을 다른 규칙이 숨기지 못한다.
    hidden = [
        HiddenItem(
            field_id=fid,
            rules=[r.id for r in rules],
            reason=" / ".join(dict.fromkeys(r.reason for r in rules)),
        )
        for fid, rules in sorted(hides.items())
        if fid not in asks
    ]
    hidden_ids = {h.field_id for h in hidden}

    answered_map: dict[str, AnsweredItem] = {}
    for ans in answers:
        answered_map[ans.field_id] = AnsweredItem(
            field_id=ans.field_id, values=ans.values, origin="user_confirmed"
        )
    for fid, f in FIELDS.items():
        if fid in answered_map:
            continue
        confirmed = [
            o.id
            for o in f.options
            if o.id in by_id and by_id[o.id].origin == "user_confirmed"
        ]
        if confirmed:
            answered_map[fid] = AnsweredItem(
                field_id=fid, values=confirmed, origin="user_confirmed"
            )
        elif fid in known and known[fid][1] == "user_confirmed":
            answered_map[fid] = AnsweredItem(
                field_id=fid, values=[known[fid][0]], origin="user_confirmed"
            )

    questions: list[Question] = []
    for fid in sorted(
        asks, key=lambda f: (groups[FIELDS[f].group]["priority"], FIELDS[f].group, f)
    ):
        if fid in answered_map:
            continue
        f = FIELDS[fid]
        ranked = sorted(recs.get(fid, []), key=lambda rr: (rr[0].priority, rr[0].id))
        pending = [o.id for o in f.options if o.id in by_id]
        origins = sorted({by_id[p].origin for p in pending})
        if not pending and fid in known and known[fid][1] != "default":
            pending, origins = [known[fid][0]], [known[fid][1]]
        mode: Literal["choose", "confirm"] = "choose"
        if pending:
            # 브리프·AI가 이미 값을 냈다면 추천 대신 그 값을 확인받는다. 규칙 추천은 대안으로 남긴다.
            recommended = pending[0]
            reason = (
                f"입력에서 읽은 값입니다({', '.join(origins)}). 맞는지 확인해 주세요."
            )
            mode = "confirm"
            alternatives = [
                Alternative(value=rec.value, rule=rule.id, reason=rec.reason)
                for rule, rec in ranked
                if rec.value != recommended
            ]
        elif ranked:
            recommended = ranked[0][1].value
            reason = ranked[0][1].reason
            alternatives = [
                Alternative(value=rec.value, rule=rule.id, reason=rec.reason)
                for rule, rec in ranked[1:]
                if rec.value != recommended
            ]
        else:
            recommended, reason, alternatives = (
                None,
                "추천 근거가 없습니다. 고르지 않으면 AI가 장면에 맞게 제안합니다.",
                [],
            )
        questions.append(
            Question(
                id=f"q.{fid}",
                field_id=fid,
                group=f.group,
                dimension=f.dimension,
                question=f.question,
                kind=f.kind,
                options=f.options,
                recommended=recommended,
                reason=reason,
                triggered_by=sorted(asks[fid]["triggers"]),
                rules=asks[fid]["rules"],
                mode=mode,
                alternatives=list({a.value: a for a in alternatives}.values()),
                tier=f.tier,
            )
        )

    active = [
        ActiveGroup(
            id=gid,
            title=GROUPS[gid].title,
            tier=GROUPS[gid].tier,
            triggered_by=sorted(g["triggers"]),
            rules=g["rules"],
            reasons=list(dict.fromkeys(g["reasons"])),
            fields=[
                fid
                for fid, f in FIELDS.items()
                if f.group == gid and fid not in hidden_ids
            ],
        )
        for gid, g in sorted(groups.items(), key=lambda kv: (kv[1]["priority"], kv[0]))
    ]

    conflicts: list[IntentConflict] = []
    for rule in applied:
        for rc in rule.conflicts:
            if rc.with_ not in ids:
                continue
            trig = sorted(set(_triggers(rule, ids)))
            pair = [*trig, rc.with_]
            conflicts.append(
                IntentConflict(
                    id=f"conflict.rule.{rule.id}~{rc.with_}",
                    kind="rule",
                    dimension=by_id[rc.with_].dimension,
                    message=rc.message,
                    candidates=[
                        ConflictCandidate(
                            value=i,
                            label=label_of(i),
                            origin=by_id[i].origin,
                            strength=by_id[i].strength,
                        )
                        for i in dict.fromkeys(pair)
                        if i in by_id
                    ],
                    blocking=rc.blocking,
                )
            )

    return Layout(
        groups=active,
        available_groups=[gid for gid in GROUPS if gid not in groups],
        questions=questions,
        answered=sorted(answered_map.values(), key=lambda a: a.field_id),
        hidden=hidden,
        conflicts=conflicts,
        applied_rules=[r.id for r in applied],
    )


def facet_contract() -> dict:
    """프론트·AI 감독이 읽는 정적 규칙 데이터."""
    return {
        "rules_version": FACET_RULES_VERSION,
        "groups": [g.model_dump() for g in GROUPS.values()],
        "fields": [f.model_dump() for f in FIELDS.values()],
        "rules": [r.model_dump(by_alias=True) for r in RULES],
        "tones": {k: v["label"] for k, v in TONES.items()},
    }


def _check_rules() -> None:
    """규칙 데이터 자체의 참조 무결성. import 시점에 깨진 규칙을 막는다."""
    known = set(ENTRIES) | set(TONES)
    for f in FIELDS.values():
        assert f.group in GROUPS, f.id
        for o in f.options:
            if o.id.startswith("tone.") or (o.id[:1] == "C" and "." in o.id):
                assert o.id in known, (f.id, o.id)
    for r in RULES:
        for g in r.activate:
            assert g in GROUPS, (r.id, g)
        for fid in (*r.ask, *r.hide, *(x.field for x in r.recommend)):
            assert fid in FIELDS, (r.id, fid)
        for rec in r.recommend:
            opts = {o.id for o in FIELDS[rec.field].options}
            assert rec.value in opts, (r.id, rec.field, rec.value)
        for token in (
            *r.when.all_of,
            *r.when.any_of,
            *r.when.none_of,
            *(c.with_ for c in r.conflicts),
        ):
            assert token.endswith(".*") or token in known, (r.id, token)


_check_rules()
