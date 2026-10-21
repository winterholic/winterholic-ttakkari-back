"""Immutable source anchors and connected document split validation."""

import hashlib
import re
import unicodedata


def digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def anchor(gold, source, *, passages=None):
    invalid = lambda reason: {
        "status": "gold_invalid",
        "reason": reason,
        "answer_id": gold["answer_id"],
    }
    identity = (
        "doc_key",
        "source_revision_id",
        "raw_sha256",
        "body_sha256",
        "source_uri",
        "observed_at",
    )
    if any(gold[key] != source[key] for key in identity):
        return invalid("source_changed")
    body, quote = source["body"], gold["quote"]
    if digest(body) != source["body_sha256"]:
        return invalid("body_hash_mismatch")
    if not quote or digest(quote) != gold["quote_sha256"]:
        return invalid("quote_hash_mismatch")
    matches = []
    for match in re.finditer(f"(?={re.escape(quote)})", body):
        start, end = match.start(), match.start() + len(quote)
        before, after = gold.get("context_before", ""), gold.get("context_after", "")
        if body[:start].endswith(before) and body[end:].startswith(after):
            matches.append((start, end))
    if len(matches) > 1:
        return {
            "status": "anchor_ambiguous",
            "reason": "repeated_quote",
            "matches": len(matches),
            "answer_id": gold["answer_id"],
        }
    if not matches:

        def normalized(s):
            return " ".join(unicodedata.normalize("NFKC", s).casefold().split())

        reason = (
            "normalization_review_required"
            if normalized(quote) in normalized(body)
            else "quote_not_found"
        )
        return invalid(reason)
    start, end = matches[0]
    result = {
        "status": "valid",
        "reason": "exact_quote",
        "span": [start, end],
        "answer_id": gold["answer_id"],
        "passage_sets": [],
    }
    if passages is None:
        return result
    ps = sorted(passages, key=lambda p: (p["start"], p["end"], p["id"]))
    for p in ps:
        if not 0 <= p["start"] < p["end"] <= len(body):
            raise ValueError("invalid_passage_span")
        if max(0, min(end, p["end"]) - max(start, p["start"])) >= 0.8 * (end - start):
            result["passage_sets"].append([p["id"]])
    if not result["passage_sets"]:
        # Shortest contiguous span-cover sets, never multiple independent gold answers.
        options = []
        for i, p in enumerate(ps):
            cursor = start
            selected = []
            for part in ps[i:]:
                if part["start"] > cursor:
                    break
                if part["end"] > cursor:
                    selected.append(part["id"])
                    cursor = part["end"]
                if cursor >= end:
                    options.append(selected)
                    break
        if options:
            size = min(map(len, options))
            result["passage_sets"] = [p for p in options if len(p) == size]
    return result


def components(cases, links):
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("query_id_leakage")
    parent = {}

    def root(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        a, b = root(a), root(b)
        parent[max(a, b)] = min(a, b)

    for pair in links:
        if len(pair) != 2:
            raise ValueError("invalid_document_link")
        join("doc:" + pair[0], "doc:" + pair[1])
    for c in cases:
        node = "query:" + c["id"]
        root(node)
        if not c["documents"]:
            raise ValueError("missing_group_documents")
        for doc in c["documents"]:
            join(node, "doc:" + doc)
        join(node, "group:" + c["group"])
    result = {c["id"]: root("query:" + c["id"]) for c in cases}
    splits = {}
    for c in cases:
        splits.setdefault(result[c["id"]], set()).add(c["split"])
    if any(len(s) > 1 for s in splits.values()):
        raise ValueError("split_leakage")
    return result
