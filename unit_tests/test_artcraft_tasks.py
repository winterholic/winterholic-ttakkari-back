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


def test_sweep_outputs_deduplicates_same_name_and_content_but_keeps_distinct_files(tmp_path: Path):
    """share_file이 이미 outbox에 둔 동일 파일은 한 번만 수집하고 정상적인 다른 파일은 유지한다."""
    out_dir = tmp_path / "out"
    (out_dir / "nested").mkdir(parents=True)
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "same.txt").write_text("same content")
    (out_dir / "same.txt").write_text("same content")
    (out_dir / "nested" / "same.txt").write_text("different content")
    (out_dir / "different.txt").write_text("same content")
    (out_dir / "different-content.txt").write_text("different")

    copied = artcraft_tasks.sweep_outputs(RunPlan(prompt="", note="", mcp_servers={}, out_dir=out_dir), DummyRun(), outbox)

    assert copied == 3
    assert (outbox / "nested" / "same.txt").read_text() == "different content"
    assert (outbox / "different.txt").read_text() == "same content"
    assert (outbox / "different-content.txt").read_text() == "different"


def test_summaries_uses_latest_round_outputs_not_previous_round_outputs():
    """이전 라운드 파일이 있어도 파일 제작 최신 라운드 산출물이 비면 실패를 표시한다."""
    from ttakkari.models import ArtcraftTask, Run, RunStatus

    db = AsyncMock()
    task = ArtcraftTask(id=uuid.uuid4(), session_id=uuid.uuid4(), category="photo", title="편집")
    first = Run(id=uuid.uuid4(), session_id=task.session_id, status=RunStatus.succeeded, prompt="첫 작업")
    latest = Run(id=uuid.uuid4(), session_id=task.session_id, status=RunStatus.succeeded, prompt="다시 작업")
    db.scalars = AsyncMock(return_value=[first, latest])
    mock_exec = MagicMock()
    mock_exec.all.return_value = [(task.session_id, first.id, 1)]
    db.execute = AsyncMock(return_value=mock_exec)

    items = asyncio.run(artcraft_tasks.summaries(db, [task]))

    assert items[0]["output_count"] == 1
    assert items[0]["error"] == "결과 파일 없음 (산출물이 생성되지 않았습니다)"


def test_summaries_does_not_report_missing_file_for_information_template():
    """파일 없이 텍스트로 답하는 정보 조회 템플릿은 산출물 없음 오류를 내지 않는다."""
    from ttakkari.models import ArtcraftTask, Run, RunStatus

    db = AsyncMock()
    task = ArtcraftTask(id=uuid.uuid4(), session_id=uuid.uuid4(), category="video", title="영상 정보", template_id="info")
    run = Run(id=uuid.uuid4(), session_id=task.session_id, status=RunStatus.succeeded, prompt="정보", result_text="길이 3초")
    db.scalars = AsyncMock(return_value=[run])
    mock_exec = MagicMock()
    mock_exec.all.return_value = []
    db.execute = AsyncMock(return_value=mock_exec)

    items = asyncio.run(artcraft_tasks.summaries(db, [task]))

    assert items[0]["output_count"] == 0
    assert items[0]["error"] is None


def test_english_information_lookup_without_template_has_no_missing_output_error(monkeypatch):
    """성공한 영어 정보 조회는 summaries와 상세 라운드 모두 파일 누락 오류가 아니다."""
    from ttakkari.models import ArtcraftTask, Run, RunStatus
    from ttakkari.api import desk_feedback

    task = ArtcraftTask(id=uuid.uuid4(), session_id=uuid.uuid4(), category="video", title="영상 길이")
    run = Run(id=uuid.uuid4(), session_id=task.session_id, status=RunStatus.succeeded,
              prompt="What is the duration of this video?", result_text="The video is 12 seconds long.")
    db_summary = AsyncMock()
    db_summary.scalars = AsyncMock(return_value=[run])
    result = MagicMock()
    result.all.return_value = []
    db_summary.execute = AsyncMock(return_value=result)
    db_detail = AsyncMock()
    db_detail.scalars = AsyncMock(side_effect=[[run], [], [], [run]])
    db_detail.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(desk_feedback, "runs_out", AsyncMock(return_value=[]))

    summaries = asyncio.run(artcraft_tasks.summaries(db_summary, [task]))
    detail = asyncio.run(artcraft_tasks.task_detail(db_detail, task))

    assert summaries[0]["error"] is None
    assert detail["error"] is None
    assert detail["rounds"][0]["error"] is None
    assert artcraft_tasks._expects_output_file(task, run) is False
    korean_lookup = Run(id=uuid.uuid4(), session_id=task.session_id, prompt="이 영상 길이를 알려 주세요")
    file_request = Run(id=uuid.uuid4(), session_id=task.session_id, prompt="Create a short video about the ocean")
    assert artcraft_tasks._expects_output_file(task, korean_lookup) is False
    assert artcraft_tasks._expects_output_file(task, file_request) is True


