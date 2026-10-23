"""B4-API-D REST endpoints (notes service). New fixture, in-process WSGI. Spec section 3.4."""

import json

from .. import codes
from ..judges import same, val
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
from notes.app import create_app


def fresh():
    data_dir = DIRS["data"]
    return create_app(data_dir), data_dir


def make(app, title, body=None):
    payload = {"title": title}
    if body is not None:
        payload["body"] = body
    return call(app, "POST", "/notes", payload)
"""
)

H1_PROBE = (
    PRE
    + """
app, _ = fresh()
rec("r1", attempt(make, app, "  First  ", "hello"))
rec("r2", attempt(make, app, "Second"))
"""
)
H2_PROBE = (
    PRE
    + """
app, _ = fresh()
for i, bad in enumerate((None, "", "   ", "x" * 81, 5, ["a"])):
    payload = {} if bad is None else {"title": bad}
    rec("t%d" % i, attempt(call, app, "POST", "/notes", payload))
rec("body_big", attempt(make, app, "ok", "b" * 1001))
rec("body_int", attempt(make, app, "ok", 7))
rec("bad_json", attempt(call, app, "POST", "/notes", raw=b"{bad"))
rec("array", attempt(call, app, "POST", "/notes", raw=b"[1, 2]"))
rec("t80", attempt(make, app, "t" * 80))
rec("b1000", attempt(make, app, "u", "b" * 1000))
"""
)
H3_PROBE = (
    PRE
    + """
app, _ = fresh()
rec("first", attempt(make, app, "Alpha"))
rec("dup_lower", attempt(make, app, "alpha"))
rec("dup_spaces", attempt(make, app, "  ALPHA  "))
rec("next", attempt(make, app, "Beta"))
"""
)
H4_PROBE = (
    PRE
    + """
app, _ = fresh()
make(app, "One", "1")
rec("ok", attempt(call, app, "GET", "/notes/1"))
for i, bad in enumerate(("/notes/2", "/notes/abc", "/notes/0", "/notes/-1", "/notes/1.5")):
    rec("bad%d" % i, attempt(call, app, "GET", bad))
"""
)
H5_PROBE = (
    PRE
    + """
app, _ = fresh()
rec("created", [attempt(make, app, "n%d" % i) for i in range(25)])
rec("default", attempt(call, app, "GET", "/notes"))
rec("page", attempt(call, app, "GET", "/notes", query="limit=5&offset=20"))
rec("max", attempt(call, app, "GET", "/notes", query="limit=100"))
rec("beyond", attempt(call, app, "GET", "/notes", query="offset=99"))
for i, q in enumerate(("limit=0", "limit=101", "limit=abc", "limit=-1")):
    rec("lim%d" % i, attempt(call, app, "GET", "/notes", query=q))
for i, q in enumerate(("offset=-1", "offset=abc")):
    rec("off%d" % i, attempt(call, app, "GET", "/notes", query=q))
"""
)
H6_PROBE = (
    PRE
    + """
import json

app1, data_dir = fresh()
path = os.path.join(data_dir, "notes.json")
rec("create1", attempt(make, app1, "Persist", "b"))
app2 = create_app(data_dir)
rec("get1", attempt(call, app2, "GET", "/notes/1"))
rec("create2", attempt(make, app2, "Second"))

real = os.replace
spy_log = []
before = open(path).read()


def spy(src, dst, *a, **k):
    try:
        src_json = json.load(open(src))
    except Exception as exc:
        src_json = "unreadable: " + type(exc).__name__
    spy_log.append({"src_json": src_json, "dst_text": open(dst).read(), "before": before})
    return real(src, dst, *a, **k)


os.replace = spy
try:
    rec("create3", attempt(make, app2, "Third"))
finally:
    os.replace = real
rec("spy", spy_log)


def boom(*a, **k):
    raise OSError("disk full")


os.replace = boom
try:
    rec("fail", attempt(make, app2, "Fourth"))
finally:
    os.replace = real
rec("after_fail_list", attempt(call, app2, "GET", "/notes"))
rec("create4", attempt(make, app2, "Fourth"))
"""
)
H7_PROBE = (
    PRE
    + """
app, _ = fresh()
rec("health", attempt(call, app, "GET", "/health"))
rec("list", attempt(call, app, "GET", "/notes"))
"""
)
H8_PROBE = (
    PRE
    + """
app, _ = fresh()
for i, (method, path) in enumerate((("POST", "/health"), ("DELETE", "/notes"), ("PUT", "/notes"), ("POST", "/notes/1"))):
    rec("m%d" % i, attempt(call, app, method, path))
rec("plain", attempt(call, app, "POST", "/notes", {"title": "a"}, content_type="text/plain"))
rec("charset", attempt(call, app, "POST", "/notes", {"title": "a"}, content_type="application/json; charset=utf-8"))
"""
)
H9_PROBE = (
    PRE
    + """
