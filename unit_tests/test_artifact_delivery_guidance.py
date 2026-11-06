"""생성 프롬프트의 전달 안내 일관성: outbox 자동 수집=에이전트 파일, 사용자 전달=share_file 필수."""
from __future__ import annotations

import types
import uuid
from pathlib import Path


def _reverse_prompt() -> str:
    from ttakkari.reverse_tasks import _prompt

    job = types.SimpleNamespace(output_path="/tmp/rev", target="관찰 대상", result={})
    run = types.SimpleNamespace(id=uuid.uuid4())
    return _prompt(job, run)


def test_reverse_prompt_requires_share_for_zip_delivery():
    from ttakkari import reverse_tasks

    src = Path(reverse_tasks.__file__).read_text(encoding="utf-8")
    assert "outbox에도 저장해 사용자가 내려받게" not in src
    prompt = _reverse_prompt()
    assert "share_file" in prompt
    assert "AI 작업 파일" in prompt


def test_artcraft_note_preserves_auto_collection_and_manual_delivery_contract():
    from ttakkari import artcraft_tasks

    note = artcraft_tasks.NOTE
    assert "작업 폴더 루트에만 둔 파일은 자동 수집되지 않는다" in note
    assert "{out} 아래에 저장한 결과는 자동 수집" in note
    assert "결과 파일에 share_file 을 호출하지 않는다" in note
    assert "share_file 은 대화에 파일을 공유할 때" in note
    assert "keep_artifact 는 사용자가 요청했을 때 보관함에 넣는 도구" in note


def test_system_note_outbox_goes_to_agent_area():
    from ttakkari.runs.manager import SYSTEM_NOTE

    assert "share_file" in SYSTEM_NOTE
    assert "agent area" in SYSTEM_NOTE
    assert "registered automatically and the user can preview and download them" not in SYSTEM_NOTE


def test_orchestrator_note_share_hands_to_user():
    from ttakkari.orchestrator import runtime

    src = Path(runtime.__file__).read_text(encoding="utf-8")
    flat = " ".join(src.replace("\\", " ").split())
    assert "that is not in the outbox, use `share_file`" not in flat
    assert "call `share_file` on it so the user can open it" in flat
