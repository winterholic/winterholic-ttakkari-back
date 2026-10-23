"""Trusted transport fault driver, never imported into the production child."""

import os
import sys

from local_llm_eval import channel
from local_llm_eval.channel import collect


def exercise_channel(fault, cwd):
    """Trusted test driver injecting transport faults; never imports candidate code."""
    program = """import sys, os, json
from _llme_channel import writer, encode_frame
fd, guard = int(sys.argv[1]), int(sys.argv[2])
expected = json.loads(sys.argv[3])
send = writer(fd, guard, expected)
fault = json.load(sys.stdin)['fault']
if fault == 'close': os.close(fd)
if fault == 'replace':
    with open('replacement', 'wb') as fh: os.dup2(fh.fileno(), fd)
if fault == 'duplicate': send({})
if fault == 'missing': raise SystemExit(0)
if fault == 'stdout':
    print('OBS\\t{}')
    raise SystemExit(0)
if fault == 'malformed':
    os.write(fd, b'invalid')
    raise SystemExit(0)
send({})
if fault == 'nonzero': raise SystemExit(3)
"""
    from pathlib import Path

    source = Path(channel.__file__).read_text(encoding="utf-8")
    bootstrap = (
        "import sys,types\nm=types.ModuleType('_llme_channel')\nsys.modules['_llme_channel']=m\n"
        + f"exec({source!r},m.__dict__)\n"
        + program
    )
    return collect(
        [os.path.realpath(sys.executable), "-I", "-S", "-B", "-c", bootstrap],
        cwd,
        {"PATH": "/usr/bin:/bin"},
        {"fault": fault},
        5,
    )
