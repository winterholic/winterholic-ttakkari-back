"""Where the sealed holdout bundle lives (outside the repository) and where its commitments are.

The location is fixed by decision, not configurable by environment: an override would let a caller
point the evaluator at a different bundle and still get a holdout verdict.
"""

from pathlib import Path

BUNDLE_ID = "local-llm-holdout-r1"
# Outside the repository, every worktree and ~/.ttakkari. Mode 0700, owner only.
SEALED_ROOT = Path("/Users/winterholic/bench-sealed") / BUNDLE_ID
COMMITMENTS = (
    Path(__file__).resolve().parents[2]
    / "docs/design/local-llm-worker-refactor/holdout-commitments.json"
)
HOLDOUT_IDS = tuple(
    f"B4-{kind}-H"
    for kind in ("EXP", "SGL", "MUL", "API", "DBG", "REF", "TST", "REC", "LNG")
)
