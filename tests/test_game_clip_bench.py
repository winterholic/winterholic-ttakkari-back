"""G31 clip_bench 판정 함수의 거짓 통과 방지 회귀 테스트(소켓·worker 없이)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "game_smoke"))
import clip_bench as cb  # noqa: E402


def _call(seq, name, inp, cid):
    return {"seq": seq, "type": "tool.call", "payload": {"call_id": cid, "name": name, "input": inp}}


def _res(seq, cid, err=False):
    return {"seq": seq, "type": "tool.result", "payload": {"call_id": cid, "is_error": err, "output": ""}}


BAD = {"clip-03.jpg", "clip-04.jpg"}


def test_read_of_bad_frame_before_edit_counts():
    ev = [_call(1, "Read", {"file_path": "/g/.ttakkari-feedback/a/clip-04.jpg"}, "c1"), _res(2, "c1"),
          _call(3, "Edit", {"file_path": "/g/src/main.ts"}, "c2")]
    r = cb.clip_evidence(ev, BAD)
    assert r["opened"] == ["clip-04.jpg"] and r["opened_bad_frame"] and r["read_before_first_edit"]


def test_ls_or_grep_mentioning_clip_is_not_opening():
    ev = [_call(1, "Bash", {"command": "cat .ttakkari-feedback/a/clip-03.jpg | wc -c"}, "c1"), _res(2, "c1")]
    r = cb.clip_evidence(ev, BAD)
    assert r["opened"] == [] and r["mentioned_only"] == ["clip-03.jpg"]
    assert not r["opened_bad_frame"]


def test_failed_read_or_read_after_edit_does_not_count():
    failed = [_call(1, "Read", {"file_path": "/g/clip-03.jpg"}, "c1"), _res(2, "c1", err=True)]
    assert cb.clip_evidence(failed, BAD)["opened"] == []
    late = [_call(1, "Edit", {"file_path": "/g/src/main.ts"}, "c0"), _call(2, "Read", {"file_path": "/g/clip-03.jpg"}, "c1"), _res(3, "c1")]
    r = cb.clip_evidence(late, BAD)
    assert r["opened_bad_frame"] and not r["read_before_first_edit"]


def test_bash_edit_before_read_is_detected():
    py = "python3 - <<'E'\ns=open('src/main.ts').read()\ns=s.replace('a','b')\nE"
    ev = [_call(1, "Bash", {"command": py}, "c0"), _res(2, "c0"), _call(3, "Read", {"file_path": "/g/clip-03.jpg"}, "c1"), _res(4, "c1")]
    assert not cb.clip_evidence(ev, BAD)["read_before_first_edit"]
    ro = [_call(1, "Bash", {"command": "git ls-files && cat .ttakkari-feedback/a/feedback.json"}, "c0"), _res(2, "c0"),
          _call(3, "Read", {"file_path": "/g/clip-03.jpg"}, "c1"), _res(4, "c1")]
    assert cb.clip_evidence(ro, BAD)["read_before_first_edit"]


def test_only_ok_frames_opened_is_not_bad_frame():
    ev = [_call(1, "Read", {"file_path": "/g/clip-01.jpg"}, "c1"), _res(2, "c1")]
    assert not cb.clip_evidence(ev, BAD)["opened_bad_frame"]


def _scan(ball_cx, red=400, blue=0, green=0):
    return {"red": red, "blue": blue, "green": green, "ball_cx": ball_cx}


def test_post_fixed_requires_full_traverse():
    full = [_scan(0.1), _scan(0.5), _scan(0.9)]
    assert cb.post_fixed("color", full)
    # 결함 임계를 뒤로 밀어 짧은 녹화 안에서만 안 보이게 한 경우: 녹화가 오른쪽 끝에 닿지 못하면 통과시키지 않는다
    short = [_scan(0.1), _scan(0.4), _scan(0.6)]
    assert not cb.post_fixed("color", short)
    assert not cb.post_fixed("color", [_scan(0.1), _scan(0.5), _scan(0.9, blue=400)])


def test_pick_frames_needs_two_ok_two_bad_in_order():
    scans = [_scan(0.1), _scan(0.2), _scan(0.7, red=0, blue=400), _scan(0.9, red=0, blue=400)]
    picks, bad = cb.pick_frames("color", scans)
    assert picks == [0, 1, 2, 3] and bad == {2, 3}
    assert cb.pick_frames("color", [_scan(0.1), _scan(0.2)])[0] == []


def test_isolation_guard_blocks_non_isolated_env(monkeypatch):
    import pytest
    monkeypatch.delenv("TTAKKARI_NO_ENV_FILE", raising=False)
    with pytest.raises(SystemExit):
        cb.guard_isolation()
