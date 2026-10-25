"""L2 Local Qwen Strict JSON Schema deep extractor.

Invokes the local Qwen/llama-server via the existing backend path to extract
detailed entities, assertions, and relations with strict JSON schema constraints.
Enforces the Qwen single-slot policy and degrades gracefully when unavailable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ttakkari.common.entity_norm import split_bilingual_label
from ttakkari.kb_models import KbPassage
from ttakkari.kb_pipeline.quote_verifier import (
    CandidateAssertion,
    CandidateEntity,
    CandidateRelation,
)

log = logging.getLogger(__name__)

L2_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "concept",
                            "technology",
                            "system",
                            "api",
                            "symbol",
                            "standard",
                            "organization",
                            "person",
                            "document_topic",
                        ],
                    },
                    "domain": {
                        "type": "string",
                        "enum": [
                            "dev",
                            "infra",
                            "ai",
                            "security",
                            "business",
                            "science",
                            "general",
                        ],
                    },
                },
                "required": ["name", "aliases", "kind", "domain"],
            },
        },
        "assertions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object_entity": {"type": ["string", "null"]},
                    "object_value": {"type": "string"},
                    "modality": {
                        "type": "string",
                        "enum": ["fact", "recommendation", "requirement", "deprecated"],
                    },
                    "polarity": {"type": "string", "enum": ["positive", "negative"]},
                    "applies_version": {"type": ["string", "null"]},
                    "evidence_quote": {"type": "string"},
                },
                "required": [
                    "subject",
                    "predicate",
                    "object_entity",
                    "object_value",
                    "modality",
                    "polarity",
                    "applies_version",
                    "evidence_quote",
                ],
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "relation_type": {
                        "type": "string",
                        "enum": [
                            "part_of",
                            "manages",
                            "depends_on",
                            "uses",
                            "implements",
                            "alternative_to",
                            "related_to",
                        ],
                    },
                    "evidence_quote": {"type": "string"},
                },
                "required": ["source", "target", "relation_type", "evidence_quote"],
            },
        },
    },
    "required": ["entities", "assertions", "relations"],
}

L2_SYSTEM_PROMPT = (
    "너는 지식 온톨로지 정밀 추출기다. 주어진 패시지에서 핵심 개념(entities), 사실 주장(assertions), 개념 간 관계(relations)를 JSON으로 추출한다.\n"
    "불변 원칙:\n"
    "1. 모든 assertions 와 relations 의 evidence_quote 는 반드시 주어진 패시지 본문에 글자 그대로 정확하게 존재하는 구절이어야 한다.\n"
    "2. 본문에 없는 인용문은 환각 방지 검증기에서 100% 폐기되므로 절대 지어내지 마라.\n"
    "3. relation_type 은 [part_of, manages, depends_on, uses, implements, alternative_to, related_to] 중 하나여야 한다."
)


@dataclass
class L2ExtractionResult:
    """Output of L2 execution."""

    entities: list[CandidateEntity] = field(default_factory=list)
    assertions: list[CandidateAssertion] = field(default_factory=list)
    relations: list[CandidateRelation] = field(default_factory=list)
    calls_made: int = 0
    degraded: bool = False
    degradation_reason: str | None = None


def check_qwen_availability() -> tuple[bool, str | None]:
    """Check Qwen single-slot policy and desk Qwen server readiness."""
    # 1. Machine-wide Qwen slot busy check
    try:
        from ttakkari.runs.qwen_slot import is_process_lock_busy

        if is_process_lock_busy():
            return False, "qwen_slot_busy"
    except (ImportError, Exception) as exc:  # noqa: BLE001
        log.debug("qwen_slot check skipped: %s", exc)

    # 2. Desk Qwen server readiness
    try:
        from ttakkari.runs.desk_qwen import available, endpoint

        if not available() or endpoint() is None:
            return False, "desk_qwen_unavailable"
    except (ImportError, Exception) as exc:  # noqa: BLE001
        return False, f"desk_qwen_unavailable: {exc}"

    return True, None


async def default_qwen_llm_call(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Production LLM caller reusing backend's existing desk_qwen path."""
    import httpx

    from ttakkari.config import get_settings
    from ttakkari.runs.desk_qwen import _http_client, endpoint

    base = endpoint()
    if not base:
        raise RuntimeError("desk_qwen_endpoint_none")

    s = get_settings()
    messages = [
        {"role": "system", "content": L2_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    payload = {
        "model": s.desk_qwen_model,
        "messages": messages,
        "temperature": 0,
        "seed": 20261025,
        "max_tokens": 1500,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "kb_l2_extraction", "strict": True, "schema": schema},
        },
    }

    async with _http_client(httpx.Timeout(45.0, connect=3.0)) as client:
        r = await client.post(base.rstrip("/") + "/v1/chat/completions", json=payload)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)