app, _ = fresh()
make(app, "a")
make(app, "b")
call(app, "GET", "/notes/1")
call(app, "POST", "/notes", raw=b"{bad")
"""
)


def _err(res, status, code, field):
    got_status, _, body = res
    same(got_status, status, "status")
    same(body, {"error": {"code": code, "field": field}}, "error body")


def _h1(o, f):
    status, headers, body = val(o, "r1")
    same(status, 201, "status")
    same(headers.get("Location"), "/notes/1", "Location")
    same(body, {"id": 1, "title": "First", "body": "hello"}, "created note")
    assert headers.get("Content-Type", "").startswith("application/json"), headers
    status, headers, body = val(o, "r2")
    same((status, body), (201, {"id": 2, "title": "Second", "body": ""}), "second note")
    same(headers.get("Location"), "/notes/2", "Location 2")


def _h2(o, f):
    for i in range(6):
        _err(val(o, f"t{i}"), 400, "invalid_title", "title")
    _err(val(o, "body_big"), 400, "invalid_body", "body")
    _err(val(o, "body_int"), 400, "invalid_body", "body")
    _err(val(o, "bad_json"), 400, "invalid_json", None)
    _err(val(o, "array"), 400, "invalid_json", None)
    same(val(o, "t80")[0], 201, "80-char title")
    same(val(o, "b1000")[0], 201, "1000-char body")


def _h3(o, f):
    same(val(o, "first")[0], 201, "first create")
    _err(val(o, "dup_lower"), 409, "duplicate_title", "title")
    _err(val(o, "dup_spaces"), 409, "duplicate_title", "title")
    status, _, body = val(o, "next")
    same((status, body["id"]), (201, 2), "failed requests must not consume an id")


def _h4(o, f):
    same(val(o, "ok")[2], {"id": 1, "title": "One", "body": "1"}, "get by id")
    for i in range(5):
        _err(val(o, f"bad{i}"), 404, "not_found", None)


def _h5(o, f):
    same([r["ok"][0] for r in o["created"]], [201] * 25, "25 creates")
    status, _, body = val(o, "default")
    same(status, 200, "default list status")
    same([n["id"] for n in body["items"]], list(range(1, 21)), "default page ids")
    same(
        (body["total"], body["limit"], body["offset"]),
        (25, 20, 0),
        "default paging fields",
    )
    _, _, page = val(o, "page")
    same([n["id"] for n in page["items"]], [21, 22, 23, 24, 25], "page ids")
    same((page["limit"], page["offset"]), (5, 20), "page fields")
    same(val(o, "max")[0], 200, "limit=100")
    same(val(o, "beyond")[2]["items"], [], "offset beyond total")
    for i in range(4):
        _err(val(o, f"lim{i}"), 400, "invalid_limit", "limit")
    for i in range(2):
        _err(val(o, f"off{i}"), 400, "invalid_offset", "offset")


def _note(note_id, title, body=""):
    return {"id": note_id, "title": title, "body": body}


def _h6(o, f):
    same(val(o, "create1")[0], 201, "create1")
    same(
        val(o, "get1")[2], _note(1, "Persist", "b"), "a new app must read earlier data"
    )
    same(val(o, "create2")[2]["id"], 2, "id continues after restart")
    same(val(o, "create3")[0], 201, "create3 with spy")
    spy = o["spy"]
    assert spy, "os.replace was never used"
    want_tmp = {
        "next_id": 4,
        "notes": [_note(1, "Persist", "b"), _note(2, "Second"), _note(3, "Third")],
    }
    same(
        spy[0]["src_json"],
        want_tmp,
        "temp file must hold the complete new state before the swap",
    )
    same(
        spy[0]["dst_text"],
        spy[0]["before"],
        "target must be untouched before os.replace",
    )
    _err(val(o, "fail"), 500, "storage_error", None)
    same(
        val(o, "after_fail_list")[2]["total"],
        3,
        "a failed write must not change the state",
    )
    same(val(o, "create4")[2]["id"], 4, "a failed write must not consume an id")
    disk = json.loads(f["dir_files"]["data"]["notes.json"])
    same(disk["next_id"], 5, "next_id on disk")
    same(
        [n["title"] for n in disk["notes"]],
        ["Persist", "Second", "Third", "Fourth"],
        "notes on disk",
    )
    same(f["dirs"]["data"], ["notes.json"], "no temp files may remain")


def _h7(o, f):
    same((val(o, "health")[0], val(o, "health")[2]), (200, {"status": "ok"}), "health")
    same(
        val(o, "list")[2],
        {"items": [], "total": 0, "limit": 20, "offset": 0},
        "empty list",
    )


def _h8(o, f):
    expected = (("GET", 0), ("GET, POST", 1), ("GET, POST", 2), ("GET", 3))
    for allow, i in expected:
        status, headers, body = val(o, f"m{i}")
        same(status, 405, f"status {i}")
        same(headers.get("Allow"), allow, f"Allow {i}")
        same(
            body, {"error": {"code": "method_not_allowed", "field": None}}, f"body {i}"
        )
    _err(val(o, "plain"), 415, "unsupported_media_type", None)
    same(val(o, "charset")[0], 201, "application/json with parameters")


def _h9(o, f):
    same(f["cwd_after"], f["cwd_before"], "files created outside data_dir")
    assert set(f["dirs"]["data"]) <= {"notes.json"}, f["dirs"]["data"]


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
    probes = [
        ("H1", codes.KIND_DELTA, H1_PROBE, _h1),
        ("H2", codes.KIND_DELTA, H2_PROBE, _h2),
        ("H3", codes.KIND_DELTA, H3_PROBE, _h3),
        ("H4", codes.KIND_DELTA, H4_PROBE, _h4),
        ("H5", codes.KIND_DELTA, H5_PROBE, _h5),
        ("H6", codes.KIND_DELTA, H6_PROBE, _h6),
        ("H7", codes.KIND_INVARIANT, H7_PROBE, _h7),
        ("H8", codes.KIND_DELTA, H8_PROBE, _h8),
        ("H9", codes.KIND_INVARIANT, H9_PROBE, _h9),
    ]
    checks = [
        Check(
            cid,
            kind,
            f"R{cid[1:]}",
            probe=probe,
            judge=judge,
            timeout=30.0,
            dirs=("data",),
        )
        for cid, kind, probe, judge in probes
    ]
    checks.append(frozen_unchanged("H10", FROZEN))
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
