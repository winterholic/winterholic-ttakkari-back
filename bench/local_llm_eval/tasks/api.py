"""B4-API-D REST endpoints (notes service). New fixture, in-process WSGI. Spec section 3.4."""

from .. import codes
from ..model import Check, Stage, Task
from ..natives import frozen_unchanged, public_suite
from ..negatives import neg, presolved_negative, standard_negatives
from . import api_fixture as fx
from ._common import check_unique, single_stage, with_paths

REQUIRED = ("notes/app.py",)
FROZEN = ("tests/*",)

PRE = (
    fx.WSGI_HELPER
    + """
import os
import tempfile
from notes.app import create_app


def fresh():
    data_dir = tempfile.mkdtemp()
    return create_app(data_dir), data_dir


def err(res, status, code, field):
    got_status, _, body = res
    assert got_status == status, (got_status, body)
    assert body == {"error": {"code": code, "field": field}}, body


def make(app, title, body=None):
    payload = {"title": title}
    if body is not None:
        payload["body"] = body
    return call(app, "POST", "/notes", payload)
"""
)

H1 = """
app, _ = fresh()
status, headers, body = make(app, "  First  ", "hello")
assert status == 201, (status, body)
assert headers.get("Location") == "/notes/1", headers
assert body == {"id": 1, "title": "First", "body": "hello"}, body
status, headers, body = make(app, "Second")
assert status == 201 and body == {"id": 2, "title": "Second", "body": ""}, body
assert headers.get("Location") == "/notes/2"
assert headers.get("Content-Type", "").startswith("application/json")
"""

H2 = """
app, _ = fresh()
for bad in (None, "", "   ", "x" * 81, 5, ["a"]):
    payload = {} if bad is None else {"title": bad}
    err(call(app, "POST", "/notes", payload), 400, "invalid_title", "title")
err(make(app, "ok", "b" * 1001), 400, "invalid_body", "body")
err(make(app, "ok", 7), 400, "invalid_body", "body")
err(call(app, "POST", "/notes", raw=b"{bad"), 400, "invalid_json", None)
err(call(app, "POST", "/notes", raw=b"[1, 2]"), 400, "invalid_json", None)
assert make(app, "t" * 80)[0] == 201
assert make(app, "u", "b" * 1000)[0] == 201
"""

H3 = """
app, _ = fresh()
assert make(app, "Alpha")[0] == 201
err(make(app, "alpha"), 409, "duplicate_title", "title")
err(make(app, "  ALPHA  "), 409, "duplicate_title", "title")
status, _, body = make(app, "Beta")
assert status == 201 and body["id"] == 2, body
"""

H4 = """
app, _ = fresh()
make(app, "One", "1")
assert call(app, "GET", "/notes/1")[2] == {"id": 1, "title": "One", "body": "1"}
for bad in ("/notes/2", "/notes/abc", "/notes/0", "/notes/-1", "/notes/1.5"):
    err(call(app, "GET", bad), 404, "not_found", None)
"""

H5 = """
app, _ = fresh()
for i in range(25):
    assert make(app, "n%d" % i)[0] == 201
status, _, body = call(app, "GET", "/notes")
assert status == 200 and [n["id"] for n in body["items"]] == list(range(1, 21)), body
assert (body["total"], body["limit"], body["offset"]) == (25, 20, 0)
status, _, body = call(app, "GET", "/notes", query="limit=5&offset=20")
assert [n["id"] for n in body["items"]] == [21, 22, 23, 24, 25] and body["limit"] == 5 and body["offset"] == 20
assert call(app, "GET", "/notes", query="limit=100")[0] == 200
assert call(app, "GET", "/notes", query="offset=99")[2]["items"] == []
for q in ("limit=0", "limit=101", "limit=abc", "limit=-1"):
    err(call(app, "GET", "/notes", query=q), 400, "invalid_limit", "limit")
for q in ("offset=-1", "offset=abc"):
    err(call(app, "GET", "/notes", query=q), 400, "invalid_offset", "offset")
"""

H6 = """
import json
import os

app1, data_dir = fresh()
assert make(app1, "Persist", "b")[0] == 201
path = os.path.join(data_dir, "notes.json")
data = json.load(open(path))
assert set(data) >= {"next_id", "notes"}, data
app2 = create_app(data_dir)
assert call(app2, "GET", "/notes/1")[2] == {"id": 1, "title": "Persist", "body": "b"}
assert make(app2, "Second")[2]["id"] == 2

# os.replace must be used, with a complete temp file and an untouched target at that moment
real = os.replace
seen = []


def spy(src, dst, *a, **k):
    assert json.load(open(src)), "temp file must hold complete JSON before the swap"
    assert open(dst).read() == before, "target changed before os.replace"
    seen.append(src)
    return real(src, dst, *a, **k)


before = open(path).read()
os.replace = spy
try:
    assert make(app2, "Third")[0] == 201
finally:
    os.replace = real
assert seen, "os.replace was never used"

# a failing replace leaves file and state untouched and consumes no id
before = open(path).read()


def boom(*a, **k):
    raise OSError("disk full")


os.replace = boom
try:
    err(make(app2, "Fourth"), 500, "storage_error", None)
finally:
    os.replace = real
assert open(path).read() == before
assert call(app2, "GET", "/notes")[2]["total"] == 3
status, _, body = make(app2, "Fourth")
assert status == 201 and body["id"] == 4, body
leftovers = [n for n in os.listdir(data_dir) if n != "notes.json"]
assert not leftovers, leftovers
"""

