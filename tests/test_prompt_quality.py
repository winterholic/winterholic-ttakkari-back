"""작업자 지시의 가독성 검사: 띄어쓰기 없는 압축체는 거절하고, 정상 문장·영어·짧은 지시는 통과시킨다."""
import pytest
from test_orchestrator import _orch, _session

from ttakkari.config import get_settings
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import prompt_quality

# 2026-11-03 운영에서 기획 세션(Codex Sol)이 실제로 보낸 지시의 앞부분.
COMPRESSED = (
    "현재 Gemma 교정 Run2f4e0987에 대한 계측단위 정정이다. 목표·위치·불변식·원3 무효셀만재실행은 앞지시와같으며 "
    "gemma-first3-v{2,3}/attempts.jsonl을 먼저대조하라. 중앙재계산에서 v3는9 attempt이지만 /v1/chat/completions receipt는14개다. "
    "따라서 보고의 실제모델요청18회는 attempt18과HTTP/generation을혼동한것이다. 추후보고에서 cells/attempts/LLM HTTP turns를따로집계하고 "
    "v2 HTTP수는 receipt/원trace/소유server로그로확정가능한범위와미확정을구별하라. 원자료수정금지다. 새3 제한도 새cell attempts3을뜻하며 "
    "각attempt의필요한multi-turn HTTP까지3개로제한하라는뜻은아니다. 기존동일turn/token안전예산을유지하고새attempt추가없이진행한다. "
    "완료기준은 원유효6+새3 coverage9와모든새HTTP body설정, 실제tool결과유효성, cleanup, 회귀tests, 정확한attempt/HTTP단위구별이다."
)
# 같은 기획 세션이 쓴 정상 지시.
READABLE = (
    "저장소와 작업 위치는 /Users/winterholic/development/hyperez/regna-ios다. 먼저 AGENTS.md와 관련 연구 자료를 읽어라. "
    "최신 사용자 목표는 세 핵심 모델과 새 6영역 에이전트 벤치마크의 실제 성능 개선이며 이번 단위는 그 첫 실행 준비다. "
    "기존 dirty 파일 및 원시 결과를 보존하고, 다른 작업자 파일은 수정하지 마라. 이번 턴에는 GPU 서버 시작과 LLM 추론을 하지 마라. "
    "모든 보고는 상태: 완료/부분/막힘, 실제 명령과 출력, 수정한 파일, 남은 작업을 포함하고 단위 완료를 전체 연구 완료로 표현하지 마라. "
    "목표는 Nemotron Nano 4B의 기존 78행 실패를 근거로 성공률을 보존하면서 지연과 잘림과 형식 오류를 줄일 상위 2개 개선 후보를 준비하는 것이다."
)


def test_detects_compressed_korean_and_passes_the_rest():
    assert prompt_quality.rewrite_request(COMPRESSED) is not None
    assert "다시 써라" in prompt_quality.rewrite_request(COMPRESSED)
    assert prompt_quality.rewrite_request(READABLE) is None
    assert prompt_quality.rewrite_request("Write the report and save it under docs/. " * 20) is None  # 영어
    assert prompt_quality.rewrite_request("원3무효셀만재실행은앞지시와같으며 먼저대조하라") is None  # 한글 150자 미만
    assert prompt_quality.rewrite_request("") is None


def test_measured_gap_between_compressed_and_readable():
    _, bad = prompt_quality.compressed_ratio(COMPRESSED)
    _, good = prompt_quality.compressed_ratio(READABLE)
    assert bad >= 0.15 and good <= 0.03  # 기준 0.08 의 양쪽에 충분한 여유가 있다


async def test_spawn_worker_rejects_compressed_prompt_and_accepts_readable(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    bad = await client.post("/api/orchestrator/workers", headers=h, json={"title": "압축", "prompt": COMPRESSED, "engine": "claude"})
    assert bad.status_code == 422, bad.text
    detail = bad.json()
    assert detail.get("code") == "prompt_compressed" or "prompt_compressed" in bad.text
    assert "다시 써라" in bad.text and "완료 기준" in bad.text
    ok = await client.post("/api/orchestrator/workers", headers=h, json={"title": "정상", "prompt": READABLE, "engine": "claude"})
    assert ok.status_code == 201, ok.text


async def test_message_worker_rejects_compressed_follow_up(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", headers=h, json={"title": "t", "prompt": READABLE, "engine": "claude"})).json()["worker_id"]
    bad = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"prompt": COMPRESSED})
    assert bad.status_code == 422 and "prompt_compressed" in bad.text
    ok = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"prompt": READABLE})
    assert ok.status_code == 202, ok.text


async def test_gate_can_be_turned_off(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_prompt_quality_gate", False)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    r = await client.post("/api/orchestrator/workers", headers=_orch(issue_token(orch["id"])),
                          json={"title": "압축 허용", "prompt": COMPRESSED, "engine": "claude"})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("text", [READABLE, "목표: 보고서 작성\n위치: ~/docs\n완료 기준: 파일 저장" * 30])
def test_structured_readable_prompts_never_flagged(text):
    assert prompt_quality.rewrite_request(text) is None
