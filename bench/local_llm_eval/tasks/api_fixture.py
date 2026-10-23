"""Fixture text for B4-API-D: starter notes service, public tests, contract prompt, references.

New fixture (no earlier one existed). The service is an in-process WSGI callable; the evaluator
drives it directly, so no socket or port is opened. Spec section 3.4.
"""

PROMPT = """notes 패키지는 작은 메모 서비스(WSGI)다. 지금은 GET /health 와 비어 있는 GET /notes 만 동작한다.
notes/app.py 의 create_app(data_dir) 가 돌려주는 WSGI 앱에 아래 계약을 모두 구현하라.
tests/ 의 기존 파일은 수정하지 마라. 표준 라이브러리만 쓴다.

모든 응답 본문은 JSON(Content-Type: application/json)이다. 오류 본문은 {"error": {"code": <문자열>, "field": <문자열 또는 null>}} 이다.

| 요청 | 조건 | 응답 |
|---|---|---|
| POST /notes | Content-Type 이 application/json 이 아님(charset 같은 매개변수는 허용) | 415, code unsupported_media_type, field null |
| POST /notes | 본문이 JSON 이 아니거나 JSON 객체가 아님 | 400, code invalid_json, field null |
| POST /notes | title 이 없거나 문자열이 아니거나 앞뒤 공백을 제거한 길이가 1~80 이 아님 | 400, code invalid_title, field title |
| POST /notes | body 가 있는데 문자열이 아니거나 1000자 초과(없으면 빈 문자열) | 400, code invalid_body, field body |
| POST /notes | 앞뒤 공백 제거 후 대소문자 무시로 같은 title 이 이미 있음 | 409, code duplicate_title, field title |
| POST /notes | 성공 | 201, 헤더 Location: /notes/{id}, 본문 {"id", "title"(공백 제거), "body"} |
| GET /notes/{id} | 존재 | 200, 위와 같은 객체 |
| GET /notes/{id} | 없거나 id 가 양의 정수가 아님 | 404, code not_found, field null |
| GET /notes | limit(기본 20, 1~100), offset(기본 0, 0 이상) | 200, {"items": [id 오름차순], "total": 전체 개수, "limit": L, "offset": O} |
| GET /notes | limit 이 정수가 아니거나 범위 밖 | 400, code invalid_limit, field limit |
| GET /notes | offset 이 정수가 아니거나 음수 | 400, code invalid_offset, field offset |
| 알려진 경로에 지원하지 않는 메서드 | /health 는 GET, /notes 는 GET 과 POST, /notes/{id} 는 GET 만 허용 | 405, code method_not_allowed, field null, 헤더 Allow: 허용 메서드를 ", " 로 이은 문자열(예: "GET, POST") |
| 그 밖의 경로 | | 404, code not_found, field null |

id 는 1부터 시작해 성공한 생성마다 1씩 늘어난다(실패한 요청은 id 를 소모하지 않는다).

저장: 데이터는 data_dir/notes.json 에 {"next_id": N, "notes": [...]} 로 저장한다. 같은 data_dir 로 만든 새 앱은 이전 데이터를 그대로 읽는다.
쓰기는 같은 디렉터리의 임시 파일에 완전한 JSON 을 쓴 뒤 os.replace 로 notes.json 을 교체한다. 저장이 OSError 로 실패하면 500, code storage_error, field null 을 돌려주고, 파일과 앱 상태는 요청 이전과 같아야 하며 임시 파일을 남기지 않는다.
data_dir 밖에는 아무 파일도 만들지 않는다. 기존 GET /health 와 GET /notes 의 응답 형식은 바꾸지 않는다.
"""

BASE_APP = '''"""Notes service (WSGI). Starter: only /health and an empty GET /notes work."""
import json


def _json(start_response, status, body, headers=()):
    payload = json.dumps(body).encode("utf-8")
    start_response(
        status,
        [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))] + list(headers),
    )
    return [payload]


def create_app(data_dir):
    def app(environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "")
        if path == "/health" and method == "GET":
            return _json(start_response, "200 OK", {"status": "ok"})
        if path == "/notes" and method == "GET":
            return _json(start_response, "200 OK", {"items": [], "total": 0, "limit": 20, "offset": 0})
        return _json(start_response, "404 Not Found", {"error": {"code": "not_found", "field": None}})

    return app
'''

