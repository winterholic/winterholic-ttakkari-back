"""Guard and condition mutants for the E0 harness.

Each mutant replaces one exact source snippet (it must occur exactly once).
`expect="kill"` means a unit test must fail on the mutant. `expect="equivalent"`
records a mutant judged behaviour-equivalent or unprovable, with the reason; it
is reported but does not fail the run. Reason producers (`reasons.append`,
`"reasons": [...]`) are generated automatically by run_mutation.py.
"""

K = "src/ttakkari/kb_eval/"


def m(mid, path, find, replace, expect="kill", note=""):
    return {
        "id": mid,
        "file": K + path,
        "find": find,
        "replace": replace,
        "expect": expect,
        "note": note,
    }


GUARDS = [
    # Latency reason conditions and guards.
    m("L1", "latency.py", 'orders.count("BA")) > 1', 'orders.count("BA")) > 3'),
    m("L2", "latency.py", "len(blocks) < 100", "len(blocks) < 10"),
    m("L3", "latency.py", "for b in blocks) < 600:", "for b in blocks) < 60:"),
    m(
        "L4",
        "latency.py",
        '> 0.2 * max(1.0, loads.mean()):\n        reasons.append("load_imbalance")',
        '> 2.0 * max(1.0, loads.mean()):\n        reasons.append("load_imbalance")',
    ),
    m(
        "L5",
        "latency.py",
        '> 0.2 * max(1.0, loads.mean()):\n            reasons.append("order_load_imbalance")',
        '> 2.0 * max(1.0, loads.mean()):\n            reasons.append("order_load_imbalance")',
    ),
    m(
        "L6",
        "latency.py",
        'len({b["profile"] for b in blocks}) != 1',
        'len({b["profile"] for b in blocks}) > 5',
    ),
    m(
        "L7",
        "latency.py",
        'any(o not in {"AB", "BA"} for o in orders)',
        "False",
        "equivalent",
        "LatencyBlock.order is Literal['AB','BA']; schema rejects anything else first",
    ),
    m("L8", "latency.py", "if high <= 1.2", "if high <= 1.3"),
    m("L9", "latency.py", "if low > 1.2", "if low > 1.3"),
    m(
        "L10",
        "latency.py",
        'slo = 800 if mode == "full" else 200',
        'slo = 900 if mode == "full" else 200',
    ),
    m(
        "L11",
        "latency.py",
        'if replicates < BOOTSTRAP_POLICY["minimum_replicates"]:\n        reasons',
        "if replicates < 1:\n        reasons",
    ),
    m(
        "L12",
        "latency.py",
        'if obs["queue_ms"] + obs["service_ms"] > obs["e2e_ms"] + 1e-6:',
        "if False:",
    ),
    m(
        "L13",
        "latency.py",
        'if len({b["block"] for b in blocks}) != len(blocks):',
        "if False:",
    ),
    m("L14", "schema.py", "if value != 1.0:", "if False:"),
    m(
        "L15",
        "latency.py",
        "if (times[:, 1] <= 0).any():",
        "if False:",
        "equivalent",
        "LatencyArm.e2e_ms has gt=0; schema rejects zero/negative first",
    ),
    m("L16", "latency.py", "if not begin <= entered <= serviced <= end:", "if False:"),
    m(
        "L17",
        "latency.py",
        "not np.isfinite(obs[k]) or obs[k] < 0",
        "False",
        "equivalent",
        "LatencyArm fields are finite (allow_inf_nan=False) and ge/gt 0 by schema",
    ),
    m(
        "L18",
        "latency.py",
        "(times.std(axis=0) < floor).any()",
        "(times.std(axis=0) < floor).all()",
    ),
    m("L19", "latency.py", 'if mode not in {"full", "fast"}:', "if False:"),
    # Degeneracy floors (N1) and their policy constants.
    m(
        "N1a",
        "validation.py",
        "return not high - low >= minimum",
        "return high - low < minimum",
    ),
    m(
        "N1b",
        "validation.py",
        "return not high - low >= minimum",
        "return not high - low > minimum",
    ),
    m(
        "N1c",
        "validation.py",
        '"timer_resolution_ms": 0.001',
        '"timer_resolution_ms": 0.0',
    ),
    m("N1d", "validation.py", '"latency_min_cv": 0.0001', '"latency_min_cv": 0.0'),
    m(
        "N1e",
        "validation.py",
        '"latency_ratio_ci_min_width": 0.0005',
        '"latency_ratio_ci_min_width": 0.0',
    ),
    m(
        "N1f",
        "validation.py",
        '"quality_ci_min_width": 0.001',
        '"quality_ci_min_width": 0.0',
    ),
    m(
        "N1g",
        "validation.py",
        '"quality_dev_min_std": 0.001',
        '"quality_dev_min_std": 0.0',
    ),
    m(
        "N1h",
        "validation.py",
        '"minimum_replicates": 10000',
        '"minimum_replicates": 100',
    ),
    # Quality gate.
    m(
        "Q1",
        "statistics.py",
        'd["low"] > -margin and d["holm_p"] <= alpha',
        'd["low"] > -1 and d["holm_p"] <= alpha',
        "equivalent",
        "third review: with a registered family both lows > -margin almost always imply holm_p <= alpha; no counterexample found",
    ),
    m(
        "Q2",
        "statistics.py",
        'd["low"] > -margin and d["holm_p"] <= alpha',
        'd["low"] > -margin',
        "equivalent",
        "third review: Holm adjustment overlaps the Bonferroni tail; no counterexample found",
    ),
    m(
        "Q3",
        "statistics.py",
        'any(d["high"] < -margin for d',
        'any(d["high"] < 1 for d',
    ),
    m(
        "Q4",
        "statistics.py",
        "bs[:, i] - means[i] - margin >= means[i]",
        "bs[:, i] - margin >= means[i]",
    ),
    m(
        "Q5",
        "statistics.py",
        "trials >= 200",
        "trials >= 1",
        "equivalent",
        "Wilson upper bound 0/n <= 0.05 already needs n >= ~72; lower trials cannot meet the loss bound",
    ),
    m(
        "Q6",
        "statistics.py",
        "and len(sizes) >= 20\n",
        "and len(sizes) >= 1\n",
        "equivalent",
        "effective clusters >= 20 implies at least 20 clusters",
    ),
    m(
        "Q6b",
        "statistics.py",
        "float(sizes.sum() ** 2 / (sizes @ sizes)) >= 20",
        "float(sizes.sum() ** 2 / (sizes @ sizes)) >= 2",
    ),
    m("Q7", "statistics.py", "cis[0][0] >= 0.8", "cis[0][0] >= 0.0"),
    m(
        "Q8",
        "statistics.py",
        "cis[1][1] <= 0.05",
        "cis[1][1] <= 1.0",
        "equivalent",
        "undecided in third review: no pilot with high neutral pass probability also passes a -0.10 loss",
    ),
    m("Q9", "statistics.py", "if len(set(groups)) < 2:", "if len(set(groups)) < 1:"),
    m(
        "Q10",
        "statistics.py",
        "running = min(1.0, max(running, (len(ordered) - i) * pvalues[key]))",
        "running = (len(ordered) - i) * pvalues[key]",
    ),
    m(
        "Q11",
        "statistics.py",
        "passed = len(sizes) >= 2 and all(",
        "passed = all(",
        "equivalent",
        "one cluster always adds insufficient_clusters (status inconclusive); power needs >= 20 clusters",
    ),
    m("Q12", "statistics.py", "elif set(groups) & set(dev_groups):", "elif False:"),
    # Defect 1: split leakage.
    m(
        "D1a",
        "integrity.py",
        'if grade > 0:\n                join(node, "doc:" + passage_owners[passage])',
        "pass",
    ),
    m("D1b", "loader.py", "        if missing:\n", "        if False:\n"),
    m("D1c", "integrity.py", 'join(node, "group:" + c["group"])', "pass"),
    m("D1d", "integrity.py", 'join(node, "doc:" + doc)', "pass"),
    m(
        "D1e",
        "loader.py",
        'if any(a["doc_key"] not in c["documents"] for a in c["anchors"]):',
        "if False:",
    ),
    m(
        "D1f",
        "loader.py",
        'links.append([by_body[s["body_sha256"]], s["doc_key"]])',
        "pass",
    ),
    m("D1g", "integrity.py", 'join("doc:" + pair[0], "doc:" + pair[1])', "pass"),
    m("D1h", "loader.py", 'or not set(c["relevance"]) <= passage_ids', ""),
    m("D1i", "loader.py", 'if c["answerable"] and not c["anchors"]:', "if False:"),
    m("D1j", "loader.py", 'if c["verification"] == "human_verified":', "if False:"),
    m(
        "D1k",
        "loader.py",
        '            raise ValueError("source_hash_mismatch:"',
        '            pass  # ValueError("source_hash_mismatch:"',
    ),
    # Defect 3: rights provenance.
    m("D3a", "rights.py", 'if bundle["kind"] != "fixture":', "if False:"),
    m("D3b", "rights.py", 'or r.get("provenance_kind") != "synthetic"', ""),
    m("D3c", "rights.py", 'or r.get("review_evidence") is not None', ""),
    m("D3d", "rights.py", 'or not r["original_url"].startswith("fixture://")', ""),
    m(
        "D3e",
        "rights.py",
        'if bundle["links"] and r["transform_allowed"] is not True:',
        "if False:",
    ),
    m(
        "D3f",
        "rights.py",
        'if r["status"] != "verified" or r["storage_allowed"] is not True:',
        'if r["storage_allowed"] is not True:',
    ),
    m(
        "D3g",
        "rights.py",
        'if r["status"] != "verified" or r["storage_allowed"] is not True:',
        'if r["status"] != "verified":',
    ),
    m(
        "D3h",
        "rights.py",
        'not s["source_uri"].startswith("fixture://") for s',
        "False for s",
    ),
    m(
        "D3i",
        "rights.py",
        'if bundle["observations"]["kind"] != "fixture" or any(',
        "if any(",
    ),
    m("D3j", "rights.py", 'if len(rights) != len(bundle["rights"]):', "if False:"),
    m(
        "D3k",
        "runner.py",
        'bundle = {**bundle, "rights_authorization": validate_rights(bundle)}',
        "bundle = dict(bundle)",
    ),
    # Defect 4: reviewer audit.
    m(
        "D4a",
        "state.py",
        '    raise ValueError(\n        "reviewer_authentication_required',
        '    Path(path).parent.mkdir(parents=True, exist_ok=True)\n    Path(path).write_text("consumed")\n    raise ValueError(\n        "reviewer_authentication_required',
    ),
    m(
        "D4b",
        "state.py",
        'if role != "independent_reviewer" or not actor or actor == "developer":',
        "if False:",
        "equivalent",
        "open_holdout refuses unconditionally afterwards; only the error text differs",
    ),
    m("D4c", "state.py", '"actor_authentication": "unauthenticated_claim",', ""),
    m("D4d", "state.py", 'if event["actor_kind"] == "human":', "if False:"),
    m(
        "D4e",
        "state.py",
        'if event["label_version"] != old.get("label_version", 0):',
        "if False:",
    ),
    # Cache, replay and resume guards.
    m("R1", "state.py", 'if record["key"] != expected_key:', "if False:"),
    m(
        "R2",
        "state.py",
        'if digest(canonical(record["payload"])) != record["payload_sha256"]:',
        "if False:",
    ),
    m(
        "R3",
        "state.py",
        'if canonical(record["payload"]) != canonical(payload):',
        "if False:",
    ),
    m("R4", "runner.py", "if mf.exists() and canonical(", "if False and canonical("),
    m(
        "R5",
        "runner.py",
        'if any(c["split"] == "holdout" for c in bundle["cases"]):',
        "if False:",
    ),
    m(
        "R6",
        "state.py",
        '        raise ValueError("incomplete_cache_identity")',
        "        pass",
    ),
    # Metric definitions.
    m(
        "X1",
        "metrics.py",
        "(2 ** relevant.get(key, 0) - 1) / math.log2(i + 2)",
        "(2 ** relevant.get(key, 0) - 1) / math.log2(i + 3)",
    ),
    m(
        "X2",
        "metrics.py",
        "mrr=1 / hits[0] if hits else 0.0",
        "mrr=1 / (hits[0] + 1) if hits else 0.0",
    ),
    m(
        "X3",
        "metrics.py",
        "recall=len(hits) / len(relevant)",
        "recall=len(hits) / (len(relevant) + 1)",
    ),
    m(
        "X4",
        "metrics.py",
        'r for r in valid if not r["answerable"] or r["answer_correct"] is False',
        'r for r in valid if not r["answerable"]',
    ),
    m("X5", "metrics.py", "not 0 <= g <= 3 for g", "not 0 <= g <= 4 for g"),
    m("X6", "metrics.py", "(2**g - 1) / math.log2(i + 2)", "(2**g) / math.log2(i + 2)"),
]
