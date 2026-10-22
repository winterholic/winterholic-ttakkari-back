"""C01~C12 독립 카탈로그 시드. 키워드(hints)는 입력 보조일 뿐 계획 결정에 쓰지 않는다.

관계 대상은 다른 항목 id(`C03.pixel_art`) 또는 능력 id(`render.three_realtime`)다.
`a|b` 형태는 둘 중 하나면 충족되는 대안 묶음이다.
근거와 사례는 실제로 연결된 것만 적는다. 없으면 evidence_status=none으로 노출한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ttakkari.creative.v2.capabilities import Strict

RelationType = Literal[
    "requires",
    "prefers",
    "compatible_with",
    "conflicts_with",
    "alternative_to",
    "produces",
    "validates_with",
]
CATALOG_NAMES: dict[str, str] = {
    "C01": "콘텐츠 유형",
    "C02": "세계관·시대",
    "C03": "시각 스타일",
    "C04": "캐릭터·오브젝트",
    "C05": "환경·조명",
    "C06": "카메라·촬영",
    "C07": "모션·물리",
    "C08": "서사·구성",
    "C09": "음성·음악",
    "C10": "편집·타이포그래피",
    "C11": "제작·출력",
    "C12": "품질·고증",
}


class Relation(Strict):
    type: RelationType
    target: str
    strength: Literal["hard", "soft"] = "hard"
    note: str = ""


class Constraint(Strict):
    text: str
    hard: bool = False


class Evidence(Strict):
    kind: Literal["doc", "lab_record", "official"]
    ref: str
    note: str = ""


class Case(Strict):
    outcome: Literal["success", "partial", "failure"]
    ref: str
    note: str


class CatalogEntry(Strict):
    id: str
    catalog: str
    slug: str
    name: str
    version: str = "1.0.0"
    knowledge: str
    constraints: list[Constraint] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    cases: list[Case] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)

    @property
    def evidence_status(self) -> str:
        return "linked" if self.evidence or self.cases else "none"


ENTRIES: dict[str, CatalogEntry] = {}


def _e(
    cat,
    slug,
    name,
    knowledge,
    *,
    req=(),
    pref=(),
    conf=(),
    alt=(),
    compat=(),
    prod=(),
    val=(),
    hints=(),
    cons=(),
    soft_conf=(),
):
    rels = [Relation(type="requires", target=t) for t in req]
    rels += [Relation(type="prefers", target=t, strength="soft") for t in pref]
    rels += [Relation(type="conflicts_with", target=t, strength="hard") for t in conf]
    rels += [
        Relation(type="conflicts_with", target=t, strength="soft") for t in soft_conf
    ]
    rels += [Relation(type="alternative_to", target=t, strength="soft") for t in alt]
    rels += [
        Relation(type="compatible_with", target=t, strength="soft") for t in compat
    ]
    rels += [Relation(type="produces", target=t, strength="soft") for t in prod]
    rels += [Relation(type="validates_with", target=t, strength="soft") for t in val]
    entry = CatalogEntry(
        id=f"{cat}.{slug}",
        catalog=cat,
        slug=slug,
        name=name,
        knowledge=knowledge,
        relations=rels,
        hints=list(hints),
        constraints=[
            Constraint(text=c[0], hard=c[1])
            if isinstance(c, tuple)
            else Constraint(text=c)
            for c in cons
        ],
    )
    ENTRIES[entry.id] = entry


# --- C01 콘텐츠 유형 ---
_e(
    "C01",
    "music_video",
    "뮤직비디오",
    "음악 구간(인트로·절·후렴)이 장면 길이와 전환을 정하고 가사·박자에 컷을 맞춘다.",
    req=["audio.music_analysis", "edit.beat_cut"],
    pref=["audio.music_generate", "anim.lipsync"],
    compat=["C08.music_video_form", "C09.music_beat"],
    val=["C12.audio_sync"],
    hints=["뮤직비디오", "뮤비", "music video"],
)
_e(
    "C01",
    "story_film",
    "스토리 영화",
    "인물 목표와 갈등의 인과가 장면 순서를 정하고 샷 연속성이 몰입을 좌우한다.",
    req=["anim.camera_virtual"],
    pref=["audio.sfx_spatial"],
    compat=["C08.three_act"],
    val=["C12.temporal_continuity", "C12.character_consistency"],
    hints=["영화", "스토리", "단편영화"],
)
_e(
    "C01",
    "animation",
    "애니메이션",
    "캐릭터 설정 일관성과 움직임 타이밍이 핵심이며 장면마다 연기 의도가 필요하다.",
    req=["asset.character_sheet"],
    val=["C12.character_consistency", "C12.motion_naturalness"],
    hints=["애니메이션", "애니"],
)
_e(
    "C01",
    "advertisement",
    "광고",
    "짧은 시간에 한 메시지를 전달한다. 플랫폼 상한과 브랜드 규칙이 hard 제약이 되기 쉽다.",
    pref=["light.studio"],
    val=["C12.viewing_experience"],
    cons=[("플랫폼 상한이 있으면 strict로 처리한다", False)],
    hints=["광고", "CF", "홍보"],
)
_e(
    "C01",
    "tech_explainer",
    "기술 설명",
    "개념을 단계로 분해하고 단계마다 시각 근거를 둔다. 발화 길이가 장면 길이의 바닥이다.",
    req=["render.react_motion"],
    pref=["audio.tts", "render.dataviz"],
    compat=["C08.explainer", "C08.problem_solution"],
    val=["C12.factual_accuracy", "C12.subtitle_legibility"],
    hints=["기술 설명", "아키텍처", "개발자", "explainer"],
)
_e(
    "C01",
    "science_edu",
    "과학 교육",
    "이해 단계별 시각화와 시뮬레이션이 필요하고 사실 정확성을 먼저 검증한다.",
    req=["research.fact_check", "render.dataviz"],
    pref=["sim.physics_viz"],
    val=["C12.factual_accuracy"],
    hints=["과학", "교육", "원리"],
)
_e(
    "C01",
    "documentary",
    "다큐멘터리",
    "사실·출처·내레이션 타임라인이 중심이다. 자료 화면과 지도·통계로 근거를 보인다.",
    req=["research.fact_check", "audio.tts"],
    pref=["research.map_data", "render.dataviz"],
    compat=["C08.documentary_form", "C09.narration"],
    val=["C12.factual_accuracy", "C12.period_accuracy"],
    hints=["다큐", "다큐멘터리"],
)
_e(
    "C01",
    "game_trailer",
    "게임 트레일러",
    "게임 세계관과 핵심 플레이를 짧은 몽타주로 보여 주고 마지막에 타이틀을 둔다.",
    pref=["render.game_engine|render.three_realtime"],
    hints=["트레일러", "게임 홍보"],
)
_e(
    "C01",
    "gameplay",
    "게임플레이",
    "상태 변화와 이벤트가 장면을 만든다. 실제 게임 상태 로그와 화면이 일치해야 한다.",
    req=["sim.game_state"],
    pref=["render.game_engine|render.three_realtime"],
    hints=["게임플레이", "플레이 영상"],
)
_e(
    "C01",
    "shorts",
    "쇼츠",
    "첫 1~3초 훅과 세로 화면이 핵심이다. 플랫폼 상한이 strict 제약이다.",
    compat=["C11.aspect"],
    soft_conf=["C08.three_act"],
    cons=[("플랫폼 상한 초과 금지", True)],
    hints=["쇼츠", "shorts", "릴스"],
)
_e(
    "C01",
    "vlog",
    "브이로그",
    "실제 촬영 소재가 중심이다. 생성 영상으로 대체하면 정체성이 사라진다.",
    hints=["브이로그", "vlog"],
    cons=[("촬영 원본이 없으면 제작 불가", True)],
)
_e(
    "C01",
    "history_reenactment",
    "역사 재현",
    "시대 고증이 미술·의상·건축·소품 전부에 걸린다. 근거 없는 요소는 표시한다.",
    req=["research.fact_check", "asset.environment"],
    val=["C12.period_accuracy"],
    hints=["역사 재현", "사극"],
)
_e(
    "C01",
    "motion_comic",
    "모션코믹",
    "컷 이미지에 패럴랙스·효과선·말풍선을 얹는다. 컷 타이밍이 대사에 묶인다.",
    req=["gen.image", "render.react_motion"],
    pref=["gen.layer_separation"],
    compat=["C03.comic_2d", "C03.korean_webtoon"],
    hints=["모션코믹", "모션 코믹"],
)
_e(
    "C01",
    "presentation",
    "프레젠테이션",
    "슬라이드 구조와 발표 시간이 길이를 정한다. 글자 크기와 정보 밀도가 가독성을 정한다.",
    req=["render.react_motion"],
    val=["C12.subtitle_legibility"],
    hints=["프레젠테이션", "발표", "슬라이드"],
)
_e(
    "C01",
    "product_intro",
    "제품 소개",
    "제품의 핵심 가치를 순서대로 보여 준다. 실제 제품 이미지·스펙 정확성이 중요하다.",
    pref=["render.react_motion", "light.studio"],
    val=["C12.factual_accuracy"],
    hints=["제품 소개", "제품 영상"],
)

# --- C02 세계관·시대 ---
_e(
    "C02",
    "modern",
    "현대",
    "현대 환경·의상·기기. 고증 부담이 낮지만 실제 브랜드·인물 사용에 주의한다.",
    hints=["현대", "요즘"],
)
_e(
    "C02",
    "near_future",
    "근미래",
    "현재 기술의 연장선. 인터페이스·차량·건축에 개연성 있는 변형을 둔다.",
    alt=["C02.cyberpunk"],
    hints=["근미래", "near future"],
)
_e(
    "C02",
    "cyberpunk",
    "사이버펑크",
    "고밀도 도시, 네온, 비, 기업 권력과 하층 삶의 대비.",
    pref=["vfx.volumetric", "light.cinematic"],
    compat=["C05.neon", "C05.rain"],
    soft_conf=["C02.joseon", "C02.medieval"],
    hints=["사이버펑크", "cyberpunk"],
)
_e(
    "C02",
    "space_sf",
    "우주 SF",
    "우주선·행성·무중력 규칙. 소리 없는 진공 같은 물리 규칙을 따를지 정해야 한다.",
    pref=["vfx.volumetric", "audio.sfx"],
    soft_conf=["C02.joseon", "C02.medieval"],
    hints=["우주", "SF", "공상과학", "sci-fi"],
)
_e(
    "C02",
    "post_apocalypse",
    "포스트 아포칼립스",
    "붕괴한 문명, 자원 결핍, 낡은 질감. 조명과 대기가 분위기를 만든다.",
    compat=["C05.fog"],
    hints=["포스트 아포칼립스", "종말", "폐허"],
)
_e(
    "C02",
    "fantasy",
    "판타지",
    "마법·종족·지리를 규칙으로 고정해야 장면이 어긋나지 않는다.",
    compat=["C07.magic", "C04.creature"],
    hints=["판타지", "마법"],
)
_e(
    "C02",
    "medieval",
    "중세",
    "성·갑옷·무기·문장 체계. 지역·시기별 고증 차이가 크다.",
    req=["research.fact_check"],
    soft_conf=["C02.space_sf", "C02.cyberpunk"],
    hints=["중세"],
)
_e(
    "C02",
    "joseon",
    "조선시대",
    "한복·건축·소품·언어 고증이 필수다. 사극 말투와 복식 계층을 확인한다.",
    req=["research.fact_check"],
    val=["C12.period_accuracy"],
    soft_conf=["C02.space_sf", "C02.cyberpunk", "C02.near_future"],
    alt=["C02.alternate_history"],
    hints=["조선", "사극", "한양"],
)
_e(
    "C02",
    "three_kingdoms",
    "삼국시대",
    "고구려·백제·신라 복식과 무장, 성곽 양식을 구분해야 한다.",
    req=["research.fact_check"],
    val=["C12.period_accuracy"],
    hints=["삼국시대", "고구려", "백제", "신라"],
)
_e(
    "C02",
    "modern_history",
    "근현대 역사",
    "기록 사진·문서가 많아 사실 대조가 가능하다. 실존 인물 묘사에 주의한다.",
    req=["research.fact_check"],
    val=["C12.factual_accuracy", "C12.period_accuracy"],
    hints=["근현대", "일제강점기", "한국전쟁"],
)
_e(
    "C02",
    "ancient_civilization",
    "고대 문명",
    "유적·유물 근거와 추정 복원을 구분해 표시한다.",
    req=["research.fact_check"],
    hints=["고대", "이집트", "메소포타미아"],
)
_e(
    "C02",
    "alternate_history",
    "가상 역사",
    "실제 역사에서 갈라진 지점을 명시하고 이후 규칙을 일관되게 유지한다.",
    alt=["C02.joseon"],
    hints=["가상 역사", "대체역사"],
)
_e(
    "C02",
    "daily_life",
    "일상생활",
    "작은 소품과 동선에서 생활감이 나온다.",
    hints=["일상", "생활"],
)
_e(
    "C02",
    "surreal",
    "초현실",
    "논리를 의도적으로 비틀되 비틀림의 규칙은 일정해야 한다.",
    hints=["초현실", "꿈"],
)

# --- C03 시각 스타일 ---
_e(
    "C03",
    "photoreal",
    "실사",
    "실제 카메라 광학과 재질 반응을 따른다. 생성 결과의 결함이 가장 눈에 띄는 스타일.",
    pref=["gen.video_i2v|render.blender_offline"],
    conf=["C03.pixel_art", "C03.vector_motion_graphics", "C03.ink_wash"],
    soft_conf=[
        "C03.japanese_anime",
        "C03.korean_webtoon",
        "C03.western_cartoon",
        "C03.comic_2d",
        "C03.lowpoly_3d",
        "C03.cel_shaded_3d",
        "C03.clay_animation",
        "C03.oil_painting",
        "C03.watercolor",
    ],
    alt=["C03.cinematic_realism"],
    hints=["실사", "포토리얼", "photoreal"],
)
_e(
    "C03",
    "cinematic_realism",
    "영화적 실사",
    "렌즈 특성·조명 설계·색보정으로 영화 질감을 만든다.",
    pref=["light.cinematic", "edit.color_grade"],
    conf=["C03.pixel_art"],
    alt=["C03.photoreal"],
    hints=["영화적", "시네마틱", "cinematic"],
)
_e(
    "C03",
    "game_engine_3d",
    "게임 엔진 3D",
    "실시간 렌더 룩. 카메라와 UI가 게임 문법을 따른다.",
    req=["render.game_engine|render.three_realtime|render.blender_offline"],
    conf=["C03.pixel_art"],
    hints=["게임 엔진", "언리얼", "게임 3D"],
)
_e(
    "C03",
    "lowpoly_3d",
    "로우폴리 3D",
    "단순한 면 구성과 평면 색으로 구성한다. 모델 수가 적어 일관성 확보가 쉽다.",
    req=["asset.model_3d"],
    pref=["render.three_realtime|render.blender_offline"],
    soft_conf=["C03.photoreal"],
    hints=["로우폴리", "low poly"],
)
_e(
    "C03",
    "cel_shaded_3d",
    "셀 셰이딩 3D",
    "3D 모델에 윤곽선과 단계 음영을 적용해 만화 질감을 낸다.",
    req=["asset.model_3d"],
    pref=["render.blender_offline|render.three_realtime"],
    soft_conf=["C03.photoreal"],
    hints=["셀 셰이딩", "툰 셰이딩", "cel shading"],
)
_e(
    "C03",
    "japanese_anime",
    "일본 애니메이션",
    "선·색·눈 표현과 제한 애니메이션 문법. 작화 일관성이 핵심이다.",
    req=["asset.character_sheet"],
    soft_conf=["C03.photoreal"],
    conf=["C03.pixel_art"],
    hints=["일본 애니", "애니메이션 스타일", "아니메", "anime"],
)
_e(
    "C03",
    "korean_webtoon",
    "한국 웹툰",
    "세로 스크롤 컷 구성과 감정 표현. 컷 간 호흡이 중요하다.",
    req=["gen.image"],
    soft_conf=["C03.photoreal"],
    compat=["C01.motion_comic"],
    hints=["웹툰"],
)
_e(
    "C03",
    "western_cartoon",
    "서양 카툰",
    "과장된 형태와 스쿼시·스트레치 연기.",
    pref=["anim.squash_stretch"],
    soft_conf=["C03.photoreal"],
    hints=["카툰", "cartoon"],
)
_e(
    "C03",
    "comic_2d",
    "2D 만화",
    "컷 분할, 효과선, 말풍선 문법.",
    pref=["edit.captions"],
    soft_conf=["C03.photoreal"],
    compat=["C01.motion_comic"],
    hints=["만화", "코믹"],
)
_e(
    "C03",
    "pixel_art",
    "픽셀아트",
    "고정 해상도와 제한 팔레트, 정수 배율. 부드러운 보간이 정체성을 깬다.",
    req=["render.pixel_art"],
    conf=[
        "C03.photoreal",
        "C03.cinematic_realism",
        "C03.game_engine_3d",
        "C03.japanese_anime",
    ],
    cons=[("정수 배율과 제한 팔레트 유지", True)],
    hints=["픽셀", "pixel"],
)
_e(
    "C03",
    "stop_motion",
    "스톱모션",
    "낮은 프레임 레이트와 단계 포즈, 실제 물체 질감.",
    req=["render.stop_motion"],
    soft_conf=["C03.photoreal"],
    hints=["스톱모션"],
)
_e(
    "C03",
    "clay_animation",
    "클레이 애니메이션",
    "점토 질감과 지문 같은 불완전성을 의도적으로 유지한다.",
    req=["render.stop_motion"],
    soft_conf=["C03.photoreal"],
    hints=["클레이", "점토"],
)
_e(
    "C03",
    "ink_wash",
    "수묵화",
    "붓질과 먹 번짐, 여백. 색은 절제한다.",
    req=["render.painterly_shader"],
    conf=["C03.photoreal"],
    hints=["수묵", "먹"],
)
_e(
    "C03",
    "oil_painting",
    "유화",
    "두꺼운 붓 자국과 색 혼합 질감.",
    pref=["render.painterly_shader"],
    soft_conf=["C03.photoreal"],
    hints=["유화"],
)
_e(
    "C03",
    "watercolor",
    "수채화",
    "번짐과 투명한 겹침, 종이 질감.",
    pref=["render.painterly_shader"],
    soft_conf=["C03.photoreal"],
    hints=["수채화"],
)
_e(
    "C03",
    "vector_motion_graphics",
    "벡터 모션그래픽",
    "단순한 도형과 정확한 타이밍. 정보 전달에 적합하다.",
    req=["render.react_motion"],
    conf=["C03.photoreal"],
    hints=["모션그래픽", "모션 그래픽"],
)
_e(
    "C03",
    "ui_animation",
    "UI 애니메이션",
    "인터페이스 동작을 정확한 상태 전이로 보여 준다.",
    req=["render.react_motion"],
    hints=["UI 애니", "화면 녹화", "UI"],
)
_e(
    "C03",
    "infographic",
    "인포그래픽",
    "데이터를 도형과 라벨로 구조화한다. 수치 정확성이 핵심이다.",
    req=["render.dataviz"],
    val=["C12.factual_accuracy"],
    hints=["인포그래픽"],
)
_e(
    "C03",
    "miniature_diorama",
    "미니어처 디오라마",
    "틸트시프트와 소형 세트 질감, 부드러운 조명.",
    pref=["light.studio", "asset.environment"],
    hints=["미니어처", "디오라마"],
)

# --- C04 캐릭터·오브젝트 ---
for slug, name, know, req in [
    (
        "human",
        "인간",
        "비율·표정·의상·소품 일관성. 얼굴은 시트로 고정한다.",
        ["asset.character_sheet"],
    ),
    ("animal", "동물", "종별 골격과 보행 주기를 지킨다.", []),
    (
        "anthropomorphic",
        "의인화 캐릭터",
        "동물·사물의 정체성과 인간 표정 사이 균형.",
        ["asset.character_sheet"],
    ),
    (
        "robot",
        "로봇",
        "관절 가동 범위와 재질. 기계 움직임 규칙을 정한다.",
        ["asset.model_3d|asset.character_sheet"],
    ),
    ("monster", "몬스터", "위협적 실루엣과 움직임 규칙, 규모감.", []),
    ("creature", "크리처", "해부 논리와 이동 방식이 설득력을 만든다.", []),
    (
        "vehicle",
        "차량",
        "바퀴·서스펜션·속도감. 도로 접지가 중요하다.",
        ["asset.model_3d"],
    ),
    ("spaceship", "우주선", "규모 기준과 추진 표현.", ["asset.model_3d"]),
    (
        "building",
        "건물",
        "양식·규모·재질. 시대 고증과 연결된다.",
        ["asset.environment"],
    ),
    ("nature_object", "자연물", "식물·바위·물의 유기적 변이.", []),
    ("weapon", "무기", "시대·문화별 형태와 사용 동작 일치.", ["asset.model_3d"]),
    ("machine", "기계", "작동 원리가 보이는 움직임.", ["asset.model_3d"]),
    (
        "ui_component",
        "UI 컴포넌트",
        "상태·간격·타이밍을 코드로 정의한다.",
        ["render.react_motion"],
    ),
]:
    _e(
        "C04",
        slug,
        name,
        know,
        req=req,
        val=["C12.character_consistency"]
        if slug in ("human", "anthropomorphic", "monster", "creature")
        else (),
        hints=[name],
    )

# --- C05 환경·조명 ---
for slug, name, know, kw in [
    ("day", "낮", "자연광 기준. 그림자 방향이 장면 간 일관돼야 한다.", {}),
    (
        "night",
        "밤",
        "저조도에서 노이즈와 암부 디테일 손실을 관리한다.",
        {"pref": ["light.cinematic"]},
    ),
    (
        "dusk",
        "황혼",
        "짧은 시간대의 색 온도 변화를 장면 순서와 맞춘다.",
        {"pref": ["light.cinematic"]},
    ),
    ("indoor", "실내", "광원 위치가 공간 구조를 설명한다.", {}),
    ("outdoor", "실외", "하늘과 지형이 스케일을 정한다.", {}),
    (
        "rain",
        "비",
        "빗줄기·젖은 표면 반사·소리가 함께 필요하다.",
        {"pref": ["vfx.particles", "audio.sfx"]},
    ),
    ("snow", "눈", "입자 밀도와 반사광, 소리 흡수.", {"pref": ["vfx.particles"]}),
    (
        "fog",
        "안개",
        "깊이에 따른 대비 감소가 공간감을 만든다.",
        {"pref": ["vfx.volumetric"]},
    ),
    (
        "backlight",
        "역광",
        "실루엣과 림 라이트. 노출 관리가 어렵다.",
        {"pref": ["light.cinematic"]},
    ),
    (
        "neon",
        "네온",
        "색 대비와 번짐, 젖은 바닥 반사.",
        {"pref": ["light.cinematic", "vfx.volumetric"]},
    ),
    (
        "volumetric_light",
        "볼류메트릭 조명",
        "빛줄기와 먼지 입자로 대기를 보인다.",
        {"req": ["vfx.volumetric"], "pref": ["light.cinematic"]},
    ),
    (
        "studio_light",
        "스튜디오 조명",
        "키·필·림 구성으로 제품과 얼굴을 통제한다.",
        {"req": ["light.studio"]},
    ),
    ("natural_light", "자연광", "시간대와 날씨로 조명이 결정된다.", {}),
    (
        "battlefield",
        "전쟁터",
        "먼지·연기·군중 동선과 지형 연속성.",
        {"req": ["vfx.particles"], "pref": ["anim.crowd"]},
    ),
    ("forest", "숲", "층층의 깊이와 빛 새어듦.", {}),
    ("city", "도시", "스카이라인과 밀도, 간판·교통 같은 생활 표지.", {}),
    ("space", "우주", "진공의 조명은 단일 강한 광원과 어둠이다.", {}),
    ("sea", "바다", "파도 시뮬레이션과 수평선 안정.", {"pref": ["sim.physics_viz"]}),
    ("desert", "사막", "강한 광원과 모래 입자, 거리 감각 상실.", {}),
]:
    _e("C05", slug, name, know, hints=[name], **kw)

# --- C06 카메라·촬영 ---
for slug, name, know, kw in [
    ("static", "고정샷", "프레이밍이 전부다. 구도 안정성이 핵심.", {}),
    (
        "pan",
        "팬",
        "수평 회전. 속도 곡선과 시작·끝 프레임이 중요하다.",
        {"req": ["anim.camera_virtual"]},
    ),
    (
        "tilt",
        "틸트",
        "수직 회전. 인물 시선 방향과 맞춘다.",
        {"req": ["anim.camera_virtual"]},
    ),
    (
        "dolly",
        "돌리",
        "전후 이동. 시차로 깊이를 만든다.",
        {"req": ["anim.camera_virtual"]},
    ),
    (
        "tracking",
        "트래킹",
        "피사체를 따라 이동. 피사체 속도와 맞춘다.",
        {"req": ["anim.camera_virtual"]},
    ),
    (
        "orbit",
        "오비트",
        "피사체 주위 회전. 배경 일관성이 필요하다.",
        {"req": ["anim.camera_virtual"]},
    ),
    (
        "crane",
        "크레인",
        "수직·대각 이동으로 규모를 드러낸다.",
        {"req": ["anim.camera_virtual"]},
    ),
    (
        "aerial",
        "항공샷",
        "고도와 지형 스케일. 지형 자산이 필요하다.",
        {"req": ["anim.camera_virtual"], "pref": ["asset.environment"]},
    ),
    ("pov", "POV", "1인칭 시점. 손·몸 일부 노출과 흔들림 규칙.", {}),
    (
        "handheld",
        "핸드헬드",
        "미세한 흔들림. 과하면 멀미를 일으킨다.",
        {"req": ["anim.camera_virtual"]},
    ),
    ("macro", "매크로", "근접 촬영과 얕은 심도.", {"pref": ["light.studio"]}),
    ("wide", "와이드", "공간 설명. 디테일 밀도가 요구된다.", {}),
    ("closeup", "클로즈업", "감정 전달. 얼굴 품질이 그대로 드러난다.", {}),
    (
        "isometric",
        "아이소메트릭",
        "고정 각도 투영. 게임·설명 영상에서 쓴다.",
        {"pref": ["render.three_realtime"]},
    ),
    ("topdown", "탑다운", "수직 시점. 지도·전술 설명과 맞는다.", {}),
    (
        "sidescroll",
        "사이드스크롤",
        "수평 이동. 레이어 속도 차이로 깊이를 만든다.",
        {"pref": ["render.react_motion"]},
    ),
    (
        "parallax_2_5d",
        "2.5D 패럴랙스",
        "레이어 분리 이미지에 카메라 이동을 적용한다.",
        {"req": ["gen.layer_separation"], "pref": ["render.react_motion"]},
    ),
]:
    _e("C06", slug, name, know, hints=[name], **kw)

# --- C07 모션·물리 ---
for slug, name, know, kw in [
    (
        "walk",
        "걷기",
        "보행 주기와 체중 이동. 발 미끄러짐 검사가 필요하다.",
        {"req": ["anim.character_3d|anim.rig_2d|gen.video_i2v"]},
    ),
    (
        "run",
        "달리기",
        "공중 구간과 팔 스윙. 카메라 속도와 맞춘다.",
        {"req": ["anim.character_3d|anim.rig_2d|gen.video_i2v"]},
    ),
    (
        "dance",
        "춤",
        "박자에 맞춘 동작. 음악 분석이 입력이다.",
        {"req": ["anim.character_3d|gen.video_i2v"], "pref": ["audio.music_analysis"]},
    ),
    (
        "dialogue",
        "대화",
        "시선·반응·침묵의 타이밍. 대사 길이가 장면 길이를 정한다.",
        {"pref": ["audio.tts", "anim.lipsync"]},
    ),
    (
        "lipsync",
        "립싱크",
        "음절 단위 입 모양과 음성 동기.",
        {"req": ["anim.lipsync", "audio.tts"], "val": ["C12.audio_sync"]},
    ),
    ("expression", "표정", "감정 곡선에 맞춘 표정 변화.", {"req": ["anim.expression"]}),
    (
        "battle",
        "전투",
        "동선·거리·타격 타이밍. 공간 연속성이 중요하다.",
        {"req": ["anim.character_3d|anim.crowd"], "val": ["C12.spatial_continuity"]},
    ),
    ("vehicle_move", "차량 이동", "속도·서스펜션·궤적.", {}),
    ("crowd", "군중", "개체 변이와 집단 흐름.", {"req": ["anim.crowd"]}),
    (
        "cloth_hair",
        "천·머리카락",
        "관성과 바람 반응. 시뮬레이션 비용이 크다.",
        {"req": ["sim.physics_viz|render.blender_offline"]},
    ),
    ("water", "물", "유체 표현. 실시간은 근사다.", {"pref": ["sim.physics_viz"]}),
    ("smoke", "연기", "입자·볼륨 기반 확산.", {"req": ["vfx.particles"]}),
    ("fire", "불", "깜빡임과 열 왜곡, 조명 영향.", {"req": ["vfx.particles"]}),
    (
        "destruction",
        "파괴",
        "파편·충격·카메라 반응.",
        {"req": ["vfx.particles", "sim.physics_viz|render.blender_offline"]},
    ),
    ("collision", "충돌", "충격 순간의 프레임 처리와 반응.", {}),
    (
        "magic",
        "마법",
        "규칙 있는 발광·입자 효과.",
        {"req": ["vfx.particles", "vfx.composite"]},
    ),
    (
        "ui_animation",
        "UI 애니메이션",
        "상태 전이와 이징.",
        {"req": ["render.react_motion"]},
    ),
]:
    _e("C07", slug, name, know, hints=[name], **kw)

# --- C08 서사·구성 ---
for slug, name, know, kw in [
    (
        "three_act",
        "3막 구조",
        "설정·대립·해결. 막 전환점이 장면 길이 배분을 정한다.",
        {},
    ),
    ("kishotenkyeol", "기승전결", "전개와 전환의 균형.", {}),
    ("explainer", "설명형", "개념을 단계로 분해해 순서대로 설명한다.", {}),
    ("problem_solution", "문제 해결형", "문제 제시, 원인, 해결, 결과.", {}),
    (
        "documentary_form",
        "다큐멘터리형",
        "주장·근거·증언·정리.",
        {"req": ["research.fact_check"]},
    ),
    ("montage", "몽타주", "짧은 컷 모음으로 시간·감정을 압축한다.", {}),
    (
        "music_video_form",
        "뮤직비디오형",
        "절·후렴 구간에 맞춘 시각 전개.",
        {"req": ["audio.music_analysis"]},
    ),
    ("twist", "반전형", "복선 배치와 공개 시점이 핵심이다.", {}),
    ("tutorial", "튜토리얼형", "따라 할 수 있는 단계와 결과 확인.", {}),
    (
        "dialogue_form",
        "대화형",
        "인물 간 대사 교환이 구조를 이룬다.",
        {"pref": ["audio.tts"]},
    ),
    ("game_cutscene", "게임 컷신형", "게임 세계 규칙 안에서 서사를 전달한다.", {}),
]:
    _e("C08", slug, name, know, hints=[name], **kw)

# --- C09 음성·음악 ---
for slug, name, know, kw in [
    (
        "narration",
        "내레이션",
        "발화 시간이 장면 길이의 바닥이다. 실측 오디오로 최종 확정한다.",
        {"req": ["audio.tts"]},
    ),
    ("dialogue", "대화", "화자별 음색과 간격. 대사 겹침 규칙.", {"req": ["audio.tts"]}),
    (
        "character_voice",
        "캐릭터 음성",
        "캐릭터 정체성에 맞는 음색 일관성.",
        {"req": ["audio.tts"]},
    ),
    (
        "vocal",
        "보컬",
        "가사 정확도와 박자 동기.",
        {"req": ["audio.music_generate"], "val": ["C12.audio_sync"]},
    ),
    (
        "bgm",
        "배경음악",
        "장면 감정과 대사 가독성을 해치지 않는 음량.",
        {"req": ["audio.music_generate|audio.mix"]},
    ),
    (
        "ambience",
        "환경음",
        "공간의 바닥 소음이 연속성을 만든다.",
        {"req": ["audio.sfx"]},
    ),
    ("sfx", "효과음", "동작 순간과 소리 시점이 맞아야 한다.", {"req": ["audio.sfx"]}),
    (
        "spatial_audio",
        "공간음향",
        "위치·거리·잔향. 긴장 연출에 쓴다.",
        {"req": ["audio.sfx_spatial"]},
    ),
    (
        "music_beat",
        "음악 비트",
        "박자 그리드가 컷 시점을 정한다.",
        {"req": ["audio.music_analysis"]},
    ),
    (
        "emotional_sound",
        "감정별 사운드 디자인",
        "감정 곡선에 맞춘 음색·밀도 설계.",
        {"pref": ["audio.mix"]},
    ),
]:
    _e("C09", slug, name, know, hints=[name], **kw)

# --- C10 편집·타이포그래피 ---
for slug, name, know, kw in [
    (
        "cut",
        "컷",
        "리듬 단위. 장면 길이 분포가 템포를 정한다.",
        {"req": ["edit.timeline"]},
    ),
    (
        "dissolve",
        "디졸브",
        "시간 경과·감정 이어짐. 남용하면 늘어진다.",
        {"req": ["edit.timeline"]},
    ),
    (
        "match_cut",
        "매치컷",
        "형태·동작 일치로 장면을 잇는다. 사전 설계가 필요하다.",
        {"req": ["edit.timeline"]},
    ),
    (
        "montage",
        "몽타주 편집",
        "짧은 컷을 규칙으로 배열한다.",
        {"req": ["edit.timeline"]},
    ),
    (
        "speed_ramp",
        "스피드 램프",
        "속도 변화로 강조. 프레임 보간 품질이 영향.",
        {"req": ["edit.timeline"], "pref": ["gen.interpolate"]},
    ),
    (
        "subtitles",
        "자막",
        "글자 크기 하한과 읽는 시간 확보.",
        {"req": ["edit.captions"], "val": ["C12.subtitle_legibility"]},
    ),
    (
        "kinetic_typography",
        "키네틱 타이포그래피",
        "글자 움직임이 의미 강조와 박자를 만든다.",
        {"req": ["render.kinetic_typo"]},
    ),
    ("speech_bubble", "말풍선", "읽는 순서와 컷 안 배치.", {"req": ["edit.captions"]}),
    (
        "speed_lines",
        "효과선",
        "속도·충격의 만화 문법.",
        {"pref": ["render.react_motion"]},
    ),
    (
        "ui_overlay",
        "UI 오버레이",
        "정보 위계를 방해하지 않는 위치와 크기.",
        {"req": ["render.react_motion"]},
    ),
    (
        "color_grade",
        "색보정",
        "장면 간 색 일관성과 분위기.",
        {"req": ["edit.color_grade"]},
    ),
    (
        "film_grain",
        "필름 그레인",
        "질감 부여. 압축 아티팩트와 구분한다.",
        {"pref": ["edit.color_grade"]},
    ),
]:
    _e("C10", slug, name, know, hints=[name], **kw)

# --- C11 제작·출력 ---
for slug, name, know, kw in [
    (
        "duration",
        "영상 길이",
        "자동 결정이 기본이고 희망 시간은 soft다. 정확한 길이와 플랫폼 상한만 strict.",
        {},
    ),
    ("aspect", "화면 비율", "플랫폼과 구도를 정한다. 변경은 재구성이 필요하다.", {}),
    ("resolution", "해상도", "렌더 시간과 메모리에 비례한다.", {}),
    ("fps", "FPS", "프레임 단위 길이 계산의 기준이다.", {}),
    ("codec", "코덱", "호환성과 품질의 균형.", {"req": ["edit.timeline"]}),
    (
        "quality_target",
        "품질 목표",
        "기계 검사와 시각 검수 기준을 함께 정한다.",
        {"req": ["qa.machine_video"]},
    ),
    (
        "budget",
        "제작 예산",
        "유료 서비스는 승인 없이 쓰지 않는다. 로컬 실행 시간도 비용이다.",
        {},
    ),
    (
        "resource_policy",
        "자원 사용 정책",
        "GPU 동시 로딩 금지와 공용 lease를 따른다.",
        {},
    ),
    (
        "render_strategy",
        "렌더링 전략",
        "장면 단위 렌더와 캐시, 부분 재실행. 거대한 단일 렌더를 피한다.",
        {"pref": ["orch.checkpoint_resume"]},
    ),
]:
    _e("C11", slug, name, know, hints=[name], **kw)

# --- C12 품질·고증 ---
for slug, name, know, kw in [
    (
        "character_consistency",
        "캐릭터 일관성",
        "시트 대비 장면별 외형 차이. 정량 평가는 아직 없다.",
        {"req": ["qa.visual_semantic"]},
    ),
    (
        "spatial_continuity",
        "공간 연속성",
        "컷 사이 방향·위치 일치.",
        {"req": ["qa.continuity"]},
    ),
    (
        "temporal_continuity",
        "시간 연속성",
        "조명·소품 상태의 시간 순서 일치.",
        {"req": ["qa.continuity"]},
    ),
    (
        "motion_naturalness",
        "움직임 자연스러움",
        "발 미끄러짐·떨림 같은 결함 점검.",
        {"req": ["qa.machine_video"], "pref": ["qa.visual_semantic"]},
    ),
    (
        "period_accuracy",
        "시대 고증",
        "의상·건축·소품 근거 확인.",
        {"req": ["research.fact_check"]},
    ),
    (
        "subtitle_legibility",
        "자막 가독성",
        "글자 크기·대비·표시 시간.",
        {"req": ["qa.machine_video"]},
    ),
    (
        "audio_sync",
        "오디오 동기화",
        "립싱크·박자 오차 측정.",
        {"req": ["qa.audio_sync"]},
    ),
    (
        "style_consistency",
        "스타일 일관성",
        "Style DNA 대비 장면별 이탈 검수.",
        {"req": ["qa.visual_semantic"]},
    ),
    (
        "viewing_experience",
        "시청 경험",
        "실제 시청과 청취로만 판정한다.",
        {"req": ["qa.visual_semantic"]},
    ),
    (
        "factual_accuracy",
        "사실 정확성",
        "주장별 출처 대조.",
        {"req": ["research.fact_check"]},
    ),
]:
    _e("C12", slug, name, know, hints=[name], **kw)

# --- 근거·사례: 실제 기록이 있는 항목만 연결한다 ---
_LINKS: dict[str, dict[str, list]] = {
    "C01.music_video": {
        "cases": [
            (
                "partial",
                "remotion-beat-v1-v2",
                "합성 트랙 기준 컷 오차 최대 0.78프레임. 조건부 검수이며 전체 레시피·품질 통과 증거가 아님",
            ),
            (
                "partial",
                "legacy-catalog-sample-music-video",
                "v1 카탈로그 샘플 렌더. 검수 미수행이며 전체 레시피·품질 통과 증거가 아님",
            ),
        ],
        "evidence": [("doc", "ADR 0065", "v1 포맷 카탈로그")],
    },
    "C01.tech_explainer": {
        "cases": [
            ("success", "legacy-catalog-sample-tech-explainer", "v1 카탈로그 샘플 렌더")
        ],
        "evidence": [("doc", "ADR 0065", "v1 포맷 카탈로그")],
    },
    "C01.shorts": {
        "cases": [
            ("success", "legacy-catalog-sample-shorts-9-16", "v1 9:16 샘플 렌더")
        ],
        "evidence": [("doc", "ADR 0065", "v1 포맷 카탈로그")],
    },
    "C01.documentary": {
        "cases": [
            (
                "success",
                "legacy-catalog-longform-narration-story",
                "v1 장편 내레이션 샘플",
            )
        ],
        "evidence": [],
    },
    "C01.story_film": {
        "cases": [
            (
                "partial",
                "three-cinematic-v1-v2",
                "v1 검은 프레임 5장·과노출 44프레임을 v2에서 0으로 줄임. 검수는 conditional",
            )
        ],
        "evidence": [],
    },
    "C06.dolly": {
        "cases": [
            (
                "partial",
                "three-cinematic-v1-v2",
                "카메라 감사는 카메라 점만 본다. 렌즈 근접 물체 문제는 못 잡음",
            )
        ],
        "evidence": [],
    },
    "C07.lipsync": {
        "cases": [
            (
                "partial",
                "three-character-talk-v1-v2",
                "음절 가중치 배분 립싱크. forced alignment 아님, 표정은 미소·눈썹뿐",
            )
        ],
        "evidence": [],
    },
    "C09.vocal": {
        "cases": [
            (
                "partial",
                "acestep-ko-ballad-run1",
                "한국어 4줄 30초, Whisper 왕복 전사 일치. R19 구성 요소 사례로만 연결됐고 검수는 conditional이며 전체 레시피·품질 통과 증거가 아님",
            ),
            (
                "failure",
                "acestep-ko-edit-variants",
                "구간 재생성 2회가 의도한 가사를 만들지 못함",
            ),
        ],
        "evidence": [],
    },
    "C09.bgm": {
        "cases": [
            (
                "success",
                "acestep-instrumental-cues",
                "인스트루멘탈 큐 2종 생성, 청취 평가 없음",
            )
        ],
        "evidence": [],
    },
    "C09.narration": {
        "cases": [
            (
                "success",
                "three-character-talk-v1-v2",
                "Supertonic 3 한국어 58음절 13.299초(RTF 0.25)",
            )
        ],
        "evidence": [],
    },
    "C10.kinetic_typography": {
        "cases": [
            (
                "partial",
                "remotion-intro-v1-v2",
                "프레임 검수로 결함 9건 수정. 검수는 conditional",
            )
        ],
        "evidence": [],
    },
    "C11.render_strategy": {
        "cases": [
            (
                "success",
                "pipeline-checkpoints-demo",
                "입력 해시 최신성 판정과 게이트 전파 시연",
            ),
            (
                "partial",
                "remotion-intro-supersample",
                "2배 슈퍼샘플은 시간 3.8배·메모리 3.6배, 소프트 엣지 개선 작음",
            ),
        ],
        "evidence": [],
    },
    "C04.human": {
        "cases": [
            (
                "partial",
                "comfy-flux2-klein-character-edit",
                "참조 편집으로 3개 장면 이동, 얼굴 유사도 정량 평가 없음",
            ),
            (
                "partial",
                "comfy-qwen-image-edit-2511",
                "3건 중 1건 성공, MPS 메모리 한계로 2건 실패",
            ),
        ],
        "evidence": [],
    },
    "C03.cinematic_realism": {
        "cases": [
            (
                "partial",
                "blender-eevee-cycles-probe",
                "1080p 1프레임 EEVEE 26.2초, Cycles Metal 159.1초",
            )
        ],
        "evidence": [],
    },
}
for _id, _link in _LINKS.items():
    _entry = ENTRIES[_id]
    _entry.cases = [Case(outcome=o, ref=r, note=n) for o, r, n in _link["cases"]]
    _entry.evidence = [Evidence(kind=k, ref=r, note=n) for k, r, n in _link["evidence"]]
