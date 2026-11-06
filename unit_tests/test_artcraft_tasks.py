"""ArtCraft 작업 단위 테스트: sweep_outputs, 카탈로그 CLI 완전성, 이전 결과 누락 감지, 심볼릭 링크/민감 파일 격리."""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from ttakkari import artcraft_catalog as catalog
from ttakkari import artcraft_tasks
from ttakkari.artcraft_tasks import RunPlan


@dataclass
class DummyRun:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: any = None
    created_at: any = None
    prompt: str = "테스트 지시"
    context_artifact_ids: list = None
    usage: dict = None

    def __post_init__(self):
        if self.started_at is None:
            self.started_at = datetime.fromtimestamp(time.time() - 10, UTC)
        if self.created_at is None:
            self.created_at = self.started_at


def test_sweep_outputs_filters_zero_byte_empty_files(tmp_path: Path):
    """0바이트 빈 파일은 유효한 결과물이 아니므로 outbox로 복사되지 않아야 한다."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    empty_file = out_dir / "empty.txt"
    empty_file.write_bytes(b"")

    plan = RunPlan(prompt="", note="", mcp_servers={}, out_dir=out_dir)
    run = DummyRun()

    copied = artcraft_tasks.sweep_outputs(plan, run, outbox)
    assert copied == 0
    assert not (outbox / "empty.txt").exists()


def test_sweep_outputs_rejects_symlink_directory_escape(tmp_path: Path):
    """out 디렉토리 하위의 심볼릭 링크 디렉토리를 통한 작업 폴더 외부 탈출을 차단해야 한다."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("TOP_SECRET")
    now = time.time()
    os.utime(secret, (now, now))

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    # out 하위에 외부를 가리키는 심볼릭 링크 디렉토리 생성
    sym_dir = out_dir / "escaped_dir"
    sym_dir.symlink_to(outside, target_is_directory=True)

    plan = RunPlan(prompt="", note="", mcp_servers={}, out_dir=out_dir)
    run = DummyRun()

    copied = artcraft_tasks.sweep_outputs(plan, run, outbox)
    assert copied == 0
    assert not (outbox / "escaped_dir" / "secret.txt").exists()
    assert not (outbox / "secret.txt").exists()


def test_sweep_outputs_rejects_sensitive_files(tmp_path: Path):
    """.env, .git, .DS_Store 같은 민감/숨김 파일은 결과물로 수집되지 않아야 한다."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    (out_dir / ".env").write_text("SECRET=123")
    (out_dir / ".git").write_text("git dir")
    (out_dir / ".DS_Store").write_bytes(b"\x00\x00")
    (out_dir / "report.docx").write_bytes(b"PK000-valid-doc")

    plan = RunPlan(prompt="", note="", mcp_servers={}, out_dir=out_dir)
    run = DummyRun()

    copied = artcraft_tasks.sweep_outputs(plan, run, outbox)
    assert copied == 1
    assert (outbox / "report.docx").exists()
    assert not (outbox / ".env").exists()
    assert not (outbox / ".git").exists()
    assert not (outbox / ".DS_Store").exists()


def test_sweep_outputs_overwrites_modified_files_even_if_size_is_same(tmp_path: Path):
    """내용이 갱신된 파일은 기존 outbox에 같은 크기의 파일이 있더라도 덮어써야 한다."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    dest = outbox / "result.txt"
    dest.write_text("AAAA")  # 4 bytes

    src = out_dir / "result.txt"
    src.write_text("BBBB")  # 4 bytes, different content!

    plan = RunPlan(prompt="", note="", mcp_servers={}, out_dir=out_dir)
    run = DummyRun()

    copied = artcraft_tasks.sweep_outputs(plan, run, outbox)
    assert copied == 1
    assert dest.read_text() == "BBBB"


def test_catalog_cli_all_commands_are_fully_qualified():
    """카탈로그의 모든 cli 요령 내 실행 명령어(백틱 블록)는 단독 파편 없이 온전한 artcraft run <app> 형태여야 한다."""
    import re
    cmd_pattern = re.compile(r"`([^`]+)`")
    for cat in catalog.CATEGORIES:
        for line in cat.cli:
            commands = cmd_pattern.findall(line)
            for cmd in commands:
                cmd_strip = cmd.strip()
                # 옵션 플래그 설명(예: `--demo`, `--filter`)이나 명령 ID 설명이 아닌 실행문인 경우
                if any(cmd_strip.startswith(prefix) for prefix in ("run ", "commands", "info ", "convert ", "render ", "cat ", "eval ", "sample ", "combine ", "extract ", "split ", "edit ")):
                    # 단독 명령어 파편이 아니어야 함!
                    pytest.fail(f"카테고리 {cat.id}의 cli 요령에 단독 명령어 파편이 있습니다: `{cmd_strip}` (line: {line})")