WSGI_HELPER = '''"""Calls a WSGI app in-process (no sockets)."""
import io
import json


def call(app, method, path, body=None, query="", content_type="application/json", raw=None):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else b"")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(data)),
        "wsgi.input": io.BytesIO(data),
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
    }
    seen = {}

    def start_response(status, headers, exc_info=None):
        seen["status"] = status
        seen["headers"] = dict(headers)

    chunks = app(environ, start_response)
    payload = b"".join(chunks)
    return int(seen["status"].split()[0]), seen["headers"], json.loads(payload) if payload else None
'''

PUBLIC_TESTS = """import tempfile
import unittest

from notes.app import create_app

from .wsgi_helper import call


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(tempfile.mkdtemp())

    def test_health(self):
        status, _, body = call(self.app, "GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))

    def test_empty_list(self):
        status, _, body = call(self.app, "GET", "/notes")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"items": [], "total": 0, "limit": 20, "offset": 0})

    def test_unknown_path(self):
        status, _, body = call(self.app, "GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
"""

GOLD_APP = '''"""Notes service (WSGI) with validation and atomic persistence."""
import json
import os
import re
import tempfile
from urllib.parse import parse_qs

TITLE_MAX = 80
BODY_MAX = 1000
STATUS = {
    200: "200 OK",
    201: "201 Created",
    400: "400 Bad Request",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    409: "409 Conflict",
    415: "415 Unsupported Media Type",
    500: "500 Internal Server Error",
}


def _json(start_response, code, body, headers=()):
    payload = json.dumps(body).encode("utf-8")
    start_response(
        STATUS[code],
        [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))] + list(headers),
    )
    return [payload]


def _error(start_response, code, error, field=None, headers=()):
    return _json(start_response, code, {"error": {"code": error, "field": field}}, headers)


class _Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.path = os.path.join(data_dir, "notes.json")
        data = {"next_id": 1, "notes": []}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        self.next_id = data["next_id"]
        self.notes = data["notes"]

    def _write(self, next_id, notes):
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".notes-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"next_id": next_id, "notes": notes}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def add(self, title, body):
        note = {"id": self.next_id, "title": title, "body": body}
        notes = self.notes + [note]
        self._write(self.next_id + 1, notes)
        self.next_id += 1
        self.notes = notes
        return note


def _int_param(values, name, default, low, high=None):
    raw = values.get(name, [str(default)])[0]
    try:
        number = int(raw)
    except ValueError:
        return None
    if number < low or (high is not None and number > high):
        return None
    return number


def create_app(data_dir):
    store = _Store(data_dir)

    def app(environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "")
        item = re.fullmatch(r"/notes/([^/]+)", path)
        if path == "/health":
            allowed = ("GET",)
        elif path == "/notes":
            allowed = ("GET", "POST")
        elif item:
            allowed = ("GET",)
        else:
            return _error(start_response, 404, "not_found")
        if method not in allowed:
            return _error(
                start_response, 405, "method_not_allowed", headers=[("Allow", ", ".join(allowed))]
            )
        if path == "/health":
            return _json(start_response, 200, {"status": "ok"})
        if item:
            raw = item.group(1)
            note = None
            if raw.isdigit() and int(raw) > 0:
                note = next((n for n in store.notes if n["id"] == int(raw)), None)
            if note is None:
                return _error(start_response, 404, "not_found")
            return _json(start_response, 200, note)
        if method == "GET":
            query = parse_qs(environ.get("QUERY_STRING", ""))
            limit = _int_param(query, "limit", 20, 1, 100)
            if limit is None:
                return _error(start_response, 400, "invalid_limit", "limit")
            offset = _int_param(query, "offset", 0, 0)
            if offset is None:
                return _error(start_response, 400, "invalid_offset", "offset")
            items = store.notes[offset : offset + limit]
            return _json(
                start_response,
                200,
                {"items": items, "total": len(store.notes), "limit": limit, "offset": offset},
            )
        content_type = environ.get("CONTENT_TYPE", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return _error(start_response, 415, "unsupported_media_type")
        length = int(environ.get("CONTENT_LENGTH") or 0)
        try:
            data = json.loads(environ["wsgi.input"].read(length) or b"null")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return _error(start_response, 400, "invalid_json")
        title = data.get("title")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= TITLE_MAX:
            return _error(start_response, 400, "invalid_title", "title")
        body = data.get("body", "")
        if not isinstance(body, str) or len(body) > BODY_MAX:
            return _error(start_response, 400, "invalid_body", "body")
        title = title.strip()
        if any(n["title"].lower() == title.lower() for n in store.notes):
            return _error(start_response, 409, "duplicate_title", "title")
        try:
            note = store.add(title, body)
        except OSError:
            return _error(start_response, 500, "storage_error")
        return _json(start_response, 201, note, [("Location", "/notes/%d" % note["id"])])

    return app
'''

