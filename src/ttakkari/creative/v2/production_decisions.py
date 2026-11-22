"""조건 조합 → 구체 제작 결정. 계약: docs/design/creative-studio-v2-intent-v2.md §16

의도 v2의 조건·답변·톤, 노출 규칙의 추천값, 합성된 Style DNA, 길이 계산 결과를 작화·조명·카메라·음향·전환·정보 구조의
결정 목록으로 풀어 계획(`production_decisions`)과 장면(`scene.production`·`scene.information`)에 남긴다.
새 카탈로그를 만들지 않고 기존 facets·카탈로그·Style DNA·길이 계산기·엔진 어댑터의 값만 읽는다.

결정마다 실행 상태를 구분한다(조용히 "반영됨"으로 뭉뚱그리지 않는다).
- executable: 선택된 엔진 어댑터가 실제로 소비하는 렌더 제어(`render_controls`)가 있고 사용자의 말에서 나온 값이다.
- plan_only: 계획과 장면 기록, AI 감독·스토리보드 입력에만 반영된다. 실행기가 소비하지 않는다(사유 기록).
- unsupported: 필요한 능력이 없거나 검증되지 않아 만들 수 없다(사유와 대안 기록).
출처가 사용자가 아닌 값(규칙 추천·AI 해석)은 렌더 제어를 바꾸지 않는다. 사용자가 확정해야 반영된다.
"""

from __future__ import annotations

from typing import Any

from ttakkari.creative.v2.catalog_data import ENTRIES
from ttakkari.creative.v2.facets import FIELDS, resolve_layout
from ttakkari.creative.v2.intent_v2 import TONES, IntentSpecV2, label_of
from ttakkari.creative.v2.registry import Registry