def test_create_task_checks_real_active_update_job_before_queue(monkeypatch):
    """실제 create_task 호출은 동일 앱의 활성 업데이트 Job을 409로 막는다."""
    from ttakkari import artcraft
    from ttakkari.artcraft import Job

    monkeypatch.setattr(artcraft, "JOBS", {"up1": Job(id="up1", kind="update", action="update photocraft", apps=["photocraft"], state="running")})
    db = AsyncMock()

    with pytest.raises(artcraft.ArtcraftError) as exc:
        asyncio.run(artcraft_tasks.create_task(db, category_id="photo", prompt="편집", template_id=None,
                                               input_ids=[], apps=["photocraft"], options=None, speed="normal", ip=None))

    assert (exc.value.status, exc.value.code) == (409, "app_updating")


def test_add_round_checks_real_active_update_job_before_queue(monkeypatch):
    """실제 add_round 호출은 동일 앱의 활성 롤백 Job을 409로 막는다."""
    from ttakkari import artcraft
    from ttakkari.artcraft import Job
    from ttakkari.models import ArtcraftTask

    monkeypatch.setattr(artcraft, "JOBS", {"up1": Job(id="up1", kind="rollback", action="rollback photocraft", apps=["photocraft"], state="queued")})
    task = ArtcraftTask(id=uuid.uuid4(), category="photo", apps=["photocraft"])

    with pytest.raises(artcraft.ArtcraftError) as exc:
        asyncio.run(artcraft_tasks.add_round(AsyncMock(), task, prompt="계속", input_ids=[], use_previous=False,
                                             options=None, speed="normal", ip=None))

    assert (exc.value.status, exc.value.code) == (409, "app_updating")


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
    assert "keep_artifact" in plan.note and "대화" in plan.note and "자동 수집과 중복" in plan.note
    assert "최종 결과 저장 위치: " in plan.prompt and " (반드시 이 위치에 저장)" in plan.prompt


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


def test_active_task_apps_returns_apps_of_active_runs():
    """active_task_apps 는 queued/running 상태인 활성 작업의 앱 목록만 반환해야 한다."""
    from ttakkari.models import ArtcraftTask

    db = AsyncMock()
    t1 = ArtcraftTask(id=uuid.uuid4(), session_id=uuid.uuid4(), apps=["photocraft", "lightcraft"])
    t2 = ArtcraftTask(id=uuid.uuid4(), session_id=uuid.uuid4(), apps=["vectorcraft"])

    # db.scalars mock
    db.scalars = AsyncMock(return_value=[t1, t2])

    busy = asyncio.run(artcraft_tasks.active_task_apps(db))
    assert busy == {"photocraft", "lightcraft", "vectorcraft"}


def test_create_task_rejects_updating_apps(monkeypatch):
    """현재 업데이트 중인 앱으로 작업을 생성하려 하면 409 app_updating 오류로 차단해야 한다."""
    from ttakkari import artcraft
    from ttakkari.artcraft import Job

    updating_job = Job(id="up1", kind="update", action="update photocraft", apps=["photocraft"], state="running")
    monkeypatch.setattr(artcraft, "JOBS", {"up1": updating_job})

    with pytest.raises(artcraft.ArtcraftError) as exc_info:
        artcraft_tasks.check_app_not_updating(["photocraft"])
    assert exc_info.value.status == 409
    assert exc_info.value.code == "app_updating"


