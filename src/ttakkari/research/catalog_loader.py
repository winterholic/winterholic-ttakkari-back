"""Seed catalog loader for Research Lab v2. Idempotent upsert of taxonomy terms, methods, and relations."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert

from ttakkari.models import utcnow
from ttakkari.research.models import Method, MethodRelation, TaxonomyTerm

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).parent / "catalog_seed"

_LAST_LOADED_HASH: str | None = None

AXIS_SINGULAR_MAP = {
    "targets": "target",
    "layers": "layer",
    "goals": "goal",
    "interventions": "intervention",
    "research_methods": "research_method",
    "categories": "category",
}


def compute_seed_hash() -> str:
    h = hashlib.sha256()
    if not SEED_DIR.exists():
        return ""
    for path in sorted(SEED_DIR.glob("*.json")):
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


async def ensure_catalog_loaded(db, force: bool = False) -> dict[str, int]:
    """Load catalog seeds idempotently if not yet loaded or if seed files changed."""
    global _LAST_LOADED_HASH
    current_hash = compute_seed_hash()
    if not current_hash:
        return {"terms": 0, "methods": 0, "relations": 0}

    if not force and _LAST_LOADED_HASH == current_hash:
        # Check if DB has at least one system method
        has_methods = (
            await db.execute(select(Method.id).where(Method.scope == "system").limit(1))
        ).scalar_one_or_none()
        if has_methods:
            return {"terms": 0, "methods": 0, "relations": 0}

    stats = await load_catalog_seeds(db)
    _LAST_LOADED_HASH = current_hash
    return stats


async def load_catalog_seeds(db) -> dict[str, int]:
    """Parse catalog_seed/*.json and upsert into database."""
    if not SEED_DIR.exists():
        logger.warning("Catalog seed directory does not exist: %s", SEED_DIR)
        return {"terms": 0, "methods": 0, "relations": 0}

    terms_count = 0
    methods_count = 0
    relations_count = 0

    # 1. Load taxonomy.json
    tax_path = SEED_DIR / "taxonomy.json"
    if tax_path.exists():
        try:
            tax_data = json.loads(tax_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"Failed to parse taxonomy seed {tax_path}: {e}") from e

        order_idx = 0
        for axis_key, items in tax_data.items():
            axis = AXIS_SINGULAR_MAP.get(axis_key, axis_key)
            if not isinstance(items, list):
                continue
            for item in items:
                order_idx += 10
                code = item.get("id") or item.get("code")
                if not code:
                    continue
                label_ko = item.get("label_ko") or item.get("label", code)
                label = item.get("label") or item.get("name") or label_ko
                description = item.get("description", "")
                parent_code = item.get("parent_code")

                # Upsert taxonomy term
                stmt = (
                    insert(TaxonomyTerm)
                    .values(
                        id=uuid.uuid4(),
                        axis=axis,
                        code=code,
                        label=label,
                        label_ko=label_ko,
                        description=description,
                        parent_code=parent_code,
                        sort_order=order_idx,
                        is_system=True,
                    )
                    .on_conflict_do_update(
                        constraint="uq_taxonomy_axis_code",
                        set_={
                            "label": label,
                            "label_ko": label_ko,
                            "description": description,
                            "parent_code": parent_code,
                            "sort_order": order_idx,
                            "is_system": True,
                        },
                    )
                )
                await db.execute(stmt)
                terms_count += 1

    # 2. Load method seed files
    method_files = sorted(
        [p for p in SEED_DIR.glob("*.json") if p.name != "taxonomy.json"]
    )
    raw_methods: list[dict[str, Any]] = []
    for m_path in method_files:
        try:
            m_data = json.loads(m_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"Failed to parse method seed {m_path}: {e}") from e

        file_methods = m_data.get("methods", [])
        if not isinstance(file_methods, list):
            continue
        raw_methods.extend(file_methods)

    # Slug to method ID map (for relation linking)
    slug_to_id: dict[str, uuid.UUID] = {}

    for m in raw_methods:
        slug = m.get("slug")
        if not slug:
            continue
        name = m.get("name") or slug
        name_ko = m.get("name_ko") or name
        summary = m.get("summary", "")
        principle = m.get("principle", "")
        category = m.get("category", "general")
        targets = m.get("targets", [])
        layers = m.get("layers", [])
        goals = m.get("goals", [])
        interventions = m.get("interventions", [])
        research_methods = m.get("research_methods", [])
        parameters = m.get("parameters", [])
        compatibility = m.get("compatibility", {})
        metrics = m.get("metrics", [])
        expected = m.get("expected_effects", [])
        if isinstance(expected, str):
            expected = [expected]
        side = m.get("side_effects", [])
        if isinstance(side, str):
            side = [side]
        evidence = m.get("evidence", [])
        evidence_status = m.get("evidence_status", "source_reported")
        adapter_hints = m.get("adapter_hints", [])
        tags = m.get("tags", [])
        # deprecated 시드(예: Kokoro)가 로드 후 validated로 둔갑해 readiness가 실행 후보로 취급하지 않도록 시드 값을 그대로 쓴다.
        lifecycle_status = m.get("lifecycle_status", "validated")

        # Check existing system seed method to retain UUID and version
        existing = (
            await db.execute(
                select(Method).where(
                    Method.scope == "system",
                    Method.slug == slug,
                    Method.version == 1,
                )
            )
        ).scalar_one_or_none()

        if existing:
            m_id = existing.id
            existing.name = name
            existing.name_ko = name_ko
            existing.summary = summary
            existing.principle = principle
            existing.category = category
            existing.targets = targets
            existing.layers = layers
            existing.goals = goals
            existing.interventions = interventions
            existing.research_methods = research_methods
            existing.parameters = parameters
            existing.compatibility = compatibility
            existing.metrics = metrics
            existing.expected_effects = expected
            existing.side_effects = side
            existing.evidence = evidence
            existing.evidence_status = evidence_status
            existing.adapter_hints = adapter_hints
            existing.tags = tags
            existing.lifecycle_status = lifecycle_status
            existing.updated_at = utcnow()
        else:
            m_id = uuid.uuid4()
            new_method = Method(
                id=m_id,
                scope="system",
                slug=slug,
                version=1,
                supersedes_id=None,
                origin="system_seed",
                lifecycle_status=lifecycle_status,
                name=name,
                name_ko=name_ko,
                summary=summary,
                principle=principle,
                category=category,
                targets=targets,
                layers=layers,
                goals=goals,
                interventions=interventions,
                research_methods=research_methods,
                parameters=parameters,
                compatibility=compatibility,
                metrics=metrics,
                expected_effects=expected,
                side_effects=side,
                evidence=evidence,
                evidence_status=evidence_status,
                adapter_hints=adapter_hints,
                tags=tags,
                source_study_id=None,
                source_node_id=None,
                promoted_from_run_id=None,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(new_method)
        slug_to_id[slug] = m_id
        methods_count += 1

    await db.flush()

    # 3. Load relations
    for m in raw_methods:
        from_slug = m.get("slug")
        from_id = slug_to_id.get(from_slug)
        if not from_id:
            continue
        relations = m.get("relations", [])
        if not isinstance(relations, list):
            continue
        for rel in relations:
            to_slug = rel.get("target") or rel.get("to_slug")
            if not to_slug:
                continue
            to_id = slug_to_id.get(to_slug)
            if not to_id:
                # Target may not be in seeds yet or target is in another domain
                target_method = (
                    await db.execute(
                        select(Method.id).where(
                            Method.scope == "system", Method.slug == to_slug
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if target_method:
                    to_id = target_method
                    slug_to_id[to_slug] = to_id
                else:
                    continue

            rel_type = rel.get("type") or rel.get("relation_type", "combines_with")
            note = rel.get("note", "")
            conditions = rel.get("conditions", {})

            stmt = (
                insert(MethodRelation)
                .values(
                    id=uuid.uuid4(),
                    scope="system",
                    from_method_id=from_id,
                    to_method_id=to_id,
                    relation_type=rel_type,
                    conditions=conditions,
                    note=note,
                )
                .on_conflict_do_update(
                    constraint="uq_method_relation",
                    set_={"conditions": conditions, "note": note},
                )
            )
            await db.execute(stmt)
            relations_count += 1

    await db.flush()
    logger.info(
        "Loaded catalog seeds: %d terms, %d methods, %d relations",
        terms_count,
        methods_count,
        relations_count,
    )
    return {
        "terms": terms_count,
        "methods": methods_count,
        "relations": relations_count,
    }