SCHEMA = 1
CATEGORIES = {
    "visual": "작화·화풍",
    "lighting": "조명",
    "camera": "카메라",
    "audio": "음향",
    "editing": "전환·편집",
    "motion": "액션·모션",
    "information": "정보 구조",
}
GROUP_CATEGORY = {
    "character": "visual",
    "drawing": "visual",
    "consistency": "visual",
    "panel": "visual",
    "world": "visual",
    "horror": "visual",
    "motion_graphics": "visual",
    "lighting_vfx": "lighting",
    "space_camera": "camera",
    "music": "audio",
    "lyrics": "information",
    "narration": "audio",
    "av_sync": "editing",
    "cut_direction": "editing",
    "explain": "information",
    "diagram": "information",
    "code": "information",
    "action": "motion",
}
FIELD_CATEGORY = {"cut.shots": "camera", "voice.subtitles": "information"}
CATALOG_CATEGORY = {
    "C02": "visual",
    "C03": "visual",
    "C04": "visual",
    "C05": "visual",
    "C06": "camera",
    "C07": "motion",
    "C08": "information",
    "C09": "audio",
    "C10": "editing",
}
# 사용자의 말(확정·자유 입력)에서 나온 값만 렌더 제어로 이어진다. 추천·AI 해석은 확정 전까지 렌더를 바꾸지 않는다.
USER_ORIGINS = ("user_confirmed", "user_free_text")
ORIGIN_RANK = {
    "user_confirmed": 0,
    "user_free_text": 1,
    "ai_inferred": 2,
    "rule_recommended": 3,
    "style_dna": 4,
}
# (설정 또는 카탈로그 id, 값) → 어댑터 style_params 이름과 값. 정지 이미지 합성기가 소비하는 값과 이름만 둔다.
CONTROL_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("sync.transition", "C10.cut"): ("transition", "cut"),
    ("sync.transition", "C10.dissolve"): ("transition", "fade"),
    ("catalog", "C10.cut"): ("transition", "cut"),
    ("catalog", "C10.dissolve"): ("transition", "fade"),
    ("cut.tempo", "fast"): ("transition", "cut"),
    ("cut.tempo", "slow"): ("transition", "fade"),
    ("camera.moves", "C06.dolly"): ("animation", "zoom_in"),
    ("camera.moves", "C06.tracking"): ("animation", "pan"),
    ("camera.moves", "C06.crane"): ("animation", "pan_down"),
    ("catalog", "C06.dolly"): ("animation", "zoom_in"),
    ("catalog", "C06.tracking"): ("animation", "pan"),
    ("catalog", "C06.pan"): ("animation", "pan"),
    ("catalog", "C06.static"): ("animation", "still"),
    ("diagram.motion", "static"): ("animation", "still"),
    ("lyrics.display", "C10.subtitles"): ("caption_style", "bottom_card"),
    ("lyrics.display", "C10.kinetic_typography"): ("caption_style", "center_highlight"),
    ("catalog", "C10.subtitles"): ("caption_style", "bottom_card"),
    ("catalog", "C10.kinetic_typography"): ("caption_style", "center_highlight"),
}
# 값이 어떤 능력을 요구하는지. (설정, 값 또는 "*") → 능력 묶음(하나라도 충족하면 된다).
NEEDS: dict[tuple[str, str], list[str]] = {
    ("character.lipsync", "approx"): ["anim.lipsync"],
    ("character.lipsync", "precise"): ["anim.lipsync"],
    ("character.expression", "exaggerated"): ["anim.expression"],
    ("music.source", "generate"): ["audio.music_generate"],
    ("sync.cut_on", "beat"): ["edit.beat_cut"],
    ("sync.cut_on", "bar"): ["edit.beat_cut"],
    ("sync.motion_source", "generated_video"): ["gen.video_i2v", "gen.video_t2v"],
    ("sync.motion_source", "animated_3d"): ["render.three_realtime", "render.blender_offline", "render.3d_preset"],
    ("vfx.types", "*"): ["vfx.particles", "vfx.composite"],
    ("drawing.shading", "painterly"): ["render.painterly_shader"],
    ("drawing.frame_rate", "on_ones"): ["render.frame_animation"],
    ("consistency.character", "strict"): ["qa.continuity"],
    ("diagram.type", "chart"): ["render.dataviz"],
    ("camera.moves", "C06.orbit"): ["anim.camera_virtual"],
    ("camera.moves", "C06.aerial"): ["anim.camera_virtual"],
    ("camera.moves", "C06.handheld"): ["anim.camera_virtual"],
    ("light.key", "C05.volumetric_light"): ["vfx.volumetric"],
    ("sync.transition", "C10.speed_ramp"): ["edit.timeline"],
    ("sync.transition", "C10.match_cut"): ["edit.timeline"],
    ("action.speed_ramp", "yes"): ["edit.timeline"],
}
DIRECTIVES: dict[tuple[str, str], str] = {
    ("drawing.shading", "cel"): "셀 셰이딩: 2~3단계 단색 음영, 그라데이션 없이 칠한다",
    ("drawing.shading", "soft"): "부드러운 음영: 완만한 그라데이션으로 입체감을 낸다",
    ("drawing.shading", "painterly"): "회화풍 음영: 붓결이 보이는 채색",
    ("drawing.line", "thin"): "가는 윤곽선으로 섬세하게 그린다",
    ("drawing.line", "bold"): "굵은 윤곽선으로 실루엣을 또렷하게 그린다",
    ("drawing.line", "sketchy"): "스케치 선으로 손맛을 남긴다",
    ("drawing.frame_rate", "on_ones"): "1코마(매 프레임 새 그림) 부드러운 동작",
    ("drawing.frame_rate", "on_twos"): "2코마(2프레임 홀드) 애니메이션 타이밍",
    ("drawing.frame_rate", "on_threes"): "3코마(3프레임 홀드) 제한 애니메이션",
    ("cut.tempo", "slow"): "컷마다 길게 머물고 호흡을 길게 둔다",
    ("cut.tempo", "standard"): "표준 컷 리듬",
    ("cut.tempo", "fast"): "짧은 컷을 빠르게 이어 속도감을 만든다",
    ("cut.tempo", "slow_build_burst"): "앞부분은 느린 빌드업, 뒷부분은 짧은 폭발 컷",
    ("cut.impact_frames", "yes"): "충격 순간에 1~2프레임 임팩트 프레임을 넣는다",
    ("cut.impact_frames", "no"): "임팩트 프레임 없이 연속 동작으로 보여 준다",
    ("character.expression", "exaggerated"): "표정을 과장해 감정을 크게 보여 준다",
    ("character.expression", "subtle"): "표정을 절제해 미묘한 감정을 보여 준다",
    ("horror.style", "dread"): "서서히 조여 오는 불안감(dread)",
    ("horror.style", "jump_scare"): "갑작스러운 점프 스케어 컷",
    ("horror.style", "psychological"): "심리적 공포: 보이지 않는 것으로 압박",
    ("horror.style", "body_horror"): "신체 변형 공포",
    ("horror.reveal", "hidden"): "위협을 끝까지 보여 주지 않는다",
    ("horror.reveal", "partial"): "위협을 일부만 드러낸다",
    ("horror.reveal", "full"): "위협을 클라이맥스에서 완전히 드러낸다",
    ("horror.gore", "none"): "유혈 없음",
    ("horror.gore", "mild"): "약한 유혈 표현",
    ("horror.gore", "strong"): "강한 유혈 표현",
    ("action.speed_ramp", "yes"): "타격 직전·직후에 속도 변화(스피드 램프)",
    ("explain.audience", "beginner"): "전문 용어는 처음 나올 때 풀어 쓰고 장면당 개념 하나만 다룬다",
    ("explain.audience", "intermediate"): "기본 용어는 설명 없이 쓰고 동작 원리 중심으로 푼다",
    ("explain.audience", "expert"): "용어 설명을 줄이고 세부 동작과 한계를 다룬다",
    ("diagram.type", "flow"): "흐름도: 데이터가 이동하는 방향을 화살표로 보여 준다",
    ("diagram.type", "sequence"): "시퀀스도: 시간 순서대로 주체 간 호출을 보여 준다",
    ("diagram.type", "architecture"): "구조도: 구성 요소와 연결 관계를 보여 준다",
    ("diagram.type", "chart"): "차트: 수치 비교를 그래프로 보여 준다",
    ("diagram.motion", "step_reveal"): "단계마다 요소를 하나씩 드러낸다",
    ("diagram.motion", "continuous_flow"): "요소가 끊김 없이 이어서 흐른다",
    ("diagram.motion", "static"): "정지 다이어그램으로 보여 준다",
    ("code.show", "snippet"): "핵심 코드 일부만 화면에 보여 준다",
    ("code.show", "full"): "코드 전체를 화면에 보여 준다",
    ("code.show", "none"): "코드를 화면에 넣지 않는다",
    ("voice.narration", "ko"): "한국어 내레이션을 쓴다",
    ("voice.narration", "none"): "내레이션 없이 화면과 자막으로 전달한다",
    ("voice.subtitles", "on"): "자막을 켠다",
    ("voice.subtitles", "off"): "자막을 끈다",
    ("music.source", "upload"): "사용자가 올린 음악을 쓴다",
    ("music.source", "generate"): "음악을 생성한다",
    ("lyrics.has", "lyrics"): "가사가 있는 곡: 가사를 화면 정보로 쓴다",
    ("lyrics.has", "instrumental"): "연주곡: 가사 없이 영상으로 전달한다",
    ("sync.cut_on", "beat"): "비트마다 컷을 전환한다",
    ("sync.cut_on", "bar"): "마디마다 컷을 전환한다",
    ("sync.cut_on", "section"): "곡의 구간(절·후렴)이 바뀔 때 컷을 전환한다",
    ("consistency.character", "strict"): "캐릭터 외형을 장면 전체에서 엄격히 유지한다",
    ("consistency.character", "loose"): "캐릭터 외형은 느슨하게 유지한다",
    ("world.tech_design", "mecha"): "기계적인 메카 디자인",
    ("world.tech_design", "organic"): "유기적인 생체 디자인",
    ("world.tech_design", "retro"): "레트로 퓨처 디자인",
}
TONE_DIRECTIVES: dict[str, dict[str, str]] = {
    "tone.horror": {
        "lighting": "저조도 단일 광원과 깊은 그림자",
        "editing": "느린 빌드업 뒤 짧은 충격 컷",
        "audio": "저주파 드론과 갑작스러운 정적",
        "camera": "느린 푸시인과 고정 시점으로 불안을 쌓는다",
    },
    "tone.action": {
        "editing": "짧은 컷과 임팩트 프레임으로 속도감을 만든다",
        "camera": "추적·핸드헬드로 움직임을 따라간다",
        "motion": "큰 동작과 빠른 가속·정지 대비",
        "audio": "리듬이 강한 타악 위주의 음악",
    },
    "tone.emotional": {
        "lighting": "따뜻한 확산광과 낮은 대비",
        "editing": "느린 디졸브와 긴 호흡",
        "audio": "서정적인 어쿠스틱·건반 위주의 음악",
        "camera": "완만한 이동과 클로즈업으로 감정을 담는다",
    },
    "tone.comedy": {
        "editing": "타이밍을 살린 컷과 리액션 샷",
        "visual": "표정과 동작을 과장한다",
        "audio": "경쾌한 효과음",
    },
    "tone.epic": {
        "camera": "와이드·크레인·항공 샷으로 규모를 보여 준다",
        "lighting": "역광과 볼류메트릭 빛",
        "audio": "오케스트라 크레센도",
    },
    "tone.calm": {
        "editing": "긴 호흡과 디졸브",
        "audio": "잔잔한 앰비언트",
        "camera": "고정에 가까운 완만한 이동",
    },
}
STYLE_FACET_CATEGORY = {
    "visual_identity": "visual",
    "character_proportions": "visual",
    "materials": "visual",
    "environment_rules": "visual",
    "lighting": "lighting",
    "camera_language": "camera",
    "sound_identity": "audio",
    "motion_language": "motion",
}
# 사용자의 필수 조건이 어느 경로로도 이어지지 않으면 안 되는 카탈로그. 이 계열은 레시피·Style DNA·렌더 제어로만 실현된다.
REALIZED_BY_PATH = ("C01", "C02", "C03", "C08", "C10")


