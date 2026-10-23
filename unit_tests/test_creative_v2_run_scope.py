from types import SimpleNamespace

from ttakkari.runs.manager import write_roots_for_session


def test_creative_intent_qwen_run_can_write_only_to_its_run_outbox(tmp_path):
    root = tmp_path / "workspace"
    outbox = tmp_path / "outbox"
    uploads = tmp_path / "uploads"
    inbox = tmp_path / "inbox"
    extra = tmp_path / "extra"
    temp = tmp_path / "temp"
    session = SimpleNamespace(worker_task_type="creative_intent")
    settings = SimpleNamespace(uploads_root=uploads, inbox_root=inbox)

    roots = write_roots_for_session(
        session, root=root, outbox=outbox, settings=settings,
        extra_writable_roots=[extra], tmp_dirs=[temp],
    )

    assert roots == [outbox]


def test_regular_run_keeps_existing_write_root_policy(tmp_path):
    paths = {name: tmp_path / name for name in ("workspace", "outbox", "uploads", "inbox", "extra", "temp")}
    session = SimpleNamespace(worker_task_type=None)
    settings = SimpleNamespace(uploads_root=paths["uploads"], inbox_root=paths["inbox"])

    roots = write_roots_for_session(
        session, root=paths["workspace"], outbox=paths["outbox"], settings=settings,
        extra_writable_roots=[paths["extra"]], tmp_dirs=[paths["temp"]],
    )

    assert roots == [paths[name] for name in ("workspace", "outbox", "uploads", "inbox", "extra", "temp")]
