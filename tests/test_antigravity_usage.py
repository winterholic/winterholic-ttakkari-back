from ttakkari.runs.usage import credits_from_antigravity, windows_from_antigravity


def test_parses_official_model_group_quota_as_used_percent_with_exact_remaining():
    body = {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {"name": "Gemini Models", "buckets": [
                        {"window": "weekly", "remaining_fraction": 0.9930701851844788,
                         "reset_time": "2026-10-22T13:21:38Z"},
                        {"window": "5h", "remaining_fraction": 0.9892208576202393,
                         "reset_time": "2026-10-15T18:21:38Z"},
                    ]},
                    {"name": "Claude and GPT models", "buckets": [
                        {"window": "weekly", "remaining_fraction": 1, "reset_time": "2026-10-22T14:26:15Z"},
                    ]},
                ]
            },
        },
    }

    windows = windows_from_antigravity(body)

    assert [(w.key, w.label, w.percent, w.remaining_percent) for w in windows] == [
        ("agy_gemini_models_5h", "Gemini Models · 5시간", 1, 98.92208576202393),
        ("agy_gemini_models_weekly", "Gemini Models · 주간", 1, 99.30701851844788),
        ("agy_claude_and_gpt_models_weekly", "Claude and GPT models · 주간", 0, 100.0),
    ]
    assert windows[0].resets_at == 1792088498


def test_credits_are_distinct_and_zero_is_a_measured_balance():
    assert credits_from_antigravity({"status": "SUCCESS", "command": {"name": "credits", "data": {"remaining_credits": 0}}}) == 0
    assert credits_from_antigravity({"status": "SUCCESS", "command": {"name": "credits", "data": {"remaining_credits": 4}}}) == 4
    assert credits_from_antigravity({"status": "ERROR", "command": {"name": "credits", "data": {"remaining_credits": 4}}}) is None


def test_unknown_or_malformed_quota_is_not_converted_to_zero_usage():
    assert windows_from_antigravity({"status": "ERROR", "command": {"name": "usage", "data": {"groups": []}}}) == []
    assert windows_from_antigravity({"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "buckets": [{"window": "weekly", "remaining_fraction": 2.0}]}
    ]}}}) == []