def _origin_of(spec: IntentSpecV2, condition_id: str) -> str | None:
    found = spec.condition(condition_id)
    return found.origin if found else None


def _directive(field_id: str, value: str, labels: dict[str, str]) -> str:
    text = DIRECTIVES.get((field_id, value))
    if text:
        return text
    field = FIELDS[field_id]
    return f"{field.label}: {labels.get(value, value)}"


def _scene_engines(plan: dict[str, Any], registry: Registry) -> list[Any]:
    """계획이 쓰는 엔진 어댑터. 어댑터를 못 찾으면 빈 목록(실행 가능 판정을 하지 않는다)."""
    try:
        from ttakkari.creative.v2 import engines

        if plan.get("director"):
            ids = list(dict.fromkeys(s["selected_engine"] for s in plan["scenes"] if s.get("selected_engine")))
            found = [engines.adapter_by_id(i) for i in ids]
            return [a for a in found if a is not None]
        providers = {
            n["best"]["entry_id"]
            for n in plan["match"]["needs"]
            if n["priority"] == "must" and n.get("best")
        }
        adapter = engines.adapter_for(providers)
        return [adapter] if adapter is not None else []
    except Exception:  # noqa: BLE001 - 어댑터 탐색 실패가 계획 생성을 막으면 안 된다
        return []


