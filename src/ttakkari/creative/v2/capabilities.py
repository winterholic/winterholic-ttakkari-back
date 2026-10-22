"""능력 분류 체계와 Tool/Model/Skill/Recipe 레지스트리 모델.

핵심 규칙: 이름이 등록됐다는 사실은 실행 가능하다는 증거가 아니다.
각 항목은 능력(claim)마다 근거 수준을 따로 갖고, 수준은 호스트 실측 스냅샷과 lab 실행 기록에서만 올라간다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# 낮음 → 높음. 실행 증거 없는 설치(installed)와 실행 실측(executed)을 섞지 않는다.
Level = Literal[
    "missing", "declared", "installed", "executed_partial", "executed", "verified"
]
LEVELS: tuple[str, ...] = (
    "missing",
    "declared",
    "installed",
    "executed_partial",
    "executed",
    "verified",
)
LEVEL_RANK = {name: i for i, name in enumerate(LEVELS)}

Integration = Literal["creative_runtime", "app_service", "lab_only", "none"]
INTEGRATION_RANK = {"creative_runtime": 3, "app_service": 2, "lab_only": 1, "none": 0}

# 능력 id → (이름, 설명)
CAPABILITIES: dict[str, tuple[str, str]] = {
    "render.react_motion": (
        "React/Remotion 모션 렌더",
        "컴포넌트 기반 모션 컴포지션을 프레임 결정적으로 mp4로 렌더",
    ),
    "render.kinetic_typo": (
        "키네틱 타이포그래피",
        "글자 단위 모션과 가독성 규칙을 갖춘 타이포 애니메이션",
    ),
    "render.dataviz": (
        "데이터 시각화 렌더",
        "SVG/Canvas 차트·지도·다이어그램을 시간축으로 애니메이션",
    ),
    "render.three_realtime": (
        "Three.js 3D 렌더",
        "WebGL 3D 장면을 프레임 결정적으로 캡처",
    ),
    "render.blender_offline": (
        "Blender 오프라인 렌더",
        "EEVEE/Cycles로 고품질 프레임 렌더",
    ),
    "render.game_engine": ("게임 엔진 렌더", "Godot 등 게임 엔진 런타임에서 장면 캡처"),
    "render.vector_2d": ("벡터 2D 애니메이션", "SVG·벡터 리그 기반 2D 애니메이션"),
    "render.pixel_art": (
        "픽셀아트 렌더",
        "스프라이트·타일맵을 제한 해상도·프레임으로 렌더",
    ),
    "render.painterly_shader": (
        "회화·수묵 셰이더",
        "붓질·잉크 확산·종이 질감 전환 렌더",
    ),
    "render.stop_motion": ("스톱모션 프레임 합성", "단계 모션과 프레임별 포즈를 합성"),
    "asset.model_3d": ("3D 모델링", "캐릭터·소품·제품 3D 에셋 제작"),
    "asset.character_sheet": (
        "캐릭터 시트",
        "일관된 캐릭터 설정화·표정·포즈 시트 제작",
    ),
    "asset.environment": ("환경 에셋", "세계관에 맞는 배경·건축·소품 제작"),
    "asset.sprites": ("스프라이트·타일맵", "픽셀 스프라이트와 타일맵 제작"),
    "gen.image": ("이미지 생성", "텍스트에서 이미지 생성"),
    "gen.image_edit": (
        "참조 이미지 편집",
        "참조를 유지한 장면·포즈 변경(캐릭터 일관성)",
    ),
    "gen.video_i2v": ("이미지→영상 생성", "키프레임 이미지에서 짧은 영상 클립 생성"),
    "gen.video_t2v": ("텍스트→영상 생성", "텍스트에서 영상 클립 생성"),
    "gen.upscale": ("영상 업스케일", "해상도를 높이는 복원"),
    "gen.interpolate": ("프레임 보간", "프레임을 보간해 움직임을 부드럽게"),
    "gen.layer_separation": (
        "레이어 분리",
        "이미지를 전경·배경 레이어로 분리해 패럴랙스 준비",
    ),
    "anim.rig_3d": ("3D 리깅", "스켈레톤·스키닝으로 캐릭터를 움직일 수 있게 준비"),
    "anim.character_3d": ("3D 캐릭터 애니메이션", "걷기·달리기·연기 등 캐릭터 모션"),
    "anim.rig_2d": ("2D 리깅", "2D 파츠 리그와 모션"),
    "anim.lipsync": ("립싱크", "음성에 맞춘 입 모양 타임라인과 렌더"),
    "anim.expression": ("표정 연기", "감정에 맞는 표정 변화"),
    "anim.squash_stretch": ("스쿼시·스트레치", "과장 타이밍과 변형 연기"),
    "anim.crowd": ("군중 시뮬레이션", "다수 인물의 동선과 전투 배치"),
    "anim.camera_virtual": (
        "가상 카메라 연출",
        "경로·속도 곡선·심도를 가진 카메라 무브",
    ),
    "sim.physics_viz": ("물리·수학 시뮬레이션", "설명용 물리/수학 시뮬레이션과 시각화"),
    "sim.game_state": ("게임 상태·이벤트", "객체 상태 관리와 이벤트 기반 장면"),
    "vfx.composite": ("VFX 합성", "레이어·알파·색 합성"),
    "vfx.volumetric": ("볼류메트릭 효과", "안개·빛줄기 등 대기 효과"),
    "vfx.particles": ("파티클·연기·먼지", "파티클 기반 효과"),
    "light.cinematic": ("시네마틱 조명", "광원 배치·색 온도·그림자 설계"),
    "light.studio": ("스튜디오 조명", "제품·미니어처용 통제된 조명"),
    "audio.tts": ("음성 합성", "대본에서 내레이션·대사 음성 생성"),
    "audio.stt": ("음성 인식", "음성에서 전사·단어 시각 추출"),
    "audio.music_generate": ("음악 생성", "BGM·보컬 곡 생성"),
    "audio.music_analysis": ("음악 분석", "BPM·박자 그리드·구간 분석"),
    "audio.mix": ("오디오 믹싱", "덕킹·라우드니스·mux"),
    "audio.sfx": ("효과음", "효과음 합성·배치"),
    "audio.sfx_spatial": ("공간 음향", "공간감과 긴장을 만드는 사운드 디자인"),
    "edit.timeline": ("타임라인 조립", "컷·전환·합성으로 최종 영상 조립"),
    "edit.captions": ("자막·말풍선", "읽기 쉬운 자막·가사·말풍선 합성"),
    "edit.beat_cut": ("박자 컷 편집", "음악 박자에 맞춘 컷·모션"),
    "edit.color_grade": ("색보정", "색보정·필름 그레인"),
    "research.fact_check": ("사실·고증 검증", "역사·과학 사실과 출처를 확인"),
    "research.map_data": ("지도·통계 자료", "지도와 통계 자료 수집·시각화 입력"),
    "qa.machine_video": ("기계 영상 검사", "ffprobe·디코드·정지·피크·프레임 통계"),
    "qa.visual_semantic": (
        "시각·의미 검수",
        "멀티모달 모델 또는 사람의 의미·스타일 검수",
    ),
    "qa.audio_sync": ("음성·음악 동기 검사", "립싱크·박자 오차를 측정"),
    "qa.continuity": ("연속성 검사", "프레이밍·가림·충돌·시각 연속성 점검"),
    "orch.checkpoint_resume": ("체크포인트·재개", "단계 해시 캐시와 부분 재실행"),
    "plan.brief_storyboard": (
        "브리프·스토리보드",
        "주제 분해, 샷 목록, 프레임 예산 확정",
    ),
}

# 모든 레시피에 공통으로 붙는 필수 능력. 기계 검사가 없으면 어떤 결과도 통과로 말할 수 없다.
GLOBAL_REQUIRES: tuple[str, ...] = ("qa.machine_video", "edit.timeline")
GLOBAL_PREFERS: tuple[str, ...] = ("qa.visual_semantic", "orch.checkpoint_resume")


class Claim(Strict):
    """항목이 제공한다고 주장하는 능력 하나와 그 근거 기록."""

    capability: str
    records: list[str] = Field(default_factory=list)
    note: str = ""


class RegistryEntry(Strict):
    id: str
    kind: Literal["tool", "model", "skill"]
    name: str
    claims: list[Claim] = Field(default_factory=list)
    probes: list[str] = Field(default_factory=list)
    integration: Integration = "none"
    depends_on: list[str] = Field(default_factory=list)
    license: str | None = None
    cost_note: str = "로컬 실행, 과금 없음"
    resource_note: str = ""
    entry_point: str | None = None
    validation: list[str] = Field(default_factory=list)
    known_missing: bool = False
    notes: str = ""


class Recipe(Strict):
    """여러 능력의 실행 계획. requires는 AND of OR(각 그룹에서 하나라도 되면 충족)이다."""

    id: str
    name: str
    concept: str
    tool_candidates: list[str]
    requires: list[list[str]]
    prefers: list[list[str]] = Field(default_factory=list)
    fits: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    cases: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    version: str = "0.1.0"


class EntryState(Strict):
    id: str
    kind: str
    name: str
    installed: bool | None
    version: str | None
    integration: Integration
    levels: dict[str, Level]
    evidence: dict[str, list[str]]
    blockers: list[str]
    cost_note: str
    license: str | None
    notes: str


def best_level(levels: list[str]) -> Level:
    return max(levels, key=lambda lv: LEVEL_RANK[lv]) if levels else "missing"  # type: ignore[return-value]