def test_prepare_run_logs_warning_when_previous_output_staging_fails(monkeypatch, tmp_path: Path):
    """이전 결과 파일 복사(_stage)가 실패했을 때 prepare_run 의 plan.log 에 경고가 기록되어야 한다."""
    from ttakkari.models import (
        ArtcraftTask,
        Artifact,
        ChatSession,
        Workspace,
    )

    # mock stage failure
    monkeypatch.setattr(artcraft_tasks, "_stage", lambda art, folder: None)
    dummy_art = Artifact(id=uuid.uuid4(), session_id=uuid.uuid4(), filename="prev_result.png", source=artcraft_tasks.OUTPUT_SOURCE)

    db = AsyncMock()
    ws = Workspace(name="ArtCraft", root_path=str(tmp_path), is_git=False)
    sess = ChatSession(id=uuid.uuid4(), workspace_id=ws.id, title="테스트", engine="claude")
    task = ArtcraftTask(id=uuid.uuid4(), session_id=sess.id, category="photo", apps=["photocraft"])
    run = DummyRun(context_artifact_ids=[], usage={"artcraft": {"use_previous": True}})

    db.scalar = AsyncMock(return_value=task)
    db.get = AsyncMock(return_value=ws)

    # _round_index -> 1, _previous_outputs -> [dummy_art]
    monkeypatch.setattr(artcraft_tasks, "_round_index", AsyncMock(return_value=1))
    monkeypatch.setattr(artcraft_tasks, "_previous_outputs", AsyncMock(return_value=[dummy_art]))

    plan = asyncio.run(artcraft_tasks.prepare_run(db, sess, run, tmp_path / "outbox"))
    assert plan is not None
    assert any("이전 결과 파일" in line and "prev_result.png" in line for line in plan.log)


def test_prepare_run_note_and_prompt_emphasize_out_folder(monkeypatch, tmp_path: Path):
    """지침과 프롬프트에 반드시 out/ 디렉토리에 저장해야만 수집된다는 경고가 강조되어야 한다."""
    from ttakkari.models import ArtcraftTask, ChatSession, Workspace

    db = AsyncMock()
    ws = Workspace(name="ArtCraft", root_path=str(tmp_path), is_git=False)
    sess = ChatSession(id=uuid.uuid4(), workspace_id=ws.id, title="테스트", engine="claude")
    task = ArtcraftTask(id=uuid.uuid4(), session_id=sess.id, category="photo", apps=["photocraft"])
    run = DummyRun(context_artifact_ids=[], usage={"artcraft": {"use_previous": False}})

    db.scalar = AsyncMock(return_value=task)
    db.get = AsyncMock(return_value=ws)
    monkeypatch.setattr(artcraft_tasks, "_round_index", AsyncMock(return_value=1))

    plan = asyncio.run(artcraft_tasks.prepare_run(db, sess, run, tmp_path / "outbox"))
    assert plan is not None
    assert "반드시" in plan.note and "out" in plan.note


def test_summaries_flags_succeeded_run_with_no_outputs():
    """Run이 succeeded 상태이지만 산출물이 0개인 경우 summaries의 error에 결과 파일 없음이 가시화되어야 한다."""
    from ttakkari.models import ArtcraftTask, Run, RunStatus

    db = AsyncMock()
    task = ArtcraftTask(id=uuid.uuid4(), session_id=uuid.uuid4(), category="photo", title="빈 작업")
    run = Run(id=uuid.uuid4(), session_id=task.session_id, status=RunStatus.succeeded, error=None, prompt="지시")

    # _runs_by_session mock
    # db.scalars(select(Run)...)
    db.scalars = AsyncMock(return_value=[run])
    # db.execute(count query...)
    mock_exec = MagicMock()
    mock_exec.all.return_value = []  # count 0
    db.execute = AsyncMock(return_value=mock_exec)

    items = asyncio.run(artcraft_tasks.summaries(db, [task]))
    assert len(items) == 1
    assert items[0]["output_count"] == 0
    assert items[0]["error"] == "결과 파일 없음 (산출물이 생성되지 않았습니다)"


