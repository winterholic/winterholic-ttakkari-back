"""접근 행(kb_document_access)이 없는 기존 문서에 접근 행을 채우는 백필.

마이그레이션이 아니라 명시 실행 CLI(`ttakkari kb-backfill-access`)로 둔 이유:
- 분류에 사람의 검토가 필요하다. 접근 행이 없는 문서가 work 인지 personal 인지 DB 만으로는 알 수 없는 경우가 있다.
- 기본 실행은 dry-run 이고 쓰기는 `--apply` 와 DB 이름 확인(`--confirm-database`)을 함께 줘야 한다.
- 온라인 마이그레이션은 statement timeout 안에서 끝나야 하지만 이 백필은 배치로 나눠 커밋한다.

분류 규칙(위에서부터 처음 맞는 것):
1. research 문서 키 또는 Collector 출처 중 work 신호가 하나라도 있으면 work. 개인 키나 운영자 접두사로 덮어쓰지 않는다.
2. research 문서: doc_key 가 `research:personal:` 이면 personal (source_key 에 scope 가 들어 있다).
3. Collector 문서: 최신 원본의 origin_meta.via == 'collector' 이고 origin_meta.scope 가 personal/work 면 그 값.
4. 운영자가 `--personal-doc-key-prefix` 로 지목한 doc_key 접두사는 personal.
5. 그 밖은 미상이다. 미상은 work(work_only)로 채운다(fail-closed). 이미 조회 쪽 규칙이 접근 행이 없는 문서를 work_only 로
   취급하므로 이 백필은 가시성을 넓히지 않고, 파이프라인이 접근 행 없는 문서의 엔터티를 버리는 문제를 풀어 준다.
personal 로 올리는 것은 위 2~4 처럼 근거가 있고 work 신호가 없는 경우뿐이다.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess

RULE_RESEARCH = "research_doc_key"
RULE_COLLECTOR = "collector_origin_meta"
RULE_OPERATOR_PREFIX = "operator_personal_prefix"
RULE_UNKNOWN = "unknown_fail_closed"
TENANT_ID = "owner"
BATCH = 500

_UNCLASSIFIED_SQL = text(
    """
    SELECT d.id, d.doc_key, src.origin_meta
    FROM kb_documents d
    LEFT JOIN LATERAL (
        SELECT s.origin_meta FROM kb_revisions r JOIN kb_sources s ON s.id = r.source_id
        WHERE r.document_id = d.id ORDER BY r.rev_no DESC LIMIT 1
    ) src ON true
    WHERE NOT EXISTS (SELECT 1 FROM kb_document_access a WHERE a.document_id = d.id)
      AND d.id > :after
    ORDER BY d.id
    LIMIT :n
    """
)


@dataclass
class BackfillItem:
    document_id: uuid.UUID
    doc_key: str
    scope: str
    rule: str


@dataclass
class BackfillReport:
    applied: bool
    total: int = 0
    by_scope: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    inserted: int = 0
    samples: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mode": "apply" if self.applied else "dry_run",
            "total_without_access_row": self.total,
            "by_scope": dict(sorted(self.by_scope.items())),
            "by_rule": dict(sorted(self.by_rule.items())),
            "rows_inserted": self.inserted,
            "samples": self.samples,
        }


def classify(doc_key: str, origin_meta: dict | None, personal_prefixes: Sequence[str] = ()) -> tuple[str, str]:
    """(scope, rule). scope 는 'work' | 'personal' 이다. 미상은 work 다."""
    meta = origin_meta or {}
    if doc_key.startswith("research:work:"):
        return "work", RULE_RESEARCH
    if meta.get("via") == "collector" and meta.get("scope") == "work":
        return "work", RULE_COLLECTOR
    if doc_key.startswith("research:personal:"):
        return "personal", RULE_RESEARCH
    if meta.get("via") == "collector" and meta.get("scope") in ("personal", "work"):
        return meta["scope"], RULE_COLLECTOR
    if any(prefix and doc_key.startswith(prefix) for prefix in personal_prefixes):
        return "personal", RULE_OPERATOR_PREFIX
    return "work", RULE_UNKNOWN


def check_target(database_url: str, *, apply: bool, confirm_database: str | None) -> str:
    """쓰기 모드는 접속한 DB 이름을 사람이 다시 입력해야 한다. 접속 DB 이름을 돌려준다."""
    name = make_url(database_url).database or ""
    if apply and confirm_database != name:
        raise PermissionError(
            f"--apply 는 --confirm-database {name!r} 를 함께 줘야 한다(접속 DB 이름을 직접 확인하라)"
        )
    return name


async def plan_backfill(
    db: AsyncSession, *, personal_prefixes: Sequence[str] = (), limit: int | None = None
) -> list[BackfillItem]:
    items: list[BackfillItem] = []
    after = uuid.UUID(int=0)
    while limit is None or len(items) < limit:
        rows = (await db.execute(_UNCLASSIFIED_SQL, {"after": after, "n": BATCH})).all()
        if not rows:
            break
        for document_id, doc_key, origin_meta in rows:
            scope, rule = classify(doc_key, origin_meta, personal_prefixes)
            items.append(BackfillItem(document_id, doc_key, scope, rule))
            if limit is not None and len(items) >= limit:
                break
        after = rows[-1][0]
    return items


async def run_backfill(
    db: AsyncSession,
    *,
    apply: bool,
    personal_prefixes: Sequence[str] = (),
    limit: int | None = None,
    sample_size: int = 10,
) -> BackfillReport:
    """접근 행이 없는 문서를 분류하고(dry-run), apply 면 배치로 채운다. 멱등이다(ON CONFLICT DO NOTHING)."""
    items = await plan_backfill(db, personal_prefixes=personal_prefixes, limit=limit)
    report = BackfillReport(applied=apply, total=len(items))
    for item in items:
        report.by_scope[item.scope] = report.by_scope.get(item.scope, 0) + 1
        report.by_rule[item.rule] = report.by_rule.get(item.rule, 0) + 1
    report.samples = [
        {"document_id": str(i.document_id), "doc_key": i.doc_key[:120], "scope": i.scope, "rule": i.rule}
        for i in items[:sample_size]
    ]
    if not apply:
        return report
    for start in range(0, len(items), BATCH):
        chunk = items[start : start + BATCH]
        result = await db.execute(
            insert(KbDocumentAccess)
            .values(
                [
                    {
                        "document_id": i.document_id,
                        "tenant_id": TENANT_ID,
                        "scope": i.scope,
                        "origin_scope": i.scope,
                        "visibility": "work_only" if i.scope == "work" else "global",
                        "hidden": False,
                    }
                    for i in chunk
                ]
            )
            .on_conflict_do_nothing(index_elements=["document_id"])
        )
        report.inserted += result.rowcount or 0
        await db.commit()
    return report
