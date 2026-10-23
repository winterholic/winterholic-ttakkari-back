"""Verdicts and failure codes (spec section 5.2)."""

PASS = "PASS"
FAIL = "FAIL"  # model-caused
INFRA = "INFRA"  # non-model cause that is proven
UNKNOWN = "UNKNOWN"  # cause not established; never silently excluded

FAIL_CODES = (
    "FAIL_NOOP",
    "FAIL_COMMENT_ONLY",
    "FAIL_PARTIAL_PATHS",
    "FAIL_TEST_TAMPER",
    "FAIL_SKIP_XFAIL",
    "FAIL_STUB_EXIT0",
    "FAIL_CHECKER_PATCH",
    "FAIL_MISSING_TARGET",
    "FAIL_EMPTY_RESULT",
    "FAIL_FORBIDDEN_WRITE",
    "FAIL_PUBLIC",
    "FAIL_HIDDEN",
    "FAIL_HIDDEN_ONLY",
    "FAIL_REGRESSION",
    "FAIL_CAUSE_PATCH_MISMATCH",
    "FAIL_REPORT_MISMATCH",
)
INFRA_BASE_MISMATCH = "INFRA_BASE_MISMATCH"

# When several checks fail the most structural reason wins.
FAIL_PRIORITY = (
    "FAIL_CHECKER_PATCH",
    "FAIL_STUB_EXIT0",
    "FAIL_TEST_TAMPER",
    "FAIL_FORBIDDEN_WRITE",
    "FAIL_SKIP_XFAIL",
    "FAIL_COMMENT_ONLY",
    "FAIL_EMPTY_RESULT",
    "FAIL_MISSING_TARGET",
    "FAIL_PARTIAL_PATHS",
    "FAIL_CAUSE_PATCH_MISMATCH",
    "FAIL_PUBLIC",
    "FAIL_HIDDEN_ONLY",
    "FAIL_HIDDEN",
    "FAIL_REGRESSION",
    "FAIL_REPORT_MISMATCH",
    "FAIL_NOOP",
)

KIND_DELTA = "delta"  # red at the pristine base, green at the reference
KIND_INVARIANT = "invariant"  # green at both
KIND_PROCESS = "process_metric"  # recorded, never gating

CHANGE_DIFF = "diff_required"
CHANGE_READ_ONLY = "read_only_report"

NEGATIVE_CONTROLS = tuple(f"N{i}" for i in range(1, 34))
