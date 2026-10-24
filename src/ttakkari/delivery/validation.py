"""Project backed validation for immutable delivery candidates."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
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

    @classmethod
    def capture(cls, candidate_path: Path) -> CandidateSnapshotIdentity:
        """Capture git identity and working-tree status fail-closed from candidate checkout."""
        if not candidate_path.is_dir() or not (candidate_path / ".git").exists():
            raise CandidateBindingError("invalid_candidate", f"Not a git checkout: {candidate_path}")

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

        status_raw = _git("status", "--porcelain=v1", "-z")
        entries = [e for e in status_raw.split("\0") if e]
        dirty_source: list[str] = []
        dirty_audit: list[str] = []
        other_dirty: list[str] = []

        idx = 0
        while idx < len(entries):
            entry = entries[idx]
            status = entry[:2]
            path = entry[3:]
            if status.startswith(("R", "C")) and idx + 1 < len(entries):
                idx += 1
                path = entries[idx]
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
    ) -> ValidationKey:
        return cls(
            candidate_head_sha=candidate_head_sha,
            candidate_tree_sha=candidate_tree_sha,
            candidate_src_tree_sha=candidate_src_tree_sha,
            changed_files=tuple(sorted(changed_files.items())),
            module_source_files=tuple(sorted(module_source_files.items())),
            transitive_dependency_files=tuple(sorted(transitive_dependency_files.items())),
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
        )

    def compute_digest(self) -> str:
        payload = {
            "schema": "delivery-validation-key-v1",
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
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
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
        return now_epoch < self.created_at_epoch or now_epoch > (self.created_at_epoch + self.ttl_seconds)


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
    if not candidate.is_dir() or not (candidate_source / "ttakkari").is_dir() or len(sha) != 40:
        return False, "Candidate checkout or SHA is invalid.", ("candidate", "ValidationError")

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(candidate_source) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=candidate, env=env, capture_output=True, text=True, check=False)

    git = run(["git", "rev-parse", "HEAD"])
    if git.returncode != 0 or git.stdout.strip() != sha:
        return False, "Candidate HEAD does not match requested tested SHA.", ("candidate_binding", "ValidationError")

    if stage == "stage1":
        lint = run([sys.executable, "-m", "ruff", "check", "src/ttakkari/delivery", "tests/delivery"])
        if lint.returncode != 0:
            return False, (lint.stdout + lint.stderr)[-2000:] or "ruff failed without output", ("ruff", "LintFailure")
        pytest_args = [sys.executable, "-m", "pytest", "-q", "--timeout=120", "-m", "contract"]
    elif stage == "stage2":
        pytest_args = [sys.executable, "-m", "pytest", "-q", "--timeout=120"]
    else:
        return False, f"Unknown validation stage: {stage}", ("stage", "ValidationError")

    result = run(pytest_args)
    output = (result.stdout + result.stderr)[-4000:]
    if result.returncode != 0:
        return False, output or f"pytest exited with status {result.returncode} without output", ("pytest", "TestFailure")
    if passed_test_count(result.stdout + result.stderr) == 0:
        return False, f"{stage} produced no explicit passing-test summary.", ("pytest_summary", "EmptyTestSuite")
    return True, f"tested_sha={sha}\n{output}", None
