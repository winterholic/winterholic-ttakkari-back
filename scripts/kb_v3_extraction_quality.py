"""KB v3 추출 품질 측정: 실제 Markdown 문서를 구조 청킹한 뒤 L0/L1(/L2 비활성) 추출 결과를 센다.

DB 없이 동작한다. 사용: uv run python scripts/kb_v3_extraction_quality.py FILE.md [FILE.md ...] [--seed]
--seed 는 시드 별칭 사전(L1)을 켠다. 출력은 JSON 한 개.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from ttakkari.common.chunking import chunk_structured_document
from ttakkari.kb_models import KbPassage, KbRevision
from ttakkari.kb_pipeline.extractor_step import (
    ExtractionOptions,
    extract_revision_knowledge,
)


def _passages(text: str, rev_id: uuid.UUID) -> list[KbPassage]:
    out = []
    for c in chunk_structured_document(text):
        out.append(KbPassage(
            id=uuid.uuid4(), revision_id=rev_id, ord=c.ord, heading=(c.section_path[-1] if c.section_path else None),
            body=c.body, char_start=c.char_start, char_end=c.char_end, sha256=c.sha256,
            section_path=list(c.section_path), chunk_kind=c.chunk_kind,
        ))
    return out


async def measure(paths: list[Path], seed: bool) -> dict:
    known = None
    meta = None
    if seed:
        from ttakkari.kb_pipeline.alias_dictionary import load_seed_dictionary
        sd = load_seed_dictionary()
        known, meta = dict(sd.aliases), dict(sd.entities)
    total = {"passages": 0, "entities": 0, "assertions": 0, "relations": 0, "mentions": 0}
    per_file = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        rev = KbRevision(id=uuid.uuid4(), document_id=uuid.uuid4(), body=text)
        passages = _passages(text, rev.id)
        kwargs = {"known_aliases": known, "known_entities": meta} if known is not None else {}
        res = await extract_revision_knowledge(
            rev, passages, options=ExtractionOptions(origin_scope="personal", visibility="global", **kwargs))
        m = res.metrics
        row = {
            "passages": len(passages), "entities": len(res.entities), "assertions": len(res.assertions),
            "relations": len(res.relations), "mentions": len(res.mentions),
            "l0_entities_raw": m.get("l0_entities_count"),
            "l0_filtered": m.get("l0_filtered"),
            "l1_mentions": m.get("l1_mentions_count", 0),
            "l1_seed_entities": m.get("l1_seed_entities", 0),
            "l1_seed_names": [e.canonical_name for e in res.entities if e.alias_source == "seed"][:10],
            "relation_types": sorted({r.relation_type for r in res.relations}),
            "sample_entities": [e.canonical_name for e in res.entities[:12]],
            "status": res.status,
        }
        per_file[str(path)] = row
        for k in total:
            total[k] += row[k]
    return {"total": total, "files": per_file}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    print(json.dumps(asyncio.run(measure([Path(f) for f in a.files], a.seed)), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
