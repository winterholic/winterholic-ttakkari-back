"""Sealed holdout bundle: commitment verification, in-memory task loading, public export.

The bundle (gold, hidden checks, mutants, expected values and the candidate-visible fixtures) lives in
SEALED_ROOT, outside the repository. The repository holds only sha256 commitments
(`holdout-commitments.json`). Spec section 4.2.

Layout inside the bundle (every file is committed individually):

    manifest.sealed.json                      counts and structure facts, no answers
    tasks/<ID>/public/workspace/...           candidate-visible starting tree
    tasks/<ID>/public/prompts.json            candidate-visible prompts, ordered by stage id
    tasks/<ID>/{gold,hidden,mutants,task}.py  sealed: references, hidden checks and expected values,
                                              mutants and negative controls, assembly

A bundle that is missing, loosely permissioned, or whose bytes differ from the commitments is an INFRA
result (never a model result) and nothing in it is executed. The loader hashes the bytes it executes,
so a file swapped after verification cannot change what runs.
"""

import hashlib
import json
import linecache
import os
import stat
import sys
from pathlib import Path

from . import codes, sandbox
from .bundle_paths import BUNDLE_ID, COMMITMENTS, HOLDOUT_IDS, SEALED_ROOT
from .manifest import manifest_of_files, tree_hash
from .model import Attempt, Evaluation, Task

SEALED_MODULES = ("gold", "hidden", "mutants", "task")
MANIFEST_NAME = "manifest.sealed.json"
SCHEMA = 1


