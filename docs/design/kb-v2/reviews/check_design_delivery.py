"""Document/evidence checks and small abstract models; no product/DB/model execution."""

import hashlib
import itertools
import json
import math
import re
import statistics
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
DESIGN = HERE.parent / "knowledge-library-design-v2.md"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def inventory(design, response):
    for number in range(1, 14):
        require(f"### P{number} —" in design, f"missing design P{number}")
        require(f"| P{number} |" in response, f"missing response P{number}")
    for number in range(1, 25):
        require(f"| F{number} |" in response, f"missing response F{number}")
    require("| F9a |" in response, "missing response F9a")
    require("제품 구현 금지" in design, "missing implementation prohibition")
    require("독립 확인 대기" in design, "missing independent-check pending state")


def main():
    manifest = json.loads((HERE / "source-manifest.json").read_text())
    originals = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    for entry in manifest["files"]:
        stored = HERE / entry["stored_relative_path"]
        data = stored.read_bytes()
        require(len(data) == entry["bytes"], f"size: {stored}")
        require(hashlib.sha256(data).hexdigest() == entry["sha256"], f"hash: {stored}")
        if originals:
            original = originals / entry["original_relative_path"]
            require(original.read_bytes() == data, f"source bytes changed: {original}")
    require(len(manifest["files"]) == 18, "unexpected original evidence count")
    print("evidence: 18/18 exact sha256+size; source byte comparison:", bool(originals))

    design = DESIGN.read_text()
    response = (HERE / "v2-review-response.md").read_text()
    inventory(design, response)
    for bad_design, bad_response in (
        (design.replace("### P9 —", "### removed —"), response),
        (design, response.replace("| F24 |", "| removed |")),
    ):
        try:
            inventory(bad_design, bad_response)
        except AssertionError:
            pass
        else:
            raise AssertionError("negative inventory control failed")
    print("coverage: P1-13=13/13, F1-24=24/24 plus F9a; negative controls rejected")

    paths = [
        DESIGN,
        ROOT / "docs/adr/0063-knowledge-library.md",
        ROOT / "docs/kb/design-v1.md",
        ROOT / "docs/design/knowledge-library.md",
        ROOT / "docs/design/collector-platform/contract-compatibility.md",
    ]
    paths += [
        HERE / name
        for name in (
            "README.md",
            "v2-review-response.md",
            "collector-contract-followups.md",
        )
    ]
    links = 0
    for path in paths:
        text = path.read_text()
        # Existing historical docs can contain raw pseudo-URLs; inspect Markdown file links.
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            filename = target.split("#", 1)[0]
            if not filename:
                continue
            require(
                (path.parent / filename).exists(), f"broken file link {path}: {target}"
            )
            links += 1
    print(
        f"links: {links} local file targets exist (historical original review excluded)"
    )

    raw_text = (HERE / "evidence/lexcmp_1m.out.txt").read_text()
    raw = json.loads(raw_text[raw_text.index("{") :])
    expected = {
        "pg_dffilter1pct_limit2000_appbm25": (19.71, 0.878),
        "tantivy_bm25": (8.27, 0.851),
        "pg_tsrankcd_or": (3501.07, 0.391),
    }
    for name, pair in expected.items():
        require(raw[name]["n"] == 213, f"raw query count: {name}")
        require((raw[name]["p95_ms"], raw[name]["recall@100"]) == pair, name)
    print(
        "historical raw: 213 queries; PG BM25 19.71/.878, Tantivy 8.27/.851, ts_rank_cd 3501.07/.391"
    )

    z = statistics.NormalDist().inv_cdf
    n = math.ceil(((z(0.975) + z(0.8)) * 0.335 / 0.05) ** 2)
    require(n == 353, "sample calculation")
    upper = 1 - 0.05 ** (1 / 40)
    chars_hours = 688 * 1_000_000 / 80_000 / 3600
    cpu_hours = 1_000_000 / 6.1 / 3600
    print(
        f"planning only: n_eff={n}; 0/40 one-sided upper={upper:.6f}; Kiwi={chars_hours:.4f}h; CPU={cpu_hours:.4f}h"
    )

    inputs = ["e\u00ad\u0301", "\u1112\u200d\u1161", "a\u200c\u0300", "e\u0008\u0301"]
    removed = "\u00ad\u200d\u200c\u0008"
    for text in inputs:
        early = unicodedata.normalize("NFC", text)
        cleaned = "".join(char for char in early if char not in removed)
        final = unicodedata.normalize("NFC", cleaned)
        require(
            not unicodedata.is_normalized("NFC", cleaned), "old-order negative control"
        )
        require(unicodedata.is_normalized("NFC", final), "final NFC model")
        require(unicodedata.normalize("NFC", final) == final, "NFC idempotence")
    changes = {
        text: unicodedata.normalize("NFKC", text)
        for text in ["㈜", "…", "ﬁ", "x²", "µ", "℃"]
    }
    print(
        "Unicode model: 4/4 early-NFC counterexamples; final NFC fixed points; NFKC:",
        changes,
    )

    # Exhaustive interleavings of two toy transactions (not a PostgreSQL test).
    events = ("Xa", "Xc", "Ya", "Yc", "R")
    valid = [
        order
        for order in itertools.permutations(events)
        if order.index("Xa") < order.index("Xc")
        and order.index("Ya") < order.index("Yc")
    ]

    def loses(order):
        allocated, committed, checkpoint = {}, set(), 0
        for event in order:
            if event.endswith("a"):
                allocated[event[0]] = len(allocated) + 1
            elif event.endswith("c"):
                committed.add(allocated[event[0]])
            else:
                checkpoint = max(committed, default=0)
        # Reader polls again with seq > checkpoint. Any late low seq is lost.
        at_read = set()
        for event in order[: order.index("R")]:
            if event.endswith("c"):
                at_read.add(allocated[event[0]])
        return bool({seq for seq in committed - at_read if seq <= checkpoint})

    unguarded = sum(loses(order) for order in valid)
    serial = [
        order
        for order in valid
        if order.index("Xc") < order.index("Ya")
        or order.index("Yc") < order.index("Xa")
    ]
    require(unguarded > 0, "missing inversion negative control")
    require(not any(loses(order) for order in serial), "serial toy model unsafe")
    print(
        f"toy seq model: unguarded loss {unguarded}/{len(valid)}, lock-before-allocation serialized loss 0/{len(serial)}"
    )
    observations = [(1, "A"), (2, "B"), (3, "A"), (0, "old")]
    require(max(observations)[1] == "A", "observation model")
    require(max(observations + [(4, "A")])[0] == 4, "same content verification")
    print(
        "toy observations: A->B->A current A; late event0 unchanged; A->A freshness advances"
    )
    print(
        "UNEXECUTED: actual PostgreSQL/kill/replay, inference/GPU/CPU, million benchmark, product harness, deployment"
    )


if __name__ == "__main__":
    main()
