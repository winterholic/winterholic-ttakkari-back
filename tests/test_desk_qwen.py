"""접수 1순위 로컬 Qwen(runs/desk_qwen). 가짜 llama.cpp 서버(httpx.MockTransport)로 정책 매핑·근거 검증·폴백을 확인한다."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from helpers import run_events, start_run, wait_run
from test_desk import _desk, _leads

from ttakkari.config import get_settings
from ttakkari.runs import desk_qwen

EXPECTED_V3_FINGERPRINT = "4483325c7c690b506fa5774e5befb046b795f3d6737ea383c126f3ab388965d4"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_qwen_enabled", True)
    monkeypatch.setattr(get_settings(), "desk_qwen_base_url", "http://qwen.test")  # 외부 서버 모드(MockTransport 가 받는다)
    desk_qwen.reset_cooldown()
    yield
    desk_qwen.reset_cooldown()


def _server(kind: str, evidence: str = "", lead_pick: str | None = None, reply: str = "안녕하세요!", calls: list | None = None,
            verify: tuple[str, str, str] = ("yes", "yes", "yes"), pure_social: str = "yes"):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        if calls is not None:
            calls.append((name, body))
        content = {"desk_kind": {"kind": kind, "evidence": evidence}, "desk_reply": {"reply": reply},
                   "desk_lead": {"lead_id": lead_pick, "same_project": "yes", "same_task": "yes"}, "desk_social_reply": {"reply": reply},
                   "desk_verify": dict(zip(("subject_same", "scope_complete", "time_ok"), verify, strict=True)),
                   "desk_social_guard": {"pure_social": pure_social}}[name]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})
    return handler


def _use(monkeypatch, handler):
    monkeypatch.setattr(desk_qwen, "_http_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))


def test_prompts_match_the_measured_harness():
    # Pin the revised topic-boundary harness; historical v1/v2 measurements do not apply to v3.
    assert desk_qwen.PROMPT_VERSION == "harness-v3-topic-boundary"
    assert desk_qwen.PROMPTS_V1_SHA256 == {
        "kind_system": "d8326292d57c61eddf59caf5ca3ac7805de578c3e3780c59971772d4123b155c",
        "reply_system": "f3987845b414c20fe08ad407ad9ce3c8bc3bf4f1a92542b7edb670f479c53e90"}
    assert desk_qwen.prompt_fingerprint() == EXPECTED_V3_FINGERPRINT


def test_grounded_requires_real_quote_about_the_same_task():
    board = "- lead_id=aa11 「앱 아이콘 교체」 상태: 완료 · 작업 세션 1개(실행 중 0) · 최근 결과: 아이콘 3종을 교체하고 PR을 올렸습니다."
    hist = "(최근 대화 없음)"
    assert desk_qwen.grounded("「앱 아이콘 교체」 상태: 완료", board, hist, "앱 아이콘 작업 마무리됐어?")
    assert not desk_qwen.grounded("없음", board, hist, "앱 아이콘 작업 마무리됐어?")
    assert not desk_qwen.grounded("결제 서버 정상 가동 중", board, hist, "결제 서버는 살아 있어?")  # 원문에 없는 인용
    assert not desk_qwen.grounded("「앱 아이콘 교체」 상태: 완료", board, hist, "배포 끝났어?")  # 다른 일
    assert desk_qwen.grounded("「앱 아이콘 교체」 · 최근 결과: 아이콘 3종", board, hist, "아이콘 결과 알려줘")  # 조각 인용


async def test_decide_maps_kinds_to_actions(monkeypatch):
    _use(monkeypatch, _server("work"))
    d = await desk_qwen.decide([], [], "로그인 API에 토큰 추가해줘", [])
    assert (d.action, d.abstained, d.lead_id, d.calls) == ("delegate", False, None, 1)
    _use(monkeypatch, _server("social"))
    d = await desk_qwen.decide([], [], "고마워", [])
    assert (d.action, d.reply, d.calls) == ("answer", "안녕하세요!", 2)  # 진술문: kind + 응답(사교 가드는 질문 꼴에서만)
    _use(monkeypatch, _server("status", evidence="없음"))
    d = await desk_qwen.decide([], [], "배포 끝났어?", [])
    assert (d.action, d.abstained, d.grounded) == ("delegate", True, False)
    assert d.reply == desk_qwen.ABSTAIN_TO_DELEGATE_REPLY
    _use(monkeypatch, _server("unclear"))
    assert (await desk_qwen.decide([], [], "음...", [])).abstained


async def test_status_with_real_evidence_answers_and_lead_is_limited_to_board(monkeypatch):
    board = ["- lead_id=aa11 「앱 아이콘 교체」 상태: 완료 · 작업 세션 1개(실행 중 0)"]
    _use(monkeypatch, _server("status", evidence="「앱 아이콘 교체」 상태: 완료", reply="네, 완료됐어요."))
    d = await desk_qwen.decide(board, [], "앱 아이콘 작업 끝났어?", ["aa11"])
    assert (d.action, d.reply, d.grounded, d.calls) == ("answer", "네, 완료됐어요.", True, 3)  # kind + 근거 검증 + 응답
    calls: list = []
    _use(monkeypatch, _server("work", lead_pick="aa11", calls=calls))
    d = await desk_qwen.decide(board, [], "아이콘 색 바꿔줘", ["aa11"])
    assert d.lead_id == "aa11"
    lead_call = next(b for n, b in calls if n == "desk_lead")
    assert lead_call["response_format"]["json_schema"]["schema"]["properties"]["lead_id"]["enum"] == [None, "aa11"]


async def test_failures_raise_and_cool_down(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")
    _use(monkeypatch, boom)
    with pytest.raises(desk_qwen.QwenUnavailable):
        await desk_qwen.decide([], [], "안녕", [])
    assert not desk_qwen.available()  # 실패 직후에는 건너뛴다
    with pytest.raises(desk_qwen.QwenUnavailable) as e:
        await desk_qwen.decide([], [], "안녕", [])
    assert e.value.reason == "cooldown"


async def test_too_long_prompt_skips_without_cooldown(monkeypatch):
    _use(monkeypatch, _server("social"))
    with pytest.raises(desk_qwen.QwenUnavailable) as e:
        await desk_qwen.decide(["x" * 5000], [], "안녕", [])
    assert e.value.reason == "prompt_too_long" and desk_qwen.available()


async def test_desk_uses_qwen_first_and_creates_lead(client, auth, make_dir, monkeypatch):
    _use(monkeypatch, _server("work"))
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "다크모드 고쳐줘"))["id"])
    assert run["status"] == "succeeded" and run["engine"] == "qwen" and run["model"] == "qwen3.5-4b-q4_k_m"
    assert run["result_text"].startswith(desk_qwen.DELEGATE_REPLY)
    route = next(e for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.route")
    assert route["payload"]["engine"] == "qwen" and route["payload"]["qwen"]["kind"] == "work"
    leads = await _leads(client, auth, desk["id"])
    assert len(leads) == 1 and leads[0]["title"] == "다크모드 고쳐줘"


async def test_desk_answers_social_with_qwen(client, auth, make_dir, monkeypatch):
    _use(monkeypatch, _server("social", reply="천만에요!"))
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "고마워"))["id"])
    assert run["result_text"] == "천만에요!" and run["engine"] == "qwen"
    assert await _leads(client, auth, desk["id"]) == []


async def test_desk_falls_back_to_luna_when_qwen_is_down(client, auth, make_dir, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")
    _use(monkeypatch, boom)
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "안녕 DESK_ANSWER"))["id"])
    assert run["status"] == "succeeded" and run["engine"] == "codex" and run["result_text"] == "desk answer by codex"
    types = [e["type"] for e in await run_events(client, auth, run["id"])]
    assert "desk.retry" in types and "desk.route" in types


# ---- 사실성 답변은 모든 경로에서 근거가 필요하다(2026-10-16 common30-11 사후 분석: board_overview 는 무검증 경로였다)

BOARD2 = ["- lead_id=aa11 「앱 아이콘 교체」 상태: 완료 · 작업 세션 1개(실행 중 0) · 최근 결과: 아이콘 3종을 교체했습니다.",
          "- lead_id=bb22 「검색 인덱스 재구축」 상태: 진행 중 · 작업 세션 2개(실행 중 2)"]


def _names(calls):
    return [n for n, _ in calls]


async def test_board_overview_is_verified_and_unsupported_question_is_handed_over(monkeypatch):
    calls: list = []
    _use(monkeypatch, _server("board_overview", verify=("no", "yes", "yes"), calls=calls))
    d = await desk_qwen.decide([], [], "에이전트들 지금 몇 개나 실행 중이야?", [])
    assert (d.action, d.abstained, d.reply) == ("delegate", True, desk_qwen.ABSTAIN_TO_DELEGATE_REPLY)
    assert "desk_verify" in _names(calls) and "desk_reply" not in _names(calls)  # 검증을 거쳤고 지어낸 답을 만들지 않았다
    verify = json.loads(next(b for n, b in calls if n == "desk_verify")["messages"][-1]["content"])
    assert desk_qwen.EMPTY_BOARD in verify["evidence"]  # board_overview 근거는 모델 인용이 아니라 코드가 넣은 작업판 전체


async def test_board_overview_answers_only_when_verifier_confirms(monkeypatch):
    calls: list = []
    _use(monkeypatch, _server("board_overview", reply="지금 맡긴 일은 없어요.", calls=calls))
    d = await desk_qwen.decide([], [], "맡긴 일 있어?", [])
    assert (d.action, d.reply, d.abstained) == ("answer", "지금 맡긴 일은 없어요.", False)
    assert _names(calls) == ["desk_kind", "desk_verify", "desk_reply"]


@pytest.mark.parametrize("verdict", [("no", "yes", "yes"), ("yes", "no", "yes"), ("yes", "yes", "no")])
async def test_any_failed_check_blocks_a_factual_answer(monkeypatch, verdict):
    calls: list = []
    _use(monkeypatch, _server("status", evidence="「앱 아이콘 교체」 상태: 완료", verify=verdict, calls=calls))
    d = await desk_qwen.decide(BOARD2, [], "앱 아이콘 교체 끝났어? 배포도 됐어?", ["aa11", "bb22"])
    assert (d.action, d.abstained) == ("delegate", True) and "desk_reply" not in _names(calls)


async def test_status_quote_not_in_context_never_reaches_verifier(monkeypatch):
    calls: list = []
    _use(monkeypatch, _server("status", evidence="서버는 정상 동작 중입니다", calls=calls))
    d = await desk_qwen.decide(BOARD2, [], "서버 정상이야?", ["aa11", "bb22"])
    assert d.abstained and d.grounded is False and _names(calls) == ["desk_kind", "desk_lead"]


async def test_factual_reply_sees_only_the_evidence_lines(monkeypatch):
    calls: list = []
    _use(monkeypatch, _server("status", evidence="「앱 아이콘 교체」 상태: 완료", reply="네, 완료됐어요.", calls=calls))
    d = await desk_qwen.decide(BOARD2, [], "앱 아이콘 교체 끝났어?", ["aa11", "bb22"])
    assert d.action == "answer"
    reply_body = json.dumps(next(b for n, b in calls if n == "desk_reply")["messages"], ensure_ascii=False)
    assert "아이콘 3종을 교체했습니다" in reply_body  # 근거 줄 전체는 준다
    assert "검색 인덱스 재구축" not in reply_body  # 다른 일의 줄은 주지 않는다


async def test_social_guard_stops_state_questions_disguised_as_chat(monkeypatch):
    calls: list = []
    _use(monkeypatch, _server("social", pure_social="no", calls=calls))
    d = await desk_qwen.decide([], [], "다들 잘 하고 있지?", [])
    assert (d.action, d.abstained) == ("delegate", True)
    assert _names(calls) == ["desk_kind", "desk_social_guard"]


async def test_social_reply_never_sees_the_board(monkeypatch):
    calls: list = []
    _use(monkeypatch, _server("social", reply="천만에요!", calls=calls))
    await desk_qwen.decide(BOARD2, [], "고마워", ["aa11", "bb22"])
    body = json.dumps(next(b for n, b in calls if n == "desk_social_reply")["messages"], ensure_ascii=False)
    assert "아이콘" not in body and "검색 인덱스" not in body


async def test_compound_question_needs_every_question_to_be_supported(monkeypatch):
    calls: list = []
    answers = iter([("yes", "yes", "yes"), ("no", "no", "yes")])  # 첫 질문은 근거 있음, 둘째는 없음

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        content = {"desk_kind": {"kind": "status", "evidence": "「앱 아이콘 교체」 상태: 완료"}, "desk_reply": {"reply": "네."},
                   "desk_lead": {"lead_id": None}}.get(name)
        if name == "desk_verify":
            content = dict(zip(("subject_same", "scope_complete", "time_ok"), next(answers), strict=True))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})
    _use(monkeypatch, handler)
    d = await desk_qwen.decide(BOARD2, [], "앱 아이콘 교체 끝났어? 서버 배포는 끝났어?", ["aa11", "bb22"])
    assert d.abstained and d.verified is False and calls.count("desk_verify") == 2 and "desk_reply" not in calls


def test_questions_split_only_real_compound_questions():
    assert desk_qwen._questions("고마워! 근데 서버는 어때?") == ["고마워! 근데 서버는 어때?"]
    assert desk_qwen._questions("맡긴 일 있어? 그리고 서버는 정상이야?") == ["맡긴 일 있어?", "그리고 서버는 정상이야?"]


async def test_plain_acknowledgement_skips_the_social_guard(monkeypatch):
    calls: list = []
    _use(monkeypatch, _server("social", reply="네!", calls=calls, pure_social="no"))  # 가드가 불러졌다면 no 로 막혔을 것
    d = await desk_qwen.decide([], [], "ㅇㅋ 알겠어", [])
    assert d.action == "answer" and _names(calls) == ["desk_kind", "desk_social_reply"]


@pytest.mark.parametrize("text", ["다들 잘 하고 있지?", "서버 안 죽었지", "배표 됏냐", "워커들 별일 없나요"])
def test_question_shaped_messages_are_checked_by_the_guard(text):
    assert desk_qwen._is_question(text)


@pytest.mark.parametrize("text", ["고마워요", "ㅇㅋ 알겠어", "네 알겠습니다", "안녕하세요 좋은 아침이에요"])
def test_statements_are_not_questions(text):
    assert not desk_qwen._is_question(text)


# ---- 형식·시간 초과·회로 차단·lead 권한

def _raw_server(status: int = 200, content: str = "{}", delay: float = 0.0, counter: list | None = None):
    async def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter.append(1)
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})
    return handler


@pytest.mark.parametrize("handler", [
    _raw_server(content="이건 JSON 이 아니다"),
    _raw_server(content='{"kind": "weird", "evidence": ""}'),  # schema 밖 값(서버가 grammar 를 어긴 경우)
    _raw_server(content='["list"]'),
    _raw_server(status=500),
])
async def test_bad_server_output_is_unavailable_not_a_guess(monkeypatch, handler):
    _use(monkeypatch, handler)
    with pytest.raises(desk_qwen.QwenUnavailable):
        await desk_qwen.decide([], [], "안녕", [])


async def test_slow_server_times_out_into_fallback(monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_qwen_timeout_seconds", 0.2)
    _use(monkeypatch, _raw_server(content='{"kind": "social", "evidence": ""}', delay=1.0))
    with pytest.raises(desk_qwen.QwenUnavailable):
        await desk_qwen.decide([], [], "안녕", [])
    assert not desk_qwen.available()


async def test_cooldown_expires_after_the_configured_window(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")
    _use(monkeypatch, boom)
    with pytest.raises(desk_qwen.QwenUnavailable):
        await desk_qwen.decide([], [], "안녕", [])
    assert not desk_qwen.available()
    monkeypatch.setattr(desk_qwen, "_down_until", time.monotonic() - 1)  # 60초가 지난 것과 같다
    _use(monkeypatch, _server("work"))
    assert (await desk_qwen.decide([], [], "코드 고쳐줘", [])).action == "delegate"


async def test_cooldown_default_is_sixty_seconds():
    assert get_settings().desk_qwen_cooldown_seconds == 60.0


async def test_lead_outside_the_board_is_dropped_even_if_server_returns_it(monkeypatch):
    _use(monkeypatch, _server("work", lead_pick="zz99"))
    d = await desk_qwen.decide(BOARD2, [], "아이콘 색 바꿔줘", ["aa11", "bb22"])
    assert d.action == "delegate" and d.lead_id is None


async def test_failed_qwen_is_skipped_for_the_cooldown_and_desk_keeps_working(client, auth, make_dir, monkeypatch):
    seen: list = []

    async def refuse(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        raise httpx.ConnectError("refused")
    _use(monkeypatch, refuse)
    desk = await _desk(client, auth, make_dir)
    first = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "안녕 DESK_ANSWER"))["id"])
    second = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "안녕 DESK_ANSWER"))["id"])
    assert first["engine"] == second["engine"] == "codex" and len(seen) == 1  # 둘째 요청은 Qwen 을 부르지 않았다
    types = [e["type"] for e in await run_events(client, auth, second["id"])]
    assert "desk.retry" not in types  # 회로가 열려 있는 동안은 시도 자체가 없으므로 재시도 이벤트도 없다
    skipped = [e["payload"] for e in await run_events(client, auth, second["id"]) if e["type"] == "desk.skip"]
    assert skipped[0]["reason"] == "cooldown"


async def test_unsupported_status_through_the_api_hands_over_to_a_planner(client, auth, make_dir, monkeypatch):
    _use(monkeypatch, _server("status", evidence="없음"))
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "에이전트들 지금 몇 개 돌아?"))["id"])
    route = next(e for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.route")["payload"]
    assert run["engine"] == "qwen" and route["action"] == "delegate" and route["qwen"]["abstained"] is True
    assert route["qwen"]["prompt_version"] == desk_qwen.PROMPT_VERSION
    assert run["result_text"].startswith(desk_qwen.ABSTAIN_TO_DELEGATE_REPLY)
    assert len(await _leads(client, auth, desk["id"])) == 1


def test_qwen_context_keeps_board_identity_and_recent_turns_within_cpu_budget():
    from ttakkari.runs.desk import _qwen_context
    board = [f'- lead_id=lead{i} 「작업 {i}」 상태: 진행 중 · 작업 세션 2개(실행 중 1) · 최근 결과: ' + '상세 결과 ' * 60 for i in range(8)]
    history = ['사용자: ' + '오래된 질문 ' * 100, '따까리: ' + '이전 답변 ' * 100, '사용자: 가장 최근 요청', '따까리: 가장 최근 답변']
    compact_board, compact_history = _qwen_context(board, history, '이 작업 이어서 해줘', 4000)
    assert sum(map(len, compact_board + compact_history)) + len('이 작업 이어서 해줘') + len(compact_board + compact_history) <= 2000
    assert len(compact_board) == 8
    for i, line in enumerate(compact_board):
        assert f'lead_id=lead{i}' in line and '상태: 진행 중' in line
        assert line == board[i] or line == board[i].split(' · 최근 결과: ')[0]
    assert compact_history[-2:] == history[-2:]


def test_qwen_context_does_not_change_short_input_or_hide_an_oversized_board():
    from ttakkari.runs.desk import _qwen_context
    assert _qwen_context(['작업판'], ['대화'], '안녕', 4000) == (['작업판'], ['대화'])
    board = ['- lead_id=keep 「' + '긴 제목' * 1500 + '」 상태: 진행 중']
    compact, _ = _qwen_context(board, [], '목록 보여줘', 4000)
    assert compact == board


async def test_production_size_context_is_still_routed_through_qwen(client, auth, make_dir, monkeypatch):
    from ttakkari.runs import desk as module
    board = [f'- lead_id=lead{i} 「작업 {i}」 상태: 완료 · 최근 결과: ' + '상세 결과 ' * 80 for i in range(8)]
    history = ['사용자: ' + '이전 대화 ' * 100] * 5 + ['사용자: 고마워']
    monkeypatch.setattr(module, '_context', lambda *_: asyncio.sleep(0, result=(board, history)))
    calls = []
    _use(monkeypatch, _server('social', reply='천만에요!', calls=calls))
    sess = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, sess['id'], '고마워'))['id'])
    assert run['engine'] == 'qwen'
    assert run['result_text'] == '천만에요!'
    kind = next(body for name, body in calls if name == 'desk_kind')
    content = kind['messages'][-1]['content']
    assert len(content) < 2100 and 'lead_id=lead7' in content and '고마워' in content


async def test_input_skip_is_visible_without_marking_server_down(client, auth, make_dir, monkeypatch):
    from ttakkari.runs import desk as module
    board = ['- lead_id=keep 「' + '긴 제목' * 1500 + '」 상태: 진행 중']
    monkeypatch.setattr(module, '_context', lambda *_: asyncio.sleep(0, result=(board, [])))
    _use(monkeypatch, _server('social'))
    sess = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, sess['id'], '안녕 DESK_ANSWER'))['id'])
    assert run['engine'] == 'codex'
    retries = [e['payload'] for e in await run_events(client, auth, run['id']) if e['type'] == 'desk.skip']
    assert any(p.get('engine') == 'qwen' and p.get('reason') == 'prompt_too_long' for p in retries)
    assert desk_qwen.available()
