"""데이터 흐름 설명 요청 → 고정 다이어그램 템플릿 레시피(R22) 연결.

HyperFrames 템플릿은 주체(발행자·저장소·소비자)가 순서대로 움직이는 다이어그램 한 종이다. 이 모듈은 템플릿이 스스로 선언한
범위(주체 개념과 별칭, 막 구성, 장면 수, 화면비)를 데이터로 두고, 요청이 그 범위 안인지 판정해 장면 지시를 자동으로 만든다.
판정은 특정 제품 이름이 아니라 개념 적용 범위로 한다. 범위 밖이면 사유를 `template_scope`에 남기고 아무것도 바꾸지 않는다.

- 범위 안: 요청 글(brief)이 템플릿의 주체 개념 3개 이상(발행자와 소비자 포함)을 말하거나 템플릿의 주제어를 직접 말한다.
- 자동 장면 지시는 `source=recipe_template`이다. 모델이 만든 값도 사용자 검토 결과도 아니며 승인 전에 사용자가 장면을 확인한다.
- 장면 수와 길이는 기존 길이 계산기(`plan_duration`)로 정한다. 길이를 채우려 장면을 늘리는 것도 템플릿의 막 안에서만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ttakkari.creative.v2.duration import (
    Beat,
    ContentProfile,
    DurationRequest,
    plan_duration,
)
from ttakkari.creative.v2.intent import InfoItem
from ttakkari.creative.v2.planner import ProductionDirection
from ttakkari.creative.v2.registry_data import (
    KAFKA_MAX_SCENES,
    KAFKA_MIN_SCENES,
    KAFKA_RECIPE_ID,
)


@dataclass(frozen=True)
class Act:
    act_id: str
    title: str
    narration: str
    needs_concept: str | None = None


@dataclass(frozen=True)
class FlowTemplate:
    preset: str
    recipe_id: str
    label: str
    fits: tuple[str, ...]
    aspects: tuple[str, ...]
    min_scenes: int
    max_scenes: int
    # 개념 → 별칭(소문자). 요청 글이 이 별칭을 말하면 그 개념을 다룬 것이다.
    concepts: dict[str, tuple[str, ...]]
    required_concepts: tuple[str, ...]
    min_concepts: int
    # 템플릿 이름을 직접 말한 경우도 범위 안이다(제품명이 개념 별칭과 겹치지 않는 사용자를 위해).
    subject_aliases: tuple[str, ...]
    acts: tuple[Act, ...]


MESSAGE_FLOW = FlowTemplate(
    preset="kafka_flow",
    recipe_id=KAFKA_RECIPE_ID,
    label="메시지 흐름(발행→저장→소비) 다이어그램",
    fits=("C01.tech_explainer", "C03.vector_motion_graphics", "C08.explainer"),
    aspects=("16:9",),
    min_scenes=KAFKA_MIN_SCENES,
    max_scenes=KAFKA_MAX_SCENES,
    concepts={
        "producer": ("producer", "프로듀서", "발행자"),
        "broker": ("broker", "브로커"),
        "consumer": ("consumer", "컨슈머", "소비자"),
        "partition": ("partition", "파티션"),
        "offset": ("offset", "오프셋"),
    },
    required_concepts=("producer", "consumer"),
    min_concepts=3,
    subject_aliases=("kafka", "카프카"),
    acts=(
        Act("publish", "발행 · Publish", "Producer가 메시지를 만들어 파티션으로 보냅니다."),
        Act("log", "로그 기록 · Append to Log", "Broker가 파티션 로그 끝에 메시지를 순서대로 덧붙입니다."),
        Act("consume", "소비 · Consume", "Consumer가 자신의 offset부터 메시지를 읽어 갑니다."),
        Act("parallel", "병렬 파티션 · Parallel Partitions", "파티션이 여러 개면 기록과 소비가 나란히 진행됩니다.", "partition"),
        Act("commit", "오프셋 커밋 · Commit Offset", "Consumer가 읽은 위치를 offset으로 남겨 이어 읽을 수 있습니다.", "offset"),
    ),
)
TEMPLATES = (MESSAGE_FLOW,)


def _text(brief: str, info_items: list[InfoItem]) -> str:
    # 요청 글(brief)만 본다. 전달 항목은 자동 구성이 만든 막 문구를 포함하므로 근거로 삼으면 판정이 스스로를 정당화한다.
    return brief.casefold()


def matched_concepts(template: FlowTemplate, brief: str, info_items: list[InfoItem]) -> list[str]:
    text = _text(brief, info_items)
    return [name for name, aliases in template.concepts.items() if any(a in text for a in aliases)]


def in_scope(template: FlowTemplate, brief: str, info_items: list[InfoItem]) -> bool:
    text = _text(brief, info_items)
    if any(a in text for a in template.subject_aliases):
        return True
    found = matched_concepts(template, brief, info_items)
    return len(found) >= template.min_concepts and all(c in found for c in template.required_concepts)


def _scope(template: FlowTemplate, status: str, reason: str, matched: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "template": template.preset,
        "recipe": template.recipe_id,
        "label": template.label,
        "status": status,
        "reason": reason,
        "matched_concepts": matched,
        "missing_concepts": [c for c in template.required_concepts if c not in matched],
        "supported": {
            "aspects": list(template.aspects),
            "scenes": [template.min_scenes, template.max_scenes],
            "concepts": sorted(template.concepts),
        },
        **extra,
    }


def _items_for(template: FlowTemplate, count: int) -> list[InfoItem]:
    return [
        InfoItem(id=act.act_id, title=act.title, narration=act.narration, essential=True)
        for act in template.acts[:count]
    ]


def _fits_length(items: list[InfoItem], request: DurationRequest) -> bool:
    profile = ContentProfile(
        beats=[Beat(id=i.id, title=i.title, narration=i.narration, info_weight=i.weight, essential=i.essential) for i in items]
    )
    return plan_duration(profile, request).status in ("target_met", "strict_met", "auto")


def direction_for(template: FlowTemplate, items: list[InfoItem]) -> ProductionDirection:
    return ProductionDirection.model_validate(
        {
            "source": "recipe_template",
            "reviewed": False,
            "intent": f"{template.label}: 요청의 주체 개념을 템플릿 막에 대응시킨다",
            "domain_knowledge": [],
            "scenes": [
                {
                    "scene_id": item.id,
                    "strategy": "code_motion",
                    "preset": template.preset,
                    "rationale": f"{template.label} 템플릿이 요청 범위(주체 개념 {', '.join(template.concepts)})를 직접 다룬다.",
                    "shots": [
                        {
                            "shot_id": f"{item.id}-shot",
                            "action": item.title,
                            "layers": ["visual", "captions"],
                            "required_capabilities": ["edit.captions"],
                        }
                    ],
                }
                for item in items
            ],
        }
    )


def plan_direction(
    brief: str,
    *,
    selections: list[str],
    aspect: str | None,
    info_items: list[InfoItem],
    request: DurationRequest,
) -> tuple[ProductionDirection | None, list[InfoItem] | None, dict[str, Any] | None]:
    """(장면 지시, 새 전달 항목, 범위 보고). 범위 보고가 None이면 이 템플릿과 관련 없는 요청이다."""
    template = MESSAGE_FLOW
    if not any(s in template.fits for s in selections):
        return None, None, None
    found = matched_concepts(template, brief, info_items)
    if not in_scope(template, brief, info_items):
        return None, None, _scope(
            template, "out_of_scope",
            f"요청이 템플릿의 주체 개념을 충분히 다루지 않습니다(필요: {', '.join(template.required_concepts)} 포함 "
            f"{template.min_concepts}개 이상, 확인된 개념: {', '.join(found) or '없음'}). 다른 레시피로 매칭합니다.",
            found,
        )
    aspect = aspect or "16:9"
    if aspect not in template.aspects:
        return None, None, _scope(
            template, "out_of_scope", f"화면비 {aspect}: 템플릿은 {', '.join(template.aspects)}만 지원합니다.", found
        )
    if info_items:
        if not template.min_scenes <= len(info_items) <= template.max_scenes:
            return None, None, _scope(
                template, "out_of_scope",
                f"전달 항목 {len(info_items)}개: 템플릿은 {template.min_scenes}~{template.max_scenes}장면만 담습니다.",
                found,
            )
        items, created = list(info_items), None
    else:
        floor = template.min_scenes
        floor += sum(1 for act in template.acts[floor:] if act.needs_concept in found)
        floor = min(floor, template.max_scenes)
        count = next(
            (n for n in range(floor, template.max_scenes + 1) if _fits_length(_items_for(template, n), request)),
            template.max_scenes,
        )
        items = created = _items_for(template, count)
    return direction_for(template, items), created, _scope(
        template, "applied",
        f"요청이 템플릿 범위 안이라 {len(items)}장면 {template.preset} 장면 지시를 자동 구성했습니다.",
        found, scenes=[i.id for i in items], scene_source="request_items" if info_items else "template_acts",
    )