def test_resolve_task_engine_uses_first_active_account_in_effective_order(monkeypatch):
    """엔진은 계정 설정의 유효 순서에서 첫 활성 개인 계정으로 선택되어야 한다."""
    from ttakkari.models import EngineAccount
    from ttakkari.runs.profiles import Profile

    db = AsyncMock()
    c_off = EngineAccount(id=uuid.uuid4(), engine="claude", name="Claude", enabled=False, pool="personal", priority=1)
    a_on = EngineAccount(id=uuid.uuid4(), engine="antigravity", name="Antigravity", enabled=True, pool="personal", priority=2)
    available_accounts = [a_on]

    async def availability(_db, engine, pool="personal", **_kwargs):
        return ([Profile(id=a.id, engine=a.engine, name=a.name, pool=pool)
                 for a in available_accounts if a.engine == engine], [])

    # 비활성 계정은 건너뛰고 활성 계정을 선택한다.
    monkeypatch.setattr(artcraft_tasks.profiles, "effective_order", AsyncMock(return_value=[c_off, a_on]))
    monkeypatch.setattr(artcraft_tasks.profiles, "availability", availability)
    engine, desc = asyncio.run(artcraft_tasks.resolve_task_engine(db))
    assert engine == "antigravity"
    assert "antigravity" in desc.lower()

    # Claude 가 켜져 있어도 유효 순서의 첫 계정이 Antigravity 면 이를 선택한다.
    c_on = EngineAccount(id=uuid.uuid4(), engine="claude", name="Claude", enabled=True, pool="personal", priority=1)
    available_accounts = [a_on, c_on]
    monkeypatch.setattr(artcraft_tasks.profiles, "effective_order", AsyncMock(return_value=[a_on, c_on]))
    engine, desc = asyncio.run(artcraft_tasks.resolve_task_engine(db))
    assert engine == "antigravity"
    available_accounts = [c_on, a_on]
    monkeypatch.setattr(artcraft_tasks.profiles, "effective_order", AsyncMock(return_value=[c_on, a_on]))
    engine, desc = asyncio.run(artcraft_tasks.resolve_task_engine(db))
    assert engine == "claude"

    # 3. 모두 꺼짐 -> 503 오류 발생
    available_accounts = []
    monkeypatch.setattr(artcraft_tasks.profiles, "effective_order", AsyncMock(return_value=[c_off]))
    with pytest.raises(artcraft_tasks.artcraft.ArtcraftError) as exc:
        asyncio.run(artcraft_tasks.resolve_task_engine(db))
    assert exc.value.status == 503
    assert exc.value.code == "no_active_engine"


def test_resolve_task_engine_skips_active_account_that_is_not_execution_ready(monkeypatch):
    """개인 임시 우선순위의 준비 안 된 엔진을 건너뛰고 다음 준비된 엔진을 선택한다."""
    from ttakkari.models import EngineAccount
    from ttakkari.runs.profiles import Profile

    db = AsyncMock()
    ag = EngineAccount(id=uuid.uuid4(), engine="antigravity", name="Antigravity", enabled=True,
                       pool="personal", priority=1)
    claude = EngineAccount(id=uuid.uuid4(), engine="claude", name="Claude", enabled=True,
                           pool="personal", priority=2)
    monkeypatch.setattr(artcraft_tasks.profiles, "effective_order", AsyncMock(return_value=[ag, claude]))
    monkeypatch.setattr(artcraft_tasks.profiles, "availability", AsyncMock(return_value=([
        Profile(id=claude.id, engine="claude", name=claude.name, pool="personal")
    ], [])))

    engine, _ = asyncio.run(artcraft_tasks.resolve_task_engine(db))

    assert engine == "claude"


def test_resolve_task_engine_reports_error_when_no_personal_engine_is_ready(monkeypatch):
    """활성 개인 계정이 있어도 실행 준비된 엔진이 없으면 원인을 분명히 알린다."""
    from ttakkari.models import EngineAccount

    db = AsyncMock()
    ag = EngineAccount(id=uuid.uuid4(), engine="antigravity", name="Antigravity", enabled=True,
                       pool="personal", priority=1)
    monkeypatch.setattr(artcraft_tasks.profiles, "effective_order", AsyncMock(return_value=[ag]))
    monkeypatch.setattr(artcraft_tasks.profiles, "availability", AsyncMock(return_value=([], [])))

    with pytest.raises(artcraft_tasks.artcraft.ArtcraftError) as exc:
        asyncio.run(artcraft_tasks.resolve_task_engine(db))

    assert exc.value.status == 503
    assert exc.value.code == "no_ready_engine"


def test_create_task_persists_the_resolved_active_engine(monkeypatch, tmp_path: Path):
    """create_task은 엔진 선택 결과를 세션에 고정해 후속 라운드가 같은 엔진을 사용하게 한다."""
    from ttakkari.models import ChatSession, Run, Workspace

    ws = Workspace(name="ArtCraft", root_path=str(tmp_path), is_git=False)
    db = AsyncMock()
    db.add = MagicMock()
    monkeypatch.setattr(artcraft_tasks, "ensure_workspace", AsyncMock(return_value=ws))
    monkeypatch.setattr(artcraft_tasks, "_load_inputs", AsyncMock())
    monkeypatch.setattr(artcraft_tasks, "resolve_task_engine", AsyncMock(return_value=("codex", "Codex")))
    monkeypatch.setattr(artcraft_tasks, "_queue", AsyncMock(return_value=Run(id=uuid.uuid4())))

    asyncio.run(artcraft_tasks.create_task(db, category_id="photo", prompt="간단한 그림", template_id=None,
                                           input_ids=[], apps=["photocraft"], options=None, speed="normal", ip=None))

    session = next(obj for obj in (call.args[0] for call in db.add.call_args_list) if isinstance(obj, ChatSession))
    assert session.engine == "codex"


