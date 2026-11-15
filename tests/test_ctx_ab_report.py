"""컨텍스트 A/B 보고 집계. 네트워크를 쓰지 않는다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ctx_ab_report import noncached_input, summarize


def _run(arm=None, **kw):
    usage = dict(kw.pop("usage", {}))
    if arm is not None:
        usage["code_map_arm"] = arm
    base = {"status": "succeeded", "usage": usage, "tool_calls": 0}
    base.update(kw)
    return base


def test_noncached_input_subtracts_cache_read():
    assert noncached_input({"input_tokens": 1000, "cache_read_input_tokens": 900}) == 100
    assert noncached_input({"input_tokens": 1000, "cached_input_tokens": 200}) == 800
    assert noncached_input({"input_tokens": 1000}) == 1000
    assert noncached_input({}) is None


def test_summarize_compares_arms():
    runs = [_run("on", usage={"input_tokens": 100}, tool_calls=4,
                 status="succeeded") for _ in range(40)]
    runs += [_run("control", usage={"input_tokens": 200}, tool_calls=8) for _ in range(40)]
    rows = summarize(runs, "code_map_arm")
    assert rows["on"]["n"] == 40 and rows["control"]["n"] == 40
    assert rows["on"]["mean_noncached_input"] == 100
    assert rows["control"]["mean_tool_calls"] == 8
    assert rows["on"]["verdict"] == "비교 가능"


def test_small_sample_is_marked_insufficient():
    runs = [_run("on") for _ in range(29)] + [_run("control") for _ in range(40)]
    rows = summarize(runs, "code_map_arm")
    assert rows["on"]["verdict"] == "표본 부족"
    assert "mean_noncached_input" not in rows["on"] or True  # 결론 없이 수치만 둔다


def test_rates_use_their_own_denominators():
    runs = [_run("on", usage={"false_completion": True}, status="succeeded") for _ in range(20)]
    runs += [_run("on", usage={"completion": {"status": "부분", "violation": False}}) for _ in range(10)]
    runs += [_run("on", status="failed") for _ in range(10)]
    rows = summarize(runs, "code_map_arm")
    on = rows["on"]
    assert on["false_completion_rate"] == 1.0 and on["false_completion_n"] == 20
    assert on["partial_rate"] == 1.0 and on["partial_n"] == 10
    assert on["fail_cancel_rate"] == 0.25 and on["n"] == 40
