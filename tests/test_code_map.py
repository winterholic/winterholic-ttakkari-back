"""작업 카드용 압축 코드 맵. 모델을 부르지 않는다."""
from pathlib import Path

from test_orchestrator import _orch, _session

from ttakkari.config import get_settings
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import code_map, ctx_ab, task_card
from ttakkari.runs.task_card import TaskCard

A_PY = '''"""주문 모듈."""

def place(order):
    validate(order)
    return store.save(order)


def validate(order):
    check(order)


class Store:
    def save(self, order):
        return place(order)
'''

B_PY = '''from a import place

def checkout(cart):
    return place(cart)
'''


def _ws(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text(A_PY, encoding="utf-8")
    (tmp_path / "b.py").write_text(B_PY, encoding="utf-8")
    (tmp_path / "note.md").write_text("# 안내", encoding="utf-8")
    return tmp_path


def test_map_has_signatures_and_one_step_callers(tmp_path):
    ws = _ws(tmp_path)
    text = code_map.build([ws / "a.py"], [ws], 6000, tmp_path / "cache")
    assert "def place(order)" in text
    assert "class Store" in text
    assert "def Store.save(" in text
    assert "b.py" in text and "checkout" in text  # 직접 호출자 1단계
    assert "호출:" in text and "불림:" in text


def test_map_is_capped_and_marks_truncation(tmp_path):
    ws = _ws(tmp_path)
    text = code_map.build([ws / "a.py", ws / "b.py"], [ws], 150, tmp_path / "cache")
    assert len(text) <= 300
    assert "생략" in text
    full = code_map.build([ws / "a.py", ws / "b.py"], [ws], 6000, tmp_path / "cache")
    assert full.startswith("[코드 맵")
    assert "a.py" in text  # 앞 파일(read_first 순) 우선


def test_language_without_parser_is_skipped_without_error(tmp_path):
    ws = _ws(tmp_path)
    (ws / "app.js").write_text("function f() { return 1; }", encoding="utf-8")
    text = code_map.build([ws / "app.js", ws / "note.md"], [ws], 6000, tmp_path / "cache")
    assert text == ""
    text = code_map.build([ws / "app.js", ws / "a.py"], [ws], 6000, tmp_path / "cache")
    assert "a.py" in text and "app.js" not in text


def test_missing_or_broken_python_is_skipped_without_error(tmp_path):
    ws = _ws(tmp_path)
    (ws / "broken.py").write_text("def f(:\n", encoding="utf-8")
    text = code_map.build([ws / "missing.py", ws / "broken.py", ws / "a.py"], [ws], 6000, tmp_path / "cache")
    assert "a.py" in text


def test_cache_reuses_parsed_index(tmp_path):
    ws = _ws(tmp_path)
    cache = tmp_path / "cache"
    first = code_map.build([ws / "a.py"], [ws], 6000, cache)
    assert (cache / "code_map.json").exists()
    (ws / "a.py").write_text(A_PY.replace("validate(order)", "validate2(order)"), encoding="utf-8")
    (ws / "a.py").write_text(A_PY, encoding="utf-8")  # 내용 해시가 같으면 다시 읽어도 같다
    assert code_map.build([ws / "a.py"], [ws], 6000, cache) == first


CARD = {
    "goal": "주문 흐름을 고친다.",
    "read_first": ["a.py"],
    "done_criteria": ["place가 검증 후 저장한다."],
}


async def _ws_session(client, auth, make_dir, files: dict[str, str]):
    root = make_dir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)},
                            headers=auth)).json()
    r = await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _usage(run: dict) -> dict:
    return run.get("usage") or {}


async def test_spawn_with_percent_zero_leaves_prompt_byte_identical(client, auth, make_dir):
    orch = await _ws_session(client, auth, make_dir, {"a.py": A_PY, "b.py": B_PY})
    h = _orch(issue_token(orch["id"]))
    r = await client.post("/api/orchestrator/workers", headers=h,
                          json={"title": "맵끔", "engine": "claude", "card": CARD})
    assert r.status_code == 201, r.text
    runs = (await client.get(f"/api/sessions/{r.json()['worker_id']}/runs", headers=auth)).json()
    assert runs[0]["prompt"] == task_card.compose("", TaskCard(**CARD))
    assert "code_map_arm" not in _usage(runs[0])


async def test_spawn_with_percent_100_injects_map_and_records_arm(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_code_map_ab_percent", 100)
    orch = await _ws_session(client, auth, make_dir, {"a.py": A_PY, "b.py": B_PY})
    h = _orch(issue_token(orch["id"]))
    r = await client.post("/api/orchestrator/workers", headers=h,
                          json={"title": "맵켬", "engine": "claude", "card": CARD})
    assert r.status_code == 201, r.text
    runs = (await client.get(f"/api/sessions/{r.json()['worker_id']}/runs", headers=auth)).json()
    assert "[코드 맵" in runs[0]["prompt"] and "def place(order)" in runs[0]["prompt"]
    assert _usage(runs[0])["code_map_arm"] == "on"
    assert _usage(runs[0])["code_map_chars"] > 0


async def test_control_arm_records_without_injection(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_code_map_ab_percent", 100)
    monkeypatch.setattr(ctx_ab, "arm", lambda *a: "control")
    orch = await _ws_session(client, auth, make_dir, {"a.py": A_PY})
    h = _orch(issue_token(orch["id"]))
    r = await client.post("/api/orchestrator/workers", headers=h,
                          json={"title": "대조", "engine": "claude", "card": CARD})
    assert r.status_code == 201, r.text
    runs = (await client.get(f"/api/sessions/{r.json()['worker_id']}/runs", headers=auth)).json()
    assert runs[0]["prompt"] == task_card.compose("", TaskCard(**CARD))
    assert _usage(runs[0])["code_map_arm"] == "control"
    assert _usage(runs[0])["code_map_chars"] == 0


async def test_arm_is_stable_between_spawn_and_follow_up(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_code_map_ab_percent", 50)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", headers=h,
                             json={"title": "안정", "engine": "claude", "card": CARD})).json()["worker_id"]
    m = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"card": CARD})
    assert m.status_code == 202, m.text
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    assert _usage(runs[0])["code_map_arm"] == _usage(runs[1])["code_map_arm"]