def _capability_state(registry: Registry, options: list[str]) -> tuple[str, str]:
    """(상태, 사유). 상태: runtime | lab | missing."""
    group = registry._group(list(options), True)
    best = group.best
    if best is None or group.level in ("missing", "declared", "installed"):
        why = "이 능력을 제공하는 항목이 없다" if best is None or group.level in ("missing", "declared") else (
            f"설치만 확인됐고 실행 기록이 없다({best.entry_id})"
        )
        return "missing", f"{'/'.join(options)}: {why}"
    if best.integration != "creative_runtime":
        return "lab", f"{'/'.join(options)}: 제작 런타임에 연결되지 않았다({best.entry_id}, {best.integration})"
    return "runtime", ""


def _record(
    *, did: str, category: str, source: str, origin: str, directive: str, field_id: str | None = None,
    values: list[str] | None = None, labels: list[str] | None = None, needs_confirmation: bool = False,
    triggered_by: list[str] | None = None, reason: str = "", alternatives: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "id": did,
        "category": category,
        "category_label": CATEGORIES[category],
        "source": source,
        "origin": origin,
        "field_id": field_id,
        "values": values or [],
        "labels": labels or [],
        "directive": directive,
        "needs_confirmation": needs_confirmation,
        "triggered_by": triggered_by or [],
        "reason": reason,
        "alternatives": alternatives or [],
        "status": "plan_only",
        "status_reason": "",
        "consumer": None,
        "control": None,
    }