async def extract_l2_from_passages(
    passages: list[KbPassage],
    *,
    llm_caller: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    max_calls: int = 8,
    daily_budget_remaining: int | None = None,
    origin_scope: str = "personal",
    visibility: str = "global",
) -> L2ExtractionResult:
    """Run L2 extraction on selected passages with budget and degradation controls."""
    result = L2ExtractionResult()

    if not passages:
        return result

    # Check budget
    if daily_budget_remaining is not None and daily_budget_remaining <= 0:
        result.degraded = True
        result.degradation_reason = "daily_budget_exhausted"
        return result

    # Caller selection: if caller is not injected, verify environment availability
    active_caller = llm_caller
    if active_caller is None:
        ready, reason = check_qwen_availability()
        if not ready:
            result.degraded = True
            result.degradation_reason = reason
            return result
        active_caller = default_qwen_llm_call

    # Limit passages according to budgets
    call_budget = min(max_calls, len(passages))
    if daily_budget_remaining is not None:
        call_budget = min(call_budget, daily_budget_remaining)

    target_passages = passages[:call_budget]

    for p in target_passages:
        body = p.body or ""
        if not body.strip():
            continue

        prompt = (
            f"[제목 / 헤딩]: {p.heading or '없음'}\n"
            f"[절 경로]: {' > '.join(p.section_path) if p.section_path else '없음'}\n"
            f"[패시지 본문]:\n{body}"
        )

        try:
            raw_res = await active_caller(prompt, L2_SCHEMA)
            result.calls_made += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("L2 extraction call failed on passage %s: %s", p.id, exc)
            result.degraded = True
            result.degradation_reason = f"llm_call_error: {exc}"
            continue

        if not isinstance(raw_res, dict):
            continue

        # Parse entities
        for e in raw_res.get("entities", []):
            if not isinstance(e, dict):
                continue
            name = str(e.get("name", "")).strip()
            if not name:
                continue
            canonical, aliases = split_bilingual_label(name)
            for a in e.get("aliases", []):
                if a and a not in aliases:
                    aliases.append(str(a).strip())

            kind = str(e.get("kind", "concept"))
            domain = str(e.get("domain", "general"))

            result.entities.append(
                CandidateEntity(
                    name=canonical,
                    canonical_name=canonical,
                    aliases=aliases,
                    kind=kind,
                    domain=domain,
                    confidence=0.85,
                    origin_scope=origin_scope,
                    visibility=visibility,
                )
            )

        # Parse assertions
        for a in raw_res.get("assertions", []):
            if not isinstance(a, dict):
                continue
            subj = str(a.get("subject", "")).strip()
            pred = str(a.get("predicate", "")).strip()
            val = str(a.get("object_value", "")).strip()
            quote = str(a.get("evidence_quote", "")).strip()

            if not subj or not pred or not val or not quote:
                continue

            canonical_subj, _ = split_bilingual_label(subj)
            obj_ent = a.get("object_entity")
            canonical_obj = split_bilingual_label(str(obj_ent))[0] if obj_ent else None

            result.assertions.append(
                CandidateAssertion(
                    subject_name=canonical_subj,
                    predicate=pred,
                    object_value=val,
                    object_entity_name=canonical_obj,
                    modality=str(a.get("modality", "fact")),
                    polarity=str(a.get("polarity", "positive")),
                    applies_version=a.get("applies_version"),
                    trust_score=0.8,
                    extractor_tier="L2",
                    evidence_quote=quote,
                    passage_id=p.id,
                )
            )

        # Parse relations
        for r in raw_res.get("relations", []):
            if not isinstance(r, dict):
                continue
            src = str(r.get("source", "")).strip()
            tgt = str(r.get("target", "")).strip()
            rtype = str(r.get("relation_type", "related_to"))
            quote = str(r.get("evidence_quote", "")).strip()

            if not src or not tgt or not quote:
                continue

            canonical_src, _ = split_bilingual_label(src)
            canonical_tgt, _ = split_bilingual_label(tgt)

            result.relations.append(
                CandidateRelation(
                    source_name=canonical_src,
                    target_name=canonical_tgt,
                    relation_type=rtype,
                    weight=1.0,
                    status="candidate",
                    evidence_quote=quote,
                    passage_id=p.id,
                )
            )

    return result
