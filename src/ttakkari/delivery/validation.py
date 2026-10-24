"""Project backed validation for immutable delivery candidates."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from importlib.metadata import distributions
from pathlib import Path

SOURCE_CRITICAL_PREFIXES = (
    "src/",
    "alembic/",
    "deploy/",
    "tests/",
    "unit_tests/",
    "benchmark_tests/",
    "pyproject.toml",
    "uv.lock",
)
AUDIT_DOC_PREFIXES = (
    "review/platform-v2/",
    "docs/design/platform-v2/",
)


class CandidateBindingError(RuntimeError):
    """Raised when candidate checkout or snapshot binding fails fail-closed checks."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _runtime_fingerprint() -> str:
    """Capture actual process inputs instead of trusting caller-supplied hashes.

    Environment values stay in memory and only their aggregate digest is kept.
    Installed distribution metadata includes pytest/plugin and dependency versions.
    This does not attest external services or mutable data; their closure must
    still be declared unverified and use the broad validation gate.
    """
    payload = {
        "python": sys.version,
        "executable": str(Path(sys.executable).resolve()),
        "platform": platform.platform(),
        "environment_sha256": hashlib.sha256(
            json.dumps(dict(os.environ), sort_keys=True).encode()
        ).hexdigest(),
        "distributions": sorted(
            (dist.metadata.get("Name", ""), dist.version) for dist in distributions()
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CandidateSnapshotIdentity:
    """Immutable identity of a delivery candidate git checkout and working tree."""

    candidate_path: str
    candidate_head_sha: str
    candidate_tree_sha: str
    candidate_src_tree_sha: str
    dirty_source_paths: tuple[str, ...]
    dirty_audit_doc_paths: tuple[str, ...]
    other_dirty_paths: tuple[str, ...]
    is_clean_source_tree: bool
    is_clean_audit_docs: bool
    file_hashes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def capture(cls, candidate_path: Path) -> CandidateSnapshotIdentity:
        """Capture git identity and working-tree status fail-closed from candidate checkout."""
        if not candidate_path.is_dir() or not (candidate_path / ".git").exists():
            raise CandidateBindingError(
                "invalid_candidate", f"Not a git checkout: {candidate_path}"
            )

        def _git(*args: str) -> str:
            p = subprocess.run(
                ["git", "-C", str(candidate_path), *args],
                capture_output=True,
                text=True,
                check=False,
            )
            if p.returncode != 0:
                raise CandidateBindingError(
                    "git_command_failed",
                    f"git {' '.join(args)} failed: {p.stderr.strip()}",
                )
            return p.stdout

        head_sha = _git("rev-parse", "--verify", "HEAD").strip()
        tree_sha = _git("rev-parse", "--verify", "HEAD^{tree}").strip()
        src_tree_sha = _git("rev-parse", "--verify", "HEAD:src/ttakkari").strip()

        status_raw = _git("status", "--porcelain=v1", "-z", "--untracked-files=all")

        def _hash_files() -> tuple[tuple[str, str], ...]:
            paths = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
            hashes = []
            root = candidate_path.resolve()
            for rel in sorted(set(paths.split("\0")) - {""}):
                path = candidate_path / rel
                try:
                    path.resolve().relative_to(root)
                    if path.is_symlink():
                        raise CandidateBindingError("unverified_symlink", rel)
                    if not path.exists():
                        hashes.append((rel, "missing"))
                        continue
                    before = path.stat()
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    after = path.stat()
                    if (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    ) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise CandidateBindingError("snapshot_changed", rel)
                    hashes.append((rel, digest.hexdigest()))
                except (OSError, ValueError) as exc:
                    raise CandidateBindingError("snapshot_unreadable", rel) from exc
            return tuple(hashes)

        file_hashes = _hash_files()
        if (
            file_hashes != _hash_files()
            or status_raw
            != _git("status", "--porcelain=v1", "-z", "--untracked-files=all")
            or head_sha != _git("rev-parse", "--verify", "HEAD").strip()
        ):
            raise CandidateBindingError(
                "snapshot_changed", "Candidate changed during capture"
            )
        entries = [e for e in status_raw.split("\0") if e]
        dirty_source: list[str] = []
        dirty_audit: list[str] = []
        other_dirty: list[str] = []

        idx = 0
        while idx < len(entries):
            entry = entries[idx]
            status = entry[:2]
            path = entry[3:]
            if any(code in status for code in ("R", "C")) and idx + 1 < len(entries):
                # Porcelain -z puts the destination first. Classify both paths.
                original = entries[idx + 1]
                if original.startswith(SOURCE_CRITICAL_PREFIXES):
                    dirty_source.append(original)
                elif original.startswith(AUDIT_DOC_PREFIXES):
                    dirty_audit.append(original)
                else:
                    other_dirty.append(original)
                idx += 1
            if any(path == p or path.startswith(p) for p in SOURCE_CRITICAL_PREFIXES):
                dirty_source.append(path)
            elif any(path == p or path.startswith(p) for p in AUDIT_DOC_PREFIXES):
                dirty_audit.append(path)
            else:
                other_dirty.append(path)
            idx += 1

        is_clean_source = len(dirty_source) == 0 and len(other_dirty) == 0
        is_clean_audit = len(dirty_audit) == 0

        return cls(
            candidate_path=str(candidate_path.resolve()),
            candidate_head_sha=head_sha,
            candidate_tree_sha=tree_sha,
            candidate_src_tree_sha=src_tree_sha,
            dirty_source_paths=tuple(sorted(dirty_source)),
            dirty_audit_doc_paths=tuple(sorted(dirty_audit)),
            other_dirty_paths=tuple(sorted(other_dirty)),
            is_clean_source_tree=is_clean_source,
            is_clean_audit_docs=is_clean_audit,
            file_hashes=file_hashes,
        )


@dataclass(frozen=True)
class ValidationKey:
    """Fail-closed content-addressed validation key capturing full test inputs."""

    candidate_head_sha: str
    candidate_tree_sha: str
    candidate_src_tree_sha: str
    changed_files: tuple[tuple[str, str], ...]
    module_source_files: tuple[tuple[str, str], ...]
    transitive_dependency_files: tuple[tuple[str, str], ...]
    selected_test_files: tuple[tuple[str, str], ...]
    conftest_helper_files: tuple[tuple[str, str], ...]
    selector_args: tuple[str, ...]
    pyproject_sha256: str
    uv_lock_sha256: str
    python_full_version: str
    platform_tag: str
    pytest_plugins_sha256: str
    sanitized_env_sha256: str
    models_py_sha256: str
    migration_head: str
    closure_complete: bool
    has_dynamic_imports: bool
    snapshot: CandidateSnapshotIdentity | None = None
    runtime_fingerprint: str = ""

    @classmethod
    def create(
        cls,
        *,
        candidate_head_sha: str,
        candidate_tree_sha: str,
        candidate_src_tree_sha: str,
        changed_files: Mapping[str, str],
        module_source_files: Mapping[str, str],
        transitive_dependency_files: Mapping[str, str],
        selected_test_files: Mapping[str, str],
        conftest_helper_files: Mapping[str, str],
        selector_args: list[str] | tuple[str, ...],
        pyproject_sha256: str,
        uv_lock_sha256: str,
        python_full_version: str,
        platform_tag: str,
        pytest_plugins_sha256: str,
        sanitized_env_sha256: str,
        models_py_sha256: str,
        migration_head: str,
        closure_complete: bool,
        has_dynamic_imports: bool = False,
        snapshot: CandidateSnapshotIdentity | None = None,
    ) -> ValidationKey:
        return cls(
            candidate_head_sha=candidate_head_sha,
            candidate_tree_sha=candidate_tree_sha,
            candidate_src_tree_sha=candidate_src_tree_sha,
            changed_files=tuple(sorted(changed_files.items())),
            module_source_files=tuple(sorted(module_source_files.items())),
            transitive_dependency_files=tuple(
                sorted(transitive_dependency_files.items())
            ),
            selected_test_files=tuple(sorted(selected_test_files.items())),
            conftest_helper_files=tuple(sorted(conftest_helper_files.items())),
            selector_args=tuple(selector_args),
            pyproject_sha256=pyproject_sha256,
            uv_lock_sha256=uv_lock_sha256,
            python_full_version=python_full_version,
            platform_tag=platform_tag,
            pytest_plugins_sha256=pytest_plugins_sha256,
            sanitized_env_sha256=sanitized_env_sha256,
            models_py_sha256=models_py_sha256,
            migration_head=migration_head,
            closure_complete=closure_complete,
            has_dynamic_imports=has_dynamic_imports,
            snapshot=snapshot,
            runtime_fingerprint=_runtime_fingerprint(),
        )

    def compute_digest(self) -> str:
        payload = {
            "schema": "delivery-validation-key-v2",
            "snapshot": asdict(self.snapshot) if self.snapshot is not None else None,
            "runtime_fingerprint": self.runtime_fingerprint,
            "candidate_head_sha": self.candidate_head_sha,
            "candidate_tree_sha": self.candidate_tree_sha,
            "candidate_src_tree_sha": self.candidate_src_tree_sha,
            "changed_files": dict(self.changed_files),
            "module_source_files": dict(self.module_source_files),
            "transitive_dependency_files": dict(self.transitive_dependency_files),
            "selected_test_files": dict(self.selected_test_files),
            "conftest_helper_files": dict(self.conftest_helper_files),
            "selector_args": list(self.selector_args),
            "pyproject_sha256": self.pyproject_sha256,
            "uv_lock_sha256": self.uv_lock_sha256,
            "python_full_version": self.python_full_version,
            "platform_tag": self.platform_tag,
            "pytest_plugins_sha256": self.pytest_plugins_sha256,
            "sanitized_env_sha256": self.sanitized_env_sha256,
            "models_py_sha256": self.models_py_sha256,
            "migration_head": self.migration_head,
            "closure_complete": self.closure_complete,
            "has_dynamic_imports": self.has_dynamic_imports,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ValidationReceipt:
    """Signed proof of executed test stage for a specific ValidationKey."""

    key_digest: str
    verifier_id: str
    exit_code: int
    passed_count: int
    created_at_epoch: float
    ttl_seconds: int
    signature: str

    @classmethod
    def issue(
        cls,
        *,
        key_digest: str,
        verifier_id: str,
        exit_code: int,
        passed_count: int,
        created_at_epoch: float,
        ttl_seconds: int,
        secret: bytes,
    ) -> ValidationReceipt:
        payload = f"{key_digest}:{verifier_id}:{exit_code}:{passed_count}:{created_at_epoch}:{ttl_seconds}"
        sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return cls(
            key_digest=key_digest,
            verifier_id=verifier_id,
            exit_code=exit_code,
            passed_count=passed_count,
            created_at_epoch=created_at_epoch,
            ttl_seconds=ttl_seconds,
            signature=sig,
        )

    def verify_signature(self, secret: bytes) -> bool:
        payload = f"{self.key_digest}:{self.verifier_id}:{self.exit_code}:{self.passed_count}:{self.created_at_epoch}:{self.ttl_seconds}"
        expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected)

    def is_expired(self, now_epoch: float) -> bool:
        return now_epoch < self.created_at_epoch or now_epoch > (
            self.created_at_epoch + self.ttl_seconds
        )


class ValidationCachePolicy:
    """Policy governing safe reuse of passing test receipts versus fail-closed broad gate fallback."""

    @staticmethod
    def can_reuse(
        key: ValidationKey,
        receipt: ValidationReceipt,
        *,
        now_epoch: float,
        secret: bytes,
    ) -> tuple[bool, str]:
        if not key.closure_complete:
            return False, "incomplete_dependency_closure"
        if key.has_dynamic_imports:
            return False, "unverified_dynamic_imports"
        if (
            not key.runtime_fingerprint
            or key.runtime_fingerprint != _runtime_fingerprint()
        ):
            return False, "runtime_changed"
        maps = (
            key.changed_files,
            key.module_source_files,
            key.transitive_dependency_files,
            key.selected_test_files,
            key.conftest_helper_files,
        )
        if not all(maps):
            return False, "missing_dependency_inputs"
        snapshot = key.snapshot
        if snapshot is None or not snapshot.file_hashes:
            return False, "snapshot_unverified"
        if not snapshot.is_clean_source_tree or not snapshot.is_clean_audit_docs:
            return False, "candidate_dirty"
        if (
            key.candidate_head_sha,
            key.candidate_tree_sha,
            key.candidate_src_tree_sha,
        ) != (
            snapshot.candidate_head_sha,
            snapshot.candidate_tree_sha,
            snapshot.candidate_src_tree_sha,
        ):
            return False, "candidate_identity_mismatch"
        actual_hashes = dict(snapshot.file_hashes)
        if any(
            actual_hashes.get(path) != digest
            for entries in maps
            for path, digest in entries
        ):
            return False, "candidate_input_mismatch"
        try:
            current = CandidateSnapshotIdentity.capture(Path(snapshot.candidate_path))
        except CandidateBindingError:
            return False, "snapshot_unverified"
        if current != snapshot:
            return False, "candidate_changed"
        if key.compute_digest() != receipt.key_digest:
            return False, "key_digest_mismatch"
        if not receipt.verify_signature(secret):
            return False, "signature_invalid"
        if receipt.is_expired(now_epoch):
            return False, "receipt_expired"
        if receipt.exit_code != 0 or receipt.passed_count <= 0:
            return False, "receipt_failed_or_empty"
        return True, "reusable"


def collected_test_count(output: str) -> int:
    """Return pytest's explicit collected count; missing/empty collection fails closed."""
    matches = re.findall(r"collected\s+(\d+)\s+items?", output)
    if matches:
        return int(matches[-1])
    matches = re.findall(r"(?:^|\n)(\d+) tests? collected(?:\s|$)", output)
    if matches:
        return int(matches[-1])
    matches = re.findall(r"(?:^|\n)(\d+) items? collected(?:\s|$)", output)
    return int(matches[-1]) if matches else 0


def passed_test_count(output: str) -> int:
    matches = re.findall(r"\b(\d+) passed\b", output)
    return int(matches[-1]) if matches else 0


async def project_stage_validator(stage: str, candidate: Path, sha: str):
    """Run the repository's real lint/pytest gate inside the detached candidate checkout."""

    candidate_source = candidate / "src"
    if (
        not candidate.is_dir()
        or not (candidate_source / "ttakkari").is_dir()
        or len(sha) != 40
    ):
        return (
            False,
            "Candidate checkout or SHA is invalid.",
            ("candidate", "ValidationError"),
        )

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(candidate_source) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=candidate, env=env, capture_output=True, text=True, check=False
        )

    git = run(["git", "rev-parse", "HEAD"])
    if git.returncode != 0 or git.stdout.strip() != sha:
        return (
            False,
            "Candidate HEAD does not match requested tested SHA.",
            ("candidate_binding", "ValidationError"),
        )
    try:
        snapshot = CandidateSnapshotIdentity.capture(candidate)
    except CandidateBindingError as exc:
        return False, str(exc), ("candidate_binding", "ValidationError")
    if not snapshot.is_clean_source_tree or not snapshot.is_clean_audit_docs:
        return (
            False,
            "Candidate is dirty; validation requires an immutable checkout.",
            ("candidate_binding", "ValidationError"),
        )

    if stage == "stage1":
        lint = run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--no-cache",
                "src/ttakkari/delivery",
                "tests/delivery",
            ]
        )
        if lint.returncode != 0:
            return (
                False,
                (lint.stdout + lint.stderr)[-2000:] or "ruff failed without output",
                ("ruff", "LintFailure"),
            )
        pytest_args = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--timeout=120",
            "-m",
            "contract",
        ]
    elif stage == "stage2":
        pytest_args = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--timeout=120",
        ]
    else:
        return False, f"Unknown validation stage: {stage}", ("stage", "ValidationError")

    result = run(pytest_args)
    try:
        if CandidateSnapshotIdentity.capture(candidate) != snapshot:
            return (
                False,
                "Candidate changed while validation ran.",
                ("candidate_binding", "ValidationError"),
            )
    except CandidateBindingError as exc:
        return False, str(exc), ("candidate_binding", "ValidationError")
    output = (result.stdout + result.stderr)[-4000:]
    if result.returncode != 0:
        return (
            False,
            output or f"pytest exited with status {result.returncode} without output",
            ("pytest", "TestFailure"),
        )
    if passed_test_count(result.stdout + result.stderr) == 0:
        return (
            False,
            f"{stage} produced no explicit passing-test summary.",
            ("pytest_summary", "EmptyTestSuite"),
        )
    return True, f"tested_sha={sha}\n{output}", None