class BundleError(Exception):
    """The bundle cannot be trusted. `code` is the INFRA code the evaluator reports."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle_digest(files: dict[str, str]) -> str:
    """sha256 over the sorted `path NUL sha256 LF` lines of every committed file."""
    body = "".join(f"{path}\0{digest}\n" for path, digest in sorted(files.items()))
    return sha256_bytes(body.encode("utf-8"))


def load_commitments(path: Path = COMMITMENTS) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            codes.INFRA_SEALED_COMMITMENT_MISMATCH,
            f"commitments unreadable: {type(exc).__name__}",
        ) from exc
    files = data.get("files")
    if (
        data.get("schema") != SCHEMA
        or data.get("bundle_id") != BUNDLE_ID
        or not isinstance(files, dict)
        or not files
    ):
        raise BundleError(
            codes.INFRA_SEALED_COMMITMENT_MISMATCH, "commitments malformed"
        )
    if data.get("bundle_sha256") != bundle_digest(files):
        raise BundleError(
            codes.INFRA_SEALED_COMMITMENT_MISMATCH,
            "commitments bundle digest does not match their file list",
        )
    return data


def _check_permissions(root: Path) -> None:
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise BundleError(codes.INFRA_SEALED_MISSING, "sealed root is not a directory")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BundleError(
            codes.INFRA_SEALED_PERMISSIONS,
            f"sealed root must be owner-only (0700): mode {info.st_mode & 0o777:o}",
        )


def _refuse(rel: str, why: str) -> BundleError:
    return BundleError(
        codes.INFRA_SEALED_COMMITMENT_MISMATCH, f"{why} in bundle: {rel}"
    )


def _read_regular(path: Path, rel: str) -> bytes:
    """Read one regular, single-link file without ever blocking on or following something else.

    The type is checked on the opened descriptor, so an entry swapped for a symlink, FIFO or socket after
    the directory scan is still refused; O_NONBLOCK keeps a FIFO from stalling the open.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise _refuse(rel, f"unreadable entry ({type(exc).__name__})") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _refuse(rel, "non-regular file")
        if info.st_nlink > 1:
            raise _refuse(rel, "hard-linked file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def scan_entries(root: Path, enforce_access: bool = True) -> list[tuple[str, Path]]:
    """Regular files under root. Only directories and regular single-link files are allowed.

    Symlinks (to anything), FIFOs, sockets, devices and hard-linked files are refused, never skipped.
    Nothing is opened here, so a special file cannot block the scan.
    """
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                raise _refuse(rel, "symlink")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise _refuse(rel, "non-regular entry")
            if enforce_access and info.st_mode & 0o077:
                raise BundleError(
                    codes.INFRA_SEALED_PERMISSIONS,
                    f"{rel} is accessible to group/other",
                )
            if stat.S_ISDIR(info.st_mode):
                continue
            if info.st_nlink > 1:
                raise _refuse(rel, "hard-linked file")
            found.append((rel, path))
    return found


def read_bundle(root: Path = SEALED_ROOT) -> dict[str, bytes]:
    """Every file under root as bytes. Non-regular entries and group/other access are refused."""
    root = Path(root)
    if not os.path.lexists(root):
        raise BundleError(codes.INFRA_SEALED_MISSING, "sealed bundle directory absent")
    _check_permissions(root)
    return {rel: _read_regular(path, rel) for rel, path in scan_entries(root)}


def commitment_key(path: str) -> str:
    """Name under which a bundle file is committed in the repository.

    Sealed modules, prompts and the manifest keep their generic names. Candidate-visible workspace files
    are committed under the hash of their relative path: the path (package and module names) would reveal
    the holdout's domain, which the repository must not carry.
    """
    marker = "/public/workspace/"
    if path.startswith("tasks/") and marker in path:
        head, relative = path.split(marker, 1)
        return f"{head}{marker}{sha256_bytes(relative.encode('utf-8'))}"
    return path


def verify_bytes(data: dict[str, bytes], commitments: dict) -> None:
    committed = commitments["files"]
    keyed = {commitment_key(path): raw for path, raw in data.items()}
    if len(keyed) != len(data):
        raise BundleError(
            codes.INFRA_SEALED_COMMITMENT_MISMATCH, "colliding commitment keys"
        )
    extra = sorted(set(keyed) - set(committed))
    missing = sorted(set(committed) - set(keyed))
    if extra or missing:
        raise BundleError(
            codes.INFRA_SEALED_COMMITMENT_MISMATCH,
            f"file set differs: {len(extra)} uncommitted, {len(missing)} missing",
        )
    bad = sorted(k for k, raw in keyed.items() if sha256_bytes(raw) != committed[k])
    if bad:
        raise BundleError(
            codes.INFRA_SEALED_COMMITMENT_MISMATCH,
            f"{len(bad)} file(s) differ from their commitment",
        )


def verified_bundle(
    root: Path = SEALED_ROOT, commitments_path: Path = COMMITMENTS
) -> tuple[dict[str, bytes], dict]:
    commitments = load_commitments(commitments_path)
    data = read_bundle(root)
    verify_bytes(data, commitments)
    return data, commitments


def _public_tree(task_id: str, data: dict[str, bytes]) -> dict[str, str]:
    prefix = f"tasks/{task_id}/public/workspace/"
    return {
        path[len(prefix) :]: raw.decode("utf-8")
        for path, raw in sorted(data.items())
        if path.startswith(prefix)
    }


def _prompts(task_id: str, data: dict[str, bytes]) -> dict[str, str]:
    return json.loads(data[f"tasks/{task_id}/public/prompts.json"].decode("utf-8"))


def build_task(task_id: str, data: dict[str, bytes], bundle_sha256: str) -> Task:
    """Execute the task's sealed modules from the given bytes (already verified by the caller)."""
    from .negatives import integrity_negatives

    prefix = f"tasks/{task_id}/"
    namespace = {
        "__name__": "llme_holdout_" + task_id.replace("-", "_"),
        "PUBLIC": _public_tree(task_id, data),
        "PROMPTS": _prompts(task_id, data),
    }
    for module in SEALED_MODULES:
        source = data[f"{prefix}{module}.py"].decode("utf-8")
        filename = f"<holdout:{task_id}/{module}.py>"
        # in-memory source so inspect.getsource (leak scan) works without re-reading the disk
        linecache.cache[filename] = (
            len(source),
            None,
            source.splitlines(True),
            filename,
        )
        exec(compile(source, filename, "exec"), namespace)  # noqa: S102
    task = namespace["build"]()
    if task.id != task_id:
        raise BundleError(
            codes.INFRA_SEALED_COMMITMENT_MISMATCH, f"{task_id}: task id mismatch"
        )
    task.split = "holdout"
    task.meta["sealed_bundle_sha256"] = bundle_sha256
    task.negatives.extend(integrity_negatives(task))
    return task


def task_from_verified(task_id: str, data: dict[str, bytes], commitments: dict) -> Task:
    """Build one task from bytes that verified_bundle() already checked, pinning its base tree."""
    task = build_task(task_id, data, commitments["bundle_sha256"])
    pinned = commitments.get("tasks", {}).get(task_id, {})
    if pinned.get("base_tree_sha256") != tree_hash(manifest_of_files(task.base_files)):
        raise BundleError(
            codes.INFRA_SEALED_COMMITMENT_MISMATCH,
            f"{task_id}: base tree differs from the committed base tree",
        )
    return task


def load_task(
    task_id: str,
    root: Path = SEALED_ROOT,
    commitments_path: Path = COMMITMENTS,
) -> Task:
    """Verify the whole bundle, then build one task from the verified bytes."""
    if task_id not in HOLDOUT_IDS:
        raise KeyError(task_id)
    data, commitments = verified_bundle(root, commitments_path)
    return task_from_verified(task_id, data, commitments)


def export_public(
    task_id: str,
    dest: Path,
    root: Path = SEALED_ROOT,
    commitments_path: Path = COMMITMENTS,
) -> dict[str, str]:
    """Write only the candidate-visible workspace to dest and return the prompts by stage id."""
    data, _ = verified_bundle(root, commitments_path)
    dest = Path(dest)
    for rel, text in _public_tree(task_id, data).items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return _prompts(task_id, data)


def infra_evaluation(code: str, detail: str) -> Evaluation:
    flags = ["sealed_bundle_error"] if code.startswith("INFRA_SEALED") else []
    if code == "INFRA_ISOLATION_UNAVAILABLE":
        flags = ["sandbox_unavailable"]
    return Evaluation(codes.INFRA, code, flags, [], [], evaluated=False, detail=detail)


def evaluate_holdout(
    task_id: str,
    attempt: Attempt,
    root: Path = SEALED_ROOT,
    commitments_path: Path = COMMITMENTS,
) -> Evaluation:
    """Holdout verdict. OS isolation first, then the commitments; any failure is INFRA and runs nothing."""
    from .evaluator import evaluate_attempt

    isolated = sandbox.availability()
    if not isolated.available:
        return infra_evaluation("INFRA_ISOLATION_UNAVAILABLE", isolated.reason)
    try:
        task = load_task(task_id, root, commitments_path)
    except BundleError as exc:
        return infra_evaluation(exc.code, exc.detail)
    return evaluate_attempt(task, attempt)


def redact_evaluation(evaluation: Evaluation) -> dict:
    """What may be shown to the profile tuning side: verdict, code and check ids only (spec 4.2-7)."""
    return {
        "verdict": evaluation.verdict,
        "fail_code": evaluation.fail_code,
        "evaluated": evaluation.evaluated,
        "stages": [
            {
                "stage": stage.stage_id,
                "verdict": stage.verdict,
                "fail_code": stage.fail_code,
                "checks": [
                    {"id": row.get("id"), "passed": row.get("passed")}
                    for row in stage.checks
                ],
            }
            for stage in evaluation.stages
        ],
    }


# ---- author side: seal -------------------------------------------------------------------


def _task_facts(task: Task) -> dict:
    checks = [c for s in task.stages for c in s.checks]
    kinds: dict[str, int] = {}
    for check in checks:
        kinds[check.kind] = kinds.get(check.kind, 0) + 1
    requirements = {c.requirement for c in checks if c.requirement}
    return {
        "stages": len(task.stages),
        "checks": len(checks),
        "check_kinds": kinds,
        "requirements": len(requirements),
        "negatives": len(task.negatives),
        "negative_controls": sorted({n.id for n in task.negatives}),
        "suite_mutants": task.meta.get("suite_mutants", 0),
        "base_tree_sha256": tree_hash(manifest_of_files(task.base_files)),
    }


def seal(
    root: Path = SEALED_ROOT,
    commitments_path: Path = COMMITMENTS,
    sealed_on: str = "",
) -> dict:
    """Author-side: write manifest.sealed.json, lock permissions, write the public commitments."""
    root = Path(root)
    # refuse links and special files before any chmod: chmod would follow a symlink out of the bundle
    entries = scan_entries(root, enforce_access=False)
    root.chmod(0o700)
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        for name in dirnames:
            (Path(dirpath) / name).chmod(0o700)
    for _, path in entries:
        path.chmod(0o600)
    manifest_path = root / MANIFEST_NAME
    manifest_path.unlink(missing_ok=True)
    data = read_bundle(root)
    tasks = {}
    for task_id in HOLDOUT_IDS:
        task = build_task(task_id, data, "unsealed")
        tasks[task_id] = _task_facts(task)
        tasks[task_id]["public_files"] = sum(
            p.startswith(f"tasks/{task_id}/public/") for p in data
        )
        tasks[task_id]["sealed_files"] = sum(
            p.startswith(f"tasks/{task_id}/") and "/public/" not in p for p in data
        )
    manifest_path.write_text(
        json.dumps(
            {"bundle_id": BUNDLE_ID, "schema": SCHEMA, "tasks": tasks},
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    data = read_bundle(root)
    files = {
        commitment_key(path): sha256_bytes(raw) for path, raw in sorted(data.items())
    }
    commitments = {
        "schema": SCHEMA,
        "bundle_id": BUNDLE_ID,
        "sealed_root": str(root),
        "hash": "sha256",
        "bundle_sha256": bundle_digest(files),
        "sealed_on": sealed_on,
        "model_runs_before_seal": 0,
        "tasks": {
            task_id: {
                "base_tree_sha256": facts["base_tree_sha256"],
                "checks": facts["checks"],
                "requirements": facts["requirements"],
                "negatives": facts["negatives"],
                "suite_mutants": facts["suite_mutants"],
                "public_files": facts["public_files"],
                "sealed_files": facts["sealed_files"],
            }
            for task_id, facts in tasks.items()
        },
        "files": files,
    }
    Path(commitments_path).write_text(
        json.dumps(commitments, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return commitments


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "verify"
    if command == "verify":
        try:
            data, commitments = verified_bundle()
        except BundleError as exc:
            print(json.dumps({"ok": False, "code": exc.code, "detail": exc.detail}))
            return 1
        print(
            json.dumps(
                {
                    "ok": True,
                    "files": len(data),
                    "bundle_sha256": commitments["bundle_sha256"],
                    "tasks": len(commitments["tasks"]),
                }
            )
        )
        return 0
    if command == "seal":
        sealed_on = argv[1] if len(argv) > 1 else ""
        commitments = seal(sealed_on=sealed_on)
        print(
            json.dumps(
                {
                    "files": len(commitments["files"]),
                    "bundle_sha256": commitments["bundle_sha256"],
                }
            )
        )
        return 0
    if command == "export-public" and len(argv) == 3:
        prompts = export_public(argv[1], Path(argv[2]))
        print(json.dumps({"stages": list(prompts)}))
        return 0
    print("usage: bundle [verify | seal [DATE] | export-public TASK DEST]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
