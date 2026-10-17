from __future__ import annotations

from ttakkari.oplog.normalize import (
    exception_fingerprint,
    fingerprint,
    normalize_message,
    parse_traceback,
)

TB = '''Traceback (most recent call last):
  File "/Users/winterholic/development/projects/winterholic-ttakkari/back/src/ttakkari/runs/manager.py", line {line}, in _run_one
    await thing()
  File "/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/lib/python3.13/site-packages/sqlalchemy/engine.py", line 88, in execute
    raise exc
sqlalchemy.exc.OperationalError: connection to server on 127.0.0.1:5432 failed (attempt {n})
'''


def test_messages_that_differ_only_by_values_share_a_normal_form():
    a = normalize_message("release stage pytest failed (exit 7) job 3f2b8c1e-1111-4222-8333-444455556666 at 2026-10-17 10:11:12.123 sha b808e353c81d")
    b = normalize_message("release stage pytest failed (exit 12) job 9a9a9a9a-aaaa-4bbb-8ccc-ddddeeeeffff at 2026-10-16 01:02:03.999 sha 0123abcd456e")
    assert a == b
    assert "<uuid>" in a and "<sha>" in a and "<n>" in a


def test_normalize_keeps_words_and_only_first_line():
    assert normalize_message("FileNotFoundError: no /Users/winter/x\nsecond line") == "FileNotFoundError: no ~/x"
    assert normalize_message("") == ""


def test_traceback_parse_and_fingerprint_ignores_line_numbers_and_values():
    a = parse_traceback(TB.format(line=100, n=1))
    b = parse_traceback(TB.format(line=250, n=9))
    assert a.exc_type == "sqlalchemy.exc.OperationalError"
    assert a.app_frames == ["ttakkari/runs/manager.py:_run_one"]
    assert exception_fingerprint(a) == exception_fingerprint(b)


def test_different_exception_type_or_frame_splits_issues():
    a = parse_traceback(TB.format(line=1, n=1))
    other = parse_traceback(TB.format(line=1, n=1).replace("_run_one", "_other_func"))
    assert exception_fingerprint(a) != exception_fingerprint(other)
    assert fingerprint("a", "b") != fingerprint("ab")


def test_chained_exceptions_use_the_final_one():
    text = ("Traceback (most recent call last):\n  File \"/x/src/a.py\", line 1, in f\n    x\nKeyError: 'a'\n\n"
            "During handling of the above exception, another exception occurred:\n\n"
            "Traceback (most recent call last):\n  File \"/x/src/b.py\", line 2, in g\n    y\nRuntimeError: boom\n")
    t = parse_traceback(text)
    assert (t.exc_type, t.exc_message, t.frames) == ("RuntimeError", "boom", ["b.py:g"])


def test_different_root_causes_do_not_merge_under_the_same_wrapper():
    root = 'Traceback (most recent call last):\n  File "/x/src/db.py", line 12, in connect\n    x()\n{kind}: details 123\n\nThe above exception was the direct cause of the following exception:\n\n'
    wrapper = 'Traceback (most recent call last):\n  File "/x/src/job.py", line 3, in run\n    x()\nRuntimeError: job failed\n'
    a = parse_traceback(root.format(kind='ConnectionError') + wrapper)
    b = parse_traceback(root.format(kind='PermissionError') + wrapper)
    assert a.exc_type == b.exc_type == 'RuntimeError'
    assert exception_fingerprint(a) != exception_fingerprint(b)
    moved = parse_traceback((root.format(kind='ConnectionError') + wrapper).replace('line 12', 'line 99').replace('123', '789'))
    assert exception_fingerprint(a) == exception_fingerprint(moved)
