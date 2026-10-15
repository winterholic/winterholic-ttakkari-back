from ttakkari.meetings.local_stt import worker


def test_baseline_and_improved_differ_only_in_declared_options():
    baseline = worker.decode_options("baseline", ["따까리"])
    improved = worker.decode_options("improved", ["따까리"])
    assert baseline["language"] == improved["language"] == "ko"
    assert baseline["vad_filter"] is False
    assert baseline["condition_on_previous_text"] is True
    assert baseline["initial_prompt"] is None
    assert improved["vad_filter"] is True
    assert improved["condition_on_previous_text"] is False
    assert improved["initial_prompt"] == "따까리"
    assert improved["word_timestamps"] is True
    assert improved["temperature"] == 0


def test_invalid_profile_rejected():
    import pytest

    with pytest.raises(ValueError):
        worker.decode_options("unknown", [])


def test_parent_guard_exits_when_supervisor_disappears(monkeypatch):
    exits = []
    monkeypatch.setattr(worker.os, "getppid", lambda: 999)
    monkeypatch.setattr(worker.os, "_exit", exits.append)

    class Tick:
        def wait(self, _):
            return False

    worker.watch_parent(123, Tick())
    assert exits == [74]


def test_parent_guard_stops_after_request_finishes(monkeypatch):
    import threading

    stop = threading.Event()
    stop.set()

    def fail(_):
        raise AssertionError("normal completion must not force exit")

    monkeypatch.setattr(worker.os, "_exit", fail)
    worker.watch_parent(123, stop)