def test_provider_adapters_receive_artcraft_mcp_servers(monkeypatch, tmp_path: Path):
    """Claude, Codex, Antigravity, Muse 설정 파일에 ArtCraft MCP 인자와 환경이 그대로 전달된다."""
    import json
    import tomllib
    from ttakkari.agents.base import LaunchSpec
    from ttakkari.agents.claude import ClaudeAdapter
    from ttakkari.agents.codex import CodexAdapter
    from ttakkari.agents.antigravity import AntigravityAdapter
    from ttakkari.agents.muse import MuseAdapter
    from ttakkari.config import get_settings

    mcp = {"artcraft_photocraft": {"command": "/bin/artcraft", "args": ["mcp", "photocraft"], "env": {}}}
    sess_id = str(uuid.uuid4())
    spec = LaunchSpec(
        cwd=tmp_path, prompt="test", model="muse-spark-1.3", effort="high",
        outbox=tmp_path / "outbox", write_roots=[tmp_path], system_note="note",
        mcp_servers=mcp, session_id=sess_id, engine_session_id=None,
    )

    # 1. Claude: build_command 시 --mcp-config 파일 생성 및 artcraft_photocraft 명세 포함 검사
    claude = ClaudeAdapter()
    c_argv = claude.build_command(spec, {})
    assert "--mcp-config" in c_argv
    mcp_file = Path(c_argv[c_argv.index("--mcp-config") + 1])
    assert mcp_file.is_file()
    claude_servers = json.loads(mcp_file.read_text())["mcpServers"]
    assert {k: v for k, v in claude_servers["artcraft_photocraft"].items() if k != "type"} == mcp["artcraft_photocraft"]
    claude.cleanup(spec)

    # 2. Codex: config toml 프로파일 작성 내용에 artcraft_photocraft 포함 검사
    codex = CodexAdapter()
    codex_home = tmp_path / "codex_home"
    codex.build_command(spec, {"CODEX_HOME": str(codex_home), "TTAKKARI_RUN_ID": "test-run"})
    cfg_files = list(codex_home.glob("*.config.toml"))
    assert len(cfg_files) == 1
    codex_server = tomllib.loads(cfg_files[0].read_text())["mcp_servers"]["artcraft_photocraft"]
    assert codex_server["command"] == mcp["artcraft_photocraft"]["command"]
    assert codex_server["args"] == mcp["artcraft_photocraft"]["args"]
    codex.cleanup(spec)

    # 3. Antigravity: _session_project 파일 생성 검사
    agy = AntigravityAdapter()
    base_dir = agy._session_project(spec.session_id, spec.mcp_servers)
    mcp_config = base_dir / ".agents" / "mcp_config.json"
    assert mcp_config.exists()
    agy_servers = json.loads(mcp_config.read_text())["mcpServers"]
    assert agy_servers["artcraft_photocraft"] == mcp["artcraft_photocraft"]
    agy.cleanup(spec)

    # 4. Muse: payload spec.json 생성 검사
    monkeypatch.setattr(get_settings(), "muse_enabled", True)
    orig_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda p: True if "package.json" in str(p) else orig_is_file(p))
    muse = MuseAdapter()
    m_cmd = muse.build_command(spec, {"XDG_CONFIG_HOME": str(tmp_path / "muse-config")})
    spec_json = Path(m_cmd[2])
    assert spec_json.exists()
    payload = json.loads(spec_json.read_text())
    assert payload["mcp_servers"]["artcraft_photocraft"] == mcp["artcraft_photocraft"]
    muse.cleanup(spec)