ALT_APP = '''"""Notes service (WSGI): table-driven routing, class based store."""
import json
import os
import tempfile
from http import HTTPStatus
from urllib.parse import parse_qs


def respond(start_response, status, body, extra=()):
    data = json.dumps(body).encode()
    phrase = HTTPStatus(status).phrase
    start_response(
        "%d %s" % (status, phrase),
        [("Content-Type", "application/json"), ("Content-Length", str(len(data))), *extra],
    )
    return [data]


def fail(start_response, status, code, field=None, extra=()):
    return respond(start_response, status, {"error": {"code": code, "field": field}}, extra)


class Notes:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.file = os.path.join(data_dir, "notes.json")
        self.state = {"next_id": 1, "notes": []}
        if os.path.isfile(self.file):
            with open(self.file, encoding="utf-8") as handle:
                self.state = json.load(handle)

    def persist(self, state):
        tmp = tempfile.NamedTemporaryFile("w", dir=self.data_dir, suffix=".tmp", delete=False, encoding="utf-8")
        try:
            json.dump(state, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self.file)
        except OSError:
            tmp.close()
            if os.path.exists(tmp.name):
                os.remove(tmp.name)
            raise
        self.state = state

    def create(self, title, body):
        note = {"id": self.state["next_id"], "title": title, "body": body}
        self.persist({"next_id": note["id"] + 1, "notes": [*self.state["notes"], note]})
        return note

    def find(self, text):
        if not text.isdecimal() or int(text) < 1:
            return None
        return next((n for n in self.state["notes"] if n["id"] == int(text)), None)


def create_app(data_dir):
    notes = Notes(data_dir)

    def health(env, sr, method, rest):
        return respond(sr, 200, {"status": "ok"})

    def collection(env, sr, method, rest):
        if method == "POST":
            return post(env, sr)
        query = parse_qs(env.get("QUERY_STRING", ""))
        spec = (("limit", 20, 1, 100, "invalid_limit"), ("offset", 0, 0, None, "invalid_offset"))
        values = {}
        for name, default, low, high, code in spec:
            try:
                number = int(query.get(name, [default])[0])
            except ValueError:
                return fail(sr, 400, code, name)
            if number < low or (high is not None and number > high):
                return fail(sr, 400, code, name)
            values[name] = number
        window = notes.state["notes"][values["offset"] : values["offset"] + values["limit"]]
        return respond(sr, 200, {"items": window, "total": len(notes.state["notes"]), **values})

    def post(env, sr):
        if env.get("CONTENT_TYPE", "").partition(";")[0].strip().lower() != "application/json":
            return fail(sr, 415, "unsupported_media_type")
        size = int(env.get("CONTENT_LENGTH") or 0)
        try:
            payload = json.loads(env["wsgi.input"].read(size))
        except ValueError:
            return fail(sr, 400, "invalid_json")
        if not isinstance(payload, dict):
            return fail(sr, 400, "invalid_json")
        title = payload.get("title")
        if not isinstance(title, str) or len(title.strip()) not in range(1, 81):
            return fail(sr, 400, "invalid_title", "title")
        body = payload.get("body", "")
        if not isinstance(body, str) or len(body) > 1000:
            return fail(sr, 400, "invalid_body", "body")
        title = title.strip()
        if title.casefold() in {n["title"].casefold() for n in notes.state["notes"]}:
            return fail(sr, 409, "duplicate_title", "title")
        try:
            note = notes.create(title, body)
        except OSError:
            return fail(sr, 500, "storage_error")
        return respond(sr, 201, note, [("Location", f"/notes/{note['id']}")])

    def single(env, sr, method, rest):
        note = notes.find(rest)
        return respond(sr, 200, note) if note else fail(sr, 404, "not_found")

    def app(env, sr):
        method, path = env["REQUEST_METHOD"], env.get("PATH_INFO", "")
        routes = {"/health": (health, "GET"), "/notes": (collection, "GET, POST")}
        if path in routes:
            handler, allowed = routes[path]
            rest = ""
        elif path.startswith("/notes/") and "/" not in path[7:]:
            handler, allowed, rest = single, "GET", path[7:]
        else:
            return fail(sr, 404, "not_found")
        if method not in allowed.split(", "):
            return fail(sr, 405, "method_not_allowed", extra=[("Allow", allowed)])
        return handler(env, sr, method, rest)

    return app
'''