H7 = """
app, _ = fresh()
assert call(app, "GET", "/health") [:1] == (200,) and call(app, "GET", "/health")[2] == {"status": "ok"}
assert call(app, "GET", "/notes")[2] == {"items": [], "total": 0, "limit": 20, "offset": 0}
"""

H8 = """
app, _ = fresh()
for method, path, allow in (
    ("POST", "/health", "GET"),
    ("DELETE", "/notes", "GET, POST"),
    ("PUT", "/notes", "GET, POST"),
    ("POST", "/notes/1", "GET"),
):
    status, headers, body = call(app, method, path)
    assert status == 405, (method, path, status)
    assert headers.get("Allow") == allow, headers
    assert body == {"error": {"code": "method_not_allowed", "field": None}}, body
err(call(app, "POST", "/notes", {"title": "a"}, content_type="text/plain"), 415, "unsupported_media_type", None)
assert call(app, "POST", "/notes", {"title": "a"}, content_type="application/json; charset=utf-8")[0] == 201
"""

H9 = """
import os

before = sorted(os.listdir("."))
app, data_dir = fresh()
make(app, "a")
make(app, "b")
call(app, "GET", "/notes/1")
call(app, "POST", "/notes", raw=b"{bad")
assert sorted(os.listdir(".")) == before, sorted(os.listdir("."))
extra = [n for n in os.listdir(data_dir) if n != "notes.json"]
assert not extra, extra
"""


def _gold(base):
    return with_paths(base, {"notes/app.py": fx.GOLD_APP})


def _alt(base):
    return with_paths(base, {"notes/app.py": fx.ALT_APP})


def build() -> Task:
    base = {
        "notes/__init__.py": "",
        "notes/app.py": fx.BASE_APP,
        "tests/__init__.py": "",
        "tests/wsgi_helper.py": fx.WSGI_HELPER,
        "tests/test_app.py": fx.PUBLIC_TESTS,
    }
    snippets = [
        ("H1", codes.KIND_DELTA, H1),
        ("H2", codes.KIND_DELTA, H2),
        ("H3", codes.KIND_DELTA, H3),
        ("H4", codes.KIND_DELTA, H4),
        ("H5", codes.KIND_DELTA, H5),
        ("H6", codes.KIND_DELTA, H6),
        ("H7", codes.KIND_INVARIANT, H7),
        ("H8", codes.KIND_DELTA, H8),
        ("H9", codes.KIND_INVARIANT, H9),
    ]
    checks = [
        Check(cid, kind, f"R{cid[1:]}", snippet=PRE + code, timeout=30.0)
        for cid, kind, code in snippets
    ]
    checks.append(frozen_unchanged("H10", FROZEN, "R10"))
    checks.append(public_suite("PUB", 3, codes.KIND_INVARIANT))
    stage = Stage(
        "S1",
        change_class=codes.CHANGE_DIFF,
        prompt=fx.PROMPT,
        checks=checks,
        required_change_paths=REQUIRED,
    )
    negatives = standard_negatives(required=REQUIRED, frozen_test="tests/test_app.py")
    negatives.append(presolved_negative())

    def variant(old, new):
        def run(prev, gold, base_):
            out = dict(gold)
            assert old in out["notes/app.py"], old
            out["notes/app.py"] = out["notes/app.py"].replace(old, new)
            return out

        return run

    def hardcoded(prev, gold, base_):
        out = dict(prev)
        out["notes/app.py"] = fx.BASE_APP.replace(
            '        return _json(start_response, "404 Not Found"',
            '        if method == "POST":\n'
            '            return _json(start_response, "201 Created", {"id": 1, "title": "x", "body": ""})\n'
            '        return _json(start_response, "404 Not Found"',
        )
        return out

    def broken_list(prev, gold, base_):
        out = dict(gold)
        out["notes/app.py"] = out["notes/app.py"].replace(
            '"total": len(store.notes), ', ""
        )
        return out

    negatives += [
        neg(
            "N3",
            "memory only: nothing is written to notes.json",
            variant(
                "            os.replace(tmp, self.path)", "            os.unlink(tmp)"
            ),
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N3",
            "non-atomic: writes notes.json directly, never calls os.replace",
            variant(
                "            os.replace(tmp, self.path)",
                "            os.unlink(tmp)\n            open(self.path, 'w').write(json.dumps({'next_id': next_id, 'notes': notes}))",
            ),
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N3",
            "unknown note id answered with 400 instead of 404",
            variant(
                '            if note is None:\n                return _error(start_response, 404, "not_found")',
                '            if note is None:\n                return _error(start_response, 400, "not_found")',
            ),
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N3",
            "duplicate titles accepted",
            variant(
                '        if any(n["title"].lower() == title.lower() for n in store.notes):',
                "        if False:",
            ),
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N3",
            "failed request consumes an id",
            variant(
                "        self._write(self.next_id + 1, notes)\n        self.next_id += 1",
                "        self.next_id += 1\n        self._write(self.next_id, notes)",
            ),
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N3",
            "response bodies hard-coded for POST",
            hardcoded,
            ("FAIL_HIDDEN_ONLY",),
        ),
        neg(
            "N3",
            "existing GET /notes format broken (total removed)",
            broken_list,
            ("FAIL_PUBLIC",),
        ),
    ]
    return check_unique(
        Task(
            id="B4-API-D",
            type="rest",
            prompt=fx.PROMPT,
            base_files=base,
            stages=[stage],
            frozen_paths=FROZEN,
            gold=single_stage(_gold),
            alt_gold=single_stage(_alt),
            negatives=negatives,
            notes="new fixture; in-process WSGI, no sockets",
        )
    )