def _collect(spec: IntentSpecV2, layout, plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    decisions: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    active: dict[str, list[str]] = {}
    for group in layout.groups:
        for fid in group.fields:
            active.setdefault(fid, []).append(group.id)
    answered = {a.field_id: a for a in layout.answered}
    questions = {q.field_id: q for q in layout.questions}
    seen_values: set[str] = set()

    def add_field(fid: str, values: list[str], origin: str, source: str, *, confirm: bool, reason: str,
                  alternatives: list[dict[str, str]], triggered_by: list[str]) -> None:
        field = FIELDS[fid]
        names = {o.id: o.label for o in field.options}
        category = FIELD_CATEGORY.get(fid) or GROUP_CATEGORY.get(field.group)
        if category is None:
            return
        directives = [_directive(fid, v, names) for v in values]
        decisions.append(
            _record(
                did=f"d.{fid}",
                category=category,
                source=source,
                origin=origin,
                directive="; ".join(directives),
                field_id=fid,
                values=values,
                labels=[names.get(v, v) for v in values],
                needs_confirmation=confirm,
                triggered_by=triggered_by,
                reason=reason,
                alternatives=alternatives,
            )
        )
        seen_values.update(v for v in values if v in ENTRIES or v in TONES)

    for fid, item in answered.items():
        if fid in ("output.duration", "output.aspect", "content.type") or fid not in FIELDS:
            continue
        if fid not in active or not item.values:
            continue  # 비어 있는 답은 사용자가 해제한 설정이다. 결정을 만들지 않는다.
        # 조건에서 읽은 확정 값(선택지가 곧 카탈로그 조건)은 출처를 조건에서 가져온다.
        add_field(fid, item.values, item.origin, "answer", confirm=False,
                  reason="사용자가 정한 값", alternatives=[], triggered_by=[])
    for fid, q in questions.items():
        if fid in ("output.duration", "output.aspect", "content.type") or q.recommended is None:
            continue
        pending = [o.id for o in q.options if o.id in {c.id for c in spec.conditions}]
        if q.mode == "confirm" and pending:
            origins = sorted(
                {_origin_of(spec, p) or "ai_inferred" for p in pending}, key=lambda o: ORIGIN_RANK.get(o, 9)
            )
            add_field(fid, pending, origins[0], "condition", confirm=True,
                      reason=q.reason, alternatives=[a.model_dump() for a in q.alternatives],
                      triggered_by=q.triggered_by)
        else:
            add_field(fid, [q.recommended], "rule_recommended", "recommended", confirm=True,
                      reason=q.reason, alternatives=[a.model_dump() for a in q.alternatives],
                      triggered_by=q.triggered_by)
    for answer in spec.answers:
        if answer.values and answer.field_id not in active and answer.field_id in FIELDS:
            skipped.append(
                {"id": f"answer.{answer.field_id}",
                 "reason": "현재 조건 조합에서 이 설정이 숨겨져 있어 결정으로 옮기지 않았습니다. 조건이 다시 켜지면 답변이 되살아납니다."}
            )
    for cond in sorted(spec.conditions, key=lambda c: c.id):
        if cond.id in seen_values:
            continue
        if cond.dimension == "tone":
            for category, text in TONE_DIRECTIVES[cond.id].items():
                decisions.append(
                    _record(
                        did=f"d.{cond.id}.{category}", category=category, source="tone", origin=cond.origin,
                        directive=f"{label_of(cond.id)} 톤: {text}", values=[cond.id], labels=[label_of(cond.id)],
                        needs_confirmation=cond.origin != "user_confirmed", triggered_by=[cond.id],
                    )
                )
            continue
        category = CATALOG_CATEGORY.get(cond.dimension)
        if category is None or cond.dimension in ("C01",):
            continue
        decisions.append(
            _record(
                did=f"d.{cond.id}", category=category, source="condition", origin=cond.origin,
                directive=f"{ENTRIES[cond.id].name} 사용", values=[cond.id], labels=[ENTRIES[cond.id].name],
                needs_confirmation=cond.origin != "user_confirmed", triggered_by=[cond.id],
            )
        )
    style = plan.get("style") or {}
    if style.get("status") == "resolved":
        dna = style.get("dna") or {}
        for entry in style.get("provenance") or []:
            facet, category = entry.get("field"), STYLE_FACET_CATEGORY.get(entry.get("field", ""))
            if category is None or entry.get("via") != "claim":
                continue
            text = (dna.get(facet) or {}).get("text")
            if not text:
                continue
            decisions.append(
                _record(
                    did=f"d.style.{facet}", category=category, source="style_dna", origin="style_dna",
                    directive=text, values=list(entry.get("sources", [])), labels=list(entry.get("sources", [])),
                    triggered_by=[], reason="Style DNA가 정한 값",
                )
            )
    return decisions, skipped


def _classify(
    decisions: list[dict[str, Any]], plan: dict[str, Any], registry: Registry, adapters: list[Any],
) -> dict[str, dict[str, Any]]:
    """실행 상태와 렌더 제어를 정한다. 같은 제어를 두 결정이 원하면 출처 순위가 앞선 쪽이 얻는다."""
    controls: dict[str, dict[str, Any]] = {}
    ordered = sorted(decisions, key=lambda d: (ORIGIN_RANK.get(d["origin"], 9), d["id"]))
    for d in ordered:
        reasons: list[str] = []
        capability_blocked = False
        for value in d["values"] or [""]:
            key_field = d["field_id"] or ("catalog" if value in ENTRIES else "")
            need = NEEDS.get((key_field, value)) or NEEDS.get((key_field, "*"))
            if need:
                state, why = _capability_state(registry, need)
                if state == "missing":
                    capability_blocked = True
                    reasons.append(why)
                elif state == "lab":
                    reasons.append(f"실행기 미연결: {why}")
        if capability_blocked:
            d["status"] = "unsupported"
            d["status_reason"] = "; ".join(reasons) + ". 대안: 계획과 AI 감독 입력에는 남기지만 제작에는 반영되지 않습니다."
            continue
        mapped = []
        for value in d["values"]:
            key_field = d["field_id"] or "catalog"
            hit = CONTROL_MAP.get((key_field, value))
            if hit:
                mapped.append((value, hit))
        if not mapped:
            d["status"] = "plan_only"
            d["status_reason"] = "; ".join(reasons) or "실행기가 소비하는 제어가 없어 계획·장면 기록과 AI 감독 입력으로만 반영됩니다."
            continue
        value, (name, ctrl_value) = mapped[0]
        consumers = [a.id for a in adapters if ctrl_value in a.style_params.get(name, ())]
        if d["origin"] not in USER_ORIGINS:
            d["status"] = "plan_only"
            d["status_reason"] = "추천·해석 값은 사용자가 확정해야 렌더에 반영됩니다."
        elif not consumers:
            d["status"] = "plan_only"
            d["status_reason"] = f"선택된 엔진이 {name}={ctrl_value}를 소비하지 않습니다."
        elif name in controls:
            d["status"] = "plan_only"
            d["status_reason"] = f"같은 제어({name})를 출처 순위가 앞선 결정({controls[name]['decision']})이 먼저 얻었습니다."
        else:
            d["status"] = "executable"
            d["consumer"] = consumers
            d["control"] = {"name": name, "value": ctrl_value}
            d["status_reason"] = f"렌더 제어 {name}={ctrl_value}로 {', '.join(consumers)}에 전달됩니다."
            controls[name] = {"value": ctrl_value, "decision": d["id"], "origin": d["origin"]}
    return controls


def _length(plan: dict[str, Any]) -> dict[str, Any]:
    duration = plan["duration"]
    request = duration["request"]
    planned = duration["planned_seconds"]
    wanted = request.get("seconds")
    status = duration["status"]
    if status == "conflict":
        treatment = "conflict"
    elif wanted and planned < wanted * 0.85:
        treatment = "capacity_short"
    elif wanted and planned > wanted * 1.15:
        treatment = "condensation_needed"
    else:
        treatment = "fit"
    message = {
        "fit": "정보량이 요청한 길이에 맞습니다.",
        "conflict": "필수 정보가 요청한 길이에 들어가지 않습니다. 대안 중 하나를 고르거나 장면을 직접 맞춰야 합니다.",
        "capacity_short": f"요청한 {wanted:g}초를 채울 정보가 모자라 {planned:g}초로 계획했습니다. 내용을 더하거나 길이를 줄이는 결정이 필요합니다."
        if wanted else "",
        "condensation_needed": f"정보량이 요청한 {wanted:g}초보다 많습니다. 비필수 항목을 줄이거나 발화를 빠르게 해야 합니다."
        if wanted else "",
    }[treatment]
    needed = duration.get("needed") or {}
    return {
        "requested": {"mode": request.get("mode"), "seconds": wanted},
        "planned_seconds": planned,
        "status": status,
        "deviation_level": duration.get("deviation_level"),
        "basis": plan.get("basis"),
        "beats": len(plan["scenes"]),
        "capacity": {
            "min_seconds": needed.get("min_seconds"),
            "ideal_seconds": needed.get("ideal_seconds"),
            "max_seconds": needed.get("max_seconds"),
        },
        "treatment": treatment,
        "message": message,
        "alternatives": [
            {"kind": a["kind"], "label": a["label"], "seconds": a["seconds"], "requires_user_approval": a["requires_user_approval"]}
            for a in duration.get("alternatives", [])
        ],
    }


def _information(scene: dict[str, Any]) -> dict[str, Any]:
    ideal = scene.get("ideal") or scene["seconds"]
    ratio = round(scene["seconds"] / ideal, 3) if ideal else 1.0
    if ratio >= 1.15:
        treatment = "extended"
    elif ratio >= 0.95:
        treatment = "full"
    elif ratio >= 0.6:
        treatment = "condensed"
    else:
        treatment = "minimal"
    return {
        "weight": scene.get("info_weight"),
        "essential": scene.get("essential"),
        "seconds": scene["seconds"],
        "ideal_seconds": ideal,
        "ratio": ratio,
        "treatment": treatment,
        "drop_candidate": treatment in ("condensed", "minimal") and not scene.get("essential", True),
    }


def engine_fit(plan: dict[str, Any], adapters: list[Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    """사용자가 정한 화면비·길이가 선택된 엔진의 규격과 부딪치는지. 렌더 직전(dispatch)과 같은 판정을 계획 단계에서 보인다.

    status: ok(규격 안) | blocked(규격 밖, 사유 기록) | no_engine(필수 능력을 맡는 제작 엔진이 없어 규격을 비교하지 못함).
    """
    from ttakkari.creative.v2 import engines

    by_scene = {s["scene_id"]: s for s in plan["scenes"]}
    rows: list[dict[str, Any]] = []
    for segment in segments:
        scenes = [by_scene[i] for i in segment["scene_ids"]]
        engine_id = scenes[0].get("selected_engine")
        adapter = engines.adapter_by_id(engine_id) if engine_id else (adapters[0] if adapters else None)
        if adapter is None:
            continue
        if plan.get("director"):
            minimum = 1 if adapter.id == engines.V1SlideshowAdapter.id else None
        else:
            minimum = adapter.limits.min_scenes if len(segments) == 1 else 1
        over = engines.limit_violations(adapter, plan["aspect"], scenes, segment["seconds"], minimum_scenes=minimum)
        # 어댑터가 소비하지 못하는 입력(프리셋 엔진에 들어온 카메라·모션 조건, 다중 샷 등)도 렌더 전에 이유로 보인다.
        validate = getattr(adapter, "validate_spec", None)
        if callable(validate):
            over += validate(scenes)
        rows.append({"segment_id": segment["id"], "engine": adapter.id, "scene_ids": segment["scene_ids"],
                     "seconds": segment["seconds"], "ok": not over, "reasons": over})
    reasons = [f"{r['segment_id']}: {why}" for r in rows for why in r["reasons"]]
    used = list(dict.fromkeys(r["engine"] for r in rows))
    status = "blocked" if reasons else "ok"
    if not rows:
        status = "no_engine"
        supporting = [a.id for a in engines.load_adapters() if plan["aspect"] in a.limits.aspects]
        reasons = [
            "필수 능력을 맡는 제작 엔진이 제작 런타임에 없어 화면비·길이를 엔진 규격과 비교하지 못했습니다.",
            *(
                []
                if supporting
                else [f"화면비 {plan['aspect']}을 지원하는 제작 엔진이 하나도 없습니다."]
            ),
        ]
    return {
        "status": status,
        "reasons": reasons,
        "segments": rows,
        "engines": used,
        "mixed_engines": len(used) > 1,
    }


def _required_coverage(spec: IntentSpecV2, plan: dict[str, Any], registry: Registry,
                       decisions: list[dict[str, Any]], scaffolds: frozenset[str]) -> list[dict[str, Any]]:
    """필수 조건이 계획의 어느 경로(레시피·Style DNA·렌더 제어·장면 기록)로 이어지는지. 어디에도 안 이어지면 unrealized."""
    match = plan["match"]
    chosen = [r for r in [match.get("primary_recipe"), *match.get("auxiliary_recipes", [])] if r]
    fits = {e for r in chosen if r in registry.recipes for e in registry.recipes[r].fits}
    kept = {s["entry_id"] for s in match["selections"]}
    style_ids = {
        item["id"]
        for item in (plan.get("intent_projection") or {}).get("items", [])
        if any(a.startswith("style_dna:") for a in item.get("applied_to", []))
    }
    controlled = {v for d in decisions if d["status"] == "executable" for v in d["values"]}
    rows = []
    for cond in sorted(spec.conditions, key=lambda c: c.id):
        if cond.strength != "required":
            continue
        dropped = cond.id in ENTRIES and cond.id not in kept
        paths: list[str] = []
        if not dropped:
            if cond.id in fits:
                paths.append("recipe")
            if cond.id in style_ids:
                paths.append("style_dna")
            if cond.id in controlled:
                paths.append("render_control")
            if cond.dimension == "tone" or cond.dimension not in REALIZED_BY_PATH:
                paths.append("scene_record")
            if cond.id in scaffolds and plan.get("basis") == "template_scaffold":
                paths.append("scaffold")
        rows.append({
            "id": cond.id, "label": label_of(cond.id), "origin": cond.origin, "realized_by": paths,
            "status": "realized" if paths else "unrealized",
            "reason": "" if paths else (
                "카탈로그 충돌로 매칭에서 제외됐습니다." if dropped
                else f"선택된 레시피({', '.join(chosen) or '없음'})·Style DNA·렌더 제어 어디에도 이 필수 조건이 연결되지 않았습니다."
            ),
        })
    return rows


def _per_scene(plan: dict[str, Any], decisions: list[dict[str, Any]]) -> None:
    scenes = plan["scenes"]
    count = len(scenes)
    tempo = next((d for d in decisions if d["field_id"] == "cut.tempo"), None)
    for index, scene in enumerate(scenes):
        by_category: dict[str, list[str]] = {}
        for d in decisions:
            if d["status"] == "unsupported":
                continue
            prefix = "[추천] " if d["origin"] == "rule_recommended" else ""
            by_category.setdefault(d["category"], []).append(prefix + d["directive"])
        if tempo and tempo["values"] == ["slow_build_burst"]:
            late = (index + 0.5) / count >= 0.6
            by_category.setdefault("editing", []).append(
                "이 장면은 폭발 구간: 짧은 컷과 임팩트로 몰아친다" if late else "이 장면은 빌드업 구간: 길게 머물며 긴장을 쌓는다"
            )
        scene["production"] = {"decision_ids": [d["id"] for d in decisions if d["status"] != "unsupported"],
                               "by_category": by_category}
        scene["information"] = _information(scene)


def apply(plan: dict[str, Any], registry: Registry, *, segments: list[dict[str, Any]] | None = None,
          scaffolds: frozenset[str] = frozenset()) -> None:
    """계획에 production_decisions를 쓰고 장면에 production·information을 붙인다. 의도 v2가 없는 계획은 건드리지 않는다."""
    if not plan.get("intent_v2"):
        return
    spec = IntentSpecV2.model_validate(plan["intent_v2"])
    known: dict[str, tuple[str, str]] = {}
    if spec.duration is not None:
        known["output.duration"] = (spec.duration.mode, spec.duration.origin)
    if spec.aspect is not None:
        known["output.aspect"] = (spec.aspect.value, spec.aspect.origin)
    layout = resolve_layout(spec.conditions, spec.answers, known)
    decisions, skipped = _collect(spec, layout, plan)
    adapters = _scene_engines(plan, registry)
    controls = _classify(decisions, plan, registry, adapters)
    coverage = _required_coverage(spec, plan, registry, decisions, scaffolds)
    _per_scene(plan, decisions)
    _update_projection(plan, spec, decisions, skipped)
    counts = {s: sum(1 for d in decisions if d["status"] == s) for s in ("executable", "plan_only", "unsupported")}
    plan["production_decisions"] = {
        "schema": SCHEMA,
        "intent_revision": spec.revision,
        "decisions": decisions,
        "render_controls": controls,
        "counts": counts,
        "required_conditions": coverage,
        "length": _length(plan),
        "not_applied": skipped,
        "status_legend": {
            "executable": "선택된 엔진이 소비하는 렌더 제어로 전달됩니다.",
            "plan_only": "계획·장면 기록과 AI 감독 입력에만 반영되고 실행기는 소비하지 않습니다.",
            "unsupported": "필요한 능력이 없거나 검증되지 않아 제작에 반영되지 않습니다.",
        },
    }
    if segments is not None:
        plan["production_decisions"]["engine_fit"] = engine_fit(plan, adapters, segments)


_EXECUTION_RANK = {"executable": 0, "plan_only": 1, "unsupported": 2}


def _update_projection(plan: dict[str, Any], spec: IntentSpecV2, decisions: list[dict[str, Any]],
                       skipped: list[dict[str, str]]) -> None:
    """intent_projection에 답변·조건이 어떤 결정으로 이어졌고 실행 상태가 무엇인지 적는다. 소비된 답변은 not_applied에서 뺀다."""
    projection = plan.get("intent_projection")
    if not projection:
        return
    by_field = {d["field_id"]: d for d in decisions if d["field_id"]}
    by_value: dict[str, list[dict[str, Any]]] = {}
    for d in decisions:
        for v in d["values"]:
            by_value.setdefault(v, []).append(d)
    generic = ("output.duration", "output.aspect", "content.type")
    projection["not_applied"] = [
        n for n in projection["not_applied"]
        if not (n["id"].startswith("answer.") and (n["id"][7:] in by_field or n["id"][7:] in generic))
    ]
    projection["items"] = [i for i in projection["items"] if i["kind"] != "answer"]
    for item in projection["items"]:
        if item["kind"] == "condition" and item["id"] in by_value:
            states = [d["status"] for d in by_value[item["id"]]]
            item["execution"] = min(states, key=_EXECUTION_RANK.get)
            item["applied_to"] = [*item["applied_to"], *(f"production_decision:{d['id']}" for d in by_value[item["id"]])]
    for answer in spec.answers:
        d = by_field.get(answer.field_id)
        if d is None:
            continue
        projection["items"].append({
            "kind": "answer", "id": f"answer.{answer.field_id}", "origin": "user_confirmed",
            "value": answer.values, "applied_to": [f"production_decision:{d['id']}"], "execution": d["status"],
        })
    known = {n["id"] for n in projection["not_applied"]}
    for d in decisions:
        if d["status"] == "unsupported":
            entry = {"id": d["id"], "reason": f"지원 불가({d['category_label']}): {d['status_reason']}"}
            if entry["id"] not in known:
                projection["not_applied"].append(entry)
    projection["not_applied"] += [s for s in skipped if s["id"] not in {n["id"] for n in projection["not_applied"]}]


def blockers(plan: dict[str, Any]) -> list[str]:
    """승인·렌더 전에 사용자가 결정해야 하는 이유. 필수 조건이 어디에도 연결되지 않았거나, 사용자가 말한 길이를 정보량이 크게 못 채운다."""
    decisions = plan.get("production_decisions") or {}
    reasons = [
        f"필수 조건 {row['label']}({row['id']}): {row['reason']}"
        for row in decisions.get("required_conditions", [])
        if row["status"] == "unrealized"
    ]
    length = decisions.get("length") or {}
    spec = plan.get("intent_v2") or {}
    origin = ((spec.get("duration") or {}).get("origin")) if spec else None
    if (
        length.get("deviation_level") == "excessive"
        and length.get("treatment") in ("capacity_short", "condensation_needed")
        and plan.get("duration_check") is None
        and origin in USER_ORIGINS
    ):
        reasons.append(length["message"] + " 대안: " + ", ".join(a["label"] for a in length["alternatives"]))
    return reasons


def render_controls(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return (plan.get("production_decisions") or {}).get("render_controls", {})
