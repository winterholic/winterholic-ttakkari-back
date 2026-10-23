"""Judge-side helpers. Judges run in the evaluator process, never next to candidate code.

A judge receives `obs` (raw observations from the probe) and `facts` (what the parent saw on disk)
and raises AssertionError or Fail. A missing observation key without a probe error means the
observation stream was incomplete or forged, which the evaluator classifies as checker tampering.
"""


class MissingObservation(Exception):
    """A probe key the judge needs is absent without a probe error: the stream was cut or forged."""


class Obs(dict):
    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            raise MissingObservation(key) from None


class Fail(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def val(obs: dict, key: str):
    """Value recorded through attempt(); a raised exception is an assertion failure."""
    entry = obs[key]
    if isinstance(entry, dict) and "raised" in entry:
        raise AssertionError(
            f"{key} raised {entry['raised'][0]}: {entry.get('msg', '')}"
        )
    if isinstance(entry, dict) and "ok" in entry:
        return entry["ok"]
    return entry


def raised(obs: dict, key: str, name: str) -> None:
    entry = obs[key]
    names = entry.get("raised", []) if isinstance(entry, dict) else []
    assert name in names, f"{key}: expected {name}, got {entry!r}"


def same(got, want, label: str = "") -> None:
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def unittest_ok(obs: dict, key: str, min_tests: int) -> None:
    """Public-suite judge: green, non-empty, no skip/xfail (with the specific failure code)."""
    r = obs[key]
    if r["skipped"] or r["expected_failures"] or r["unexpected_successes"]:
        raise Fail("FAIL_SKIP_XFAIL", f"skip/xfail present: {r}")
    if r["tests"] < min_tests:
        raise Fail(
            "FAIL_EMPTY_RESULT" if r["tests"] == 0 else "FAIL_PUBLIC",
            f"collected {r['tests']} tests, need {min_tests}",
        )
    if r["failures"] or r["errors"]:
        raise Fail("FAIL_PUBLIC", r.get("tail") or "unittest failed")