def test_effectcraft_motion_prompt_instructs_cli_video_render(monkeypatch, tmp_path: Path):
    """motion 카테고리 태스크는 MCP 프레임만으로 완료를 주장하지 않도록 CLI 동영상 렌더 안내가 주입되어야 한다."""
    from ttakkari.models import ChatSession, ArtcraftTask, Workspace

    db = AsyncMock()
    ws = Workspace(name="ArtCraft", root_path=str(tmp_path), is_git=False)
    sess = ChatSession(id=uuid.uuid4(), workspace_id=ws.id, title="모션", engine="claude")
    task = ArtcraftTask(id=uuid.uuid4(), session_id=sess.id, category="motion", apps=["effectcraft"])
    run = DummyRun(context_artifact_ids=[], usage={"artcraft": {"use_previous": False}})

    db.scalar = AsyncMock(return_value=task)
    db.get = AsyncMock(return_value=ws)
    monkeypatch.setattr(artcraft_tasks, "_round_index", AsyncMock(return_value=1))

    plan = asyncio.run(artcraft_tasks.prepare_run(db, sess, run, tmp_path / "outbox"))
    assert plan is not None
    # 동영상 렌더 CLI 지침이 prompt 또는 note에 명시되어야 함
    combined = plan.prompt + "\n" + plan.note
    assert "EffectCraft" in combined
    assert "render --out" in combined and ".mp4" in combined


def test_prepare_run_builds_app_specific_artcraft_mcp_servers(monkeypatch, tmp_path: Path):
    """실행 준비는 선택한 ArtCraft 앱마다 해당 앱 MCP의 registry 이름과 작업 폴더를 전달한다."""
    from ttakkari.models import ArtcraftTask, ChatSession, Workspace

    db = AsyncMock()
    ws = Workspace(name="ArtCraft", root_path=str(tmp_path), is_git=False)
    sess = ChatSession(id=uuid.uuid4(), workspace_id=ws.id, title="편집", engine="codex")
    task = ArtcraftTask(id=uuid.uuid4(), session_id=sess.id, category="photo", apps=["photocraft", "lightcraft"])
    run = DummyRun(context_artifact_ids=[], usage={"artcraft": {"use_previous": False}})
    db.scalar = AsyncMock(return_value=task)
    db.get = AsyncMock(return_value=ws)
    monkeypatch.setattr(artcraft_tasks, "_round_index", AsyncMock(return_value=1))
    monkeypatch.setattr(artcraft_tasks.artcraft, "known_app", lambda app: app)
    monkeypatch.setattr(artcraft_tasks.artcraft, "binary_path", lambda: Path("/artcraft/bin/artcraft"))
    monkeypatch.setattr(artcraft_tasks.artcraft, "binary_ok", lambda: True)

    plan = asyncio.run(artcraft_tasks.prepare_run(db, sess, run, tmp_path / "outbox"))

    assert plan is not None
    assert set(plan.mcp_servers) == {"artcraft_photocraft", "artcraft_lightcraft"}
    assert plan.mcp_servers["artcraft_photocraft"]["args"][:2] == ["mcp", "photocraft"]
    assert plan.mcp_servers["artcraft_lightcraft"]["args"][:2] == ["mcp", "lightcraft"]
    assert str(tmp_path) in " ".join(plan.mcp_servers["artcraft_photocraft"]["args"])


def test_effectcraft_catalog_documents_cli_video_render_syntax():
    """카탈로그는 MCP 단일 프레임과 지원 CLI의 동영상 렌더를 구분해 실제 인자 순서를 안내한다."""
    motion = catalog.category("motion")
    assert motion is not None
    instructions = " ".join(motion.cli)
    assert "--project X.ecproj render --out o.mp4" in instructions
    assert "MCP 도구 단독 동영상 렌더" in " ".join(motion.cannot)


def test_independent_verifier_validates_real_outputs(tmp_path: Path):
    """독립 제작 verifier 가 정상 파일과 빈/손상 파일을 정확히 판별해야 한다."""
    # 1. 0바이트 파일 -> 실패
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    res = artcraft_tasks.verify_task_output(empty)
    assert res["valid"] is False
    assert "0바이트" in res["error"]

    # 2. 유효한 PDF -> 성공
    good_pdf = tmp_path / "good.pdf"
    good_pdf.write_bytes(b"%PDF-1.4 header and contents")
    res = artcraft_tasks.verify_task_output(good_pdf)
    assert res["valid"] is True
    assert res["kind"] == "pdf"

    # 3. 유효한 PNG -> 성공
    good_png = tmp_path / "good.png"
    good_png.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...")
    res = artcraft_tasks.verify_task_output(good_png)
    assert res["valid"] is True
    assert res["kind"] == "png"

    # 4. 유효한 SVG -> 성공
    good_svg = tmp_path / "good.svg"
    good_svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'><rect/></svg>")
    res = artcraft_tasks.verify_task_output(good_svg)
    assert res["valid"] is True
    assert res["kind"] == "svg"

    # 5. 가짜 PDF (헤더 없음) -> 실패
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"NOT A REAL PDF CONTENT")
    res = artcraft_tasks.verify_task_output(bad_pdf)
    assert res["valid"] is False

