"""Direct byte-for-byte regression checks for the basename source sample."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPLICA = Path(__file__).with_name("reverse_compare_basename_replica.py")
ORIGINAL = "/usr/bin/basename"
EXISTING_VECTORS = [
    ["report.txt"],
    ["폴더 이름/한글 파일.txt", ".txt"],
    ["folder///"],
    ["/"],
    [""],
    ["-s", ".txt", "hello world.txt"],
    ["-a", "one.txt", "two words.txt"],
    [],
    ["-z"],
]
ADDITIONAL_VECTORS = [
    ["////"],
    ["-"],
    ["--", "--"],
    ["-as", ".txt", "file.txt"],
    ["-s"],
    ["-s", "foo", "foo"],
    ["-s", ".txt", "one.txt", "two.txt"],
]


def _run(command: list[str], argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command + argv, capture_output=True, check=False, timeout=3)


@pytest.mark.parametrize("argv", EXISTING_VECTORS, ids=["existing-" + str(i) for i in range(9)])
def test_existing_vectors_match_original_bytes_and_exit(argv: list[str]) -> None:
    original = _run([ORIGINAL], argv)
    replica = _run([sys.executable, str(REPLICA)], argv)

    assert (replica.stdout, replica.stderr, replica.returncode) == (
        original.stdout,
        original.stderr,
        original.returncode,
    )


@pytest.mark.parametrize("argv", ADDITIONAL_VECTORS, ids=["additional-" + str(i) for i in range(7)])
def test_additional_vectors_match_original_bytes_and_exit(argv: list[str]) -> None:
    original = _run([ORIGINAL], argv)
    replica = _run([sys.executable, str(REPLICA)], argv)

    assert (replica.stdout, replica.stderr, replica.returncode) == (
        original.stdout,
        original.stderr,
        original.returncode,
    )
