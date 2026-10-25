from ttakkari.collector.triage import (
    calculate_quality,
    calculate_relevance,
    evaluate_triage,
)


def test_relevance_distinguishes_related_and_unrelated():
    goal = "Python 비동기 프로그래밍 asyncio 이벤트 루프와 코루틴"

    # Highly relevant doc
    rel_text = (
        "파이썬 asyncio 모듈은 비동기 I/O를 위한 프레임워크입니다. "
        "이벤트 루프(event loop)가 코루틴(coroutine) 태스크를 관리하며 논블로킹 I/O를 처리합니다. "
        "async def와 await 구문을 사용하여 비동기 작업을 정의합니다."
    )
    score_rel, ev_rel = calculate_relevance(
        goal, "Python Asyncio 이벤트 루프 가이드", rel_text
    )
    assert score_rel >= 0.40, f"Expected high score, got {score_rel}"
    assert any("title_matches" in e for e in ev_rel)

    # Completely unrelated doc
    unrel_text = (
        "오늘의 야구 경기 결과입니다. 9회말 끝내기 안타로 승리를 거두었습니다. "
        "선발 투수의 호투와 타선의 집중력이 돋보였습니다. 관중들의 환호가 이어졌습니다."
    )
    score_unrel, ev_unrel = calculate_relevance(
        goal, "야구 경기 하이라이트", unrel_text
    )
    assert score_unrel < 0.10, f"Expected low score, got {score_unrel}"
    assert "term_coverage=0.00" in ev_unrel


def test_quality_penalizes_empty_short_and_spam():
    # Good quality technical text
    good_text = (
        "# 비동기 처리 아키텍처\n\n"
        "비동기 시스템의 핵심은 이벤트 기반 논블로킹 구조입니다.\n\n"
        "## 주요 구성 요소\n"
        "- 이벤트 큐: 발생하는 메시지를 수신 대기합니다.\n"
        "- 작업 워커: 백그라운드에서 작업을 처리합니다.\n\n"
        "```python\ndef run_worker():\n    pass\n```"
    )
    q_good, _ev_good = calculate_quality(good_text)
    assert q_good >= 0.70

    # Extremely short (<50 chars)
    q_short, ev_short = calculate_quality("짧은 글")
    assert q_short <= 0.40
    assert any("too_short" in e for e in ev_short)

    # Symbol heavy spam
    spam_text = "$$$$$ %%%%% !!!!!! @@@@@ ***** &&&&& ????? <<<<< >>>>>"
    q_spam, ev_spam = calculate_quality(spam_text)
    assert q_spam <= 0.30
    assert any("symbol_heavy" in e for e in ev_spam)


def test_conservative_triage_actions():
    goal = "PostgreSQL MVCC 트랜잭션 격리 수준 동시성 제어"

    # 1. Clear Good Document -> auto_accepted
    good_doc = (
        "PostgreSQL은 다중 버전 동시성 제어(MVCC)를 통해 높은 트랜잭션 동시성을 제공합니다. "
        "격리 수준(isolation level)에 따라 스냅샷을 생성하여 읽기 작업이 쓰기 작업을 블로킹하지 않습니다. "
        "Read Committed와 Repeatable Read 수준의 차이점을 설명합니다."
    )
    out_good = evaluate_triage(
        goal=goal, title="PostgreSQL MVCC 내부 구조", text=good_doc
    )
    assert out_good.action == "accept"
    assert out_good.triage_status == "auto_accepted"
    assert "triage:accepted" in out_good.reasons

    # 2. Clear Bad / Irrelevant Document (sufficient length, but zero overlap) -> auto_held
    bad_doc = (
        "오늘 프로야구 경기에서는 홈팀이 9회말 만루 찬스에서 끝내기 적시타를 터뜨리며 역전승을 거두었습니다. "
        "선발 투수는 7이닝 무실점으로 호투하였으며 관중들은 열광적인 응원을 보냈습니다. 내일도 주말 3연전이 이어집니다."
    )
    out_bad = evaluate_triage(goal=goal, title="프로야구 주말 경기 속보", text=bad_doc)
    assert out_bad.action == "hold"
    assert out_bad.triage_status == "auto_held"
    assert "triage:irrelevant_to_goal" in out_bad.reasons

    # 3. Exact duplicate -> auto_held
    out_dup = evaluate_triage(
        goal=goal, title="PostgreSQL MVCC", text=good_doc, exact_duplicate=True
    )
    assert out_dup.action == "hold"
    assert out_dup.triage_status == "auto_held"
    assert "exact_duplicate" in out_dup.reasons

    # 4. Ambiguous / Partial Document -> review_needed (Conservative: ACCEPT, do not hold!)
    ambig_doc = (
        "데이터베이스 관리 시스템 개론입니다. 여러 트랜잭션이 동시에 실행될 때 일관성을 유지해야 합니다. "
        "동시성 제어 방법에는 락킹과 타임스탬프 순서화 등이 있습니다. "
    )
    out_ambig = evaluate_triage(goal=goal, title="데이터베이스 기초", text=ambig_doc)
    assert out_ambig.action == "accept"  # Conservative: accepted into storage!
    assert out_ambig.triage_status == "review_needed"
    assert "review_needed" in out_ambig.reasons


def test_quality_envelope_conforms_to_collector_contracts():
    goal = "리액트 상태 관리"
    doc = "React에서 Zustand와 Redux를 사용한 상태 관리 패턴을 비교합니다."
    out = evaluate_triage(goal=goal, title="React 상태 관리", text=doc)

    # Check contracts requirement on relevance: query_id present when measured
    rel_axis = out.quality_envelope["relevance"]
    assert rel_axis["status"] == "measured"
    assert rel_axis["scale"] == "similarity"
    assert rel_axis["query_id"] is not None
    assert rel_axis["model_id"]
    assert rel_axis["model_revision"]

    # Check unmeasured axis
    edu_axis = out.quality_envelope["education"]
    assert edu_axis["status"] == "unmeasured"
    assert edu_axis["value"] is None
