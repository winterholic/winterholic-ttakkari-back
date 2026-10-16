import json

from ttakkari.knowledge.chatgpt import parse_shard


def test_all_branches_speakers_and_unsupported_parts_are_accounted():
    raw = json.dumps(
        [
            {
                "id": "c1",
                "current_node": "b",
                "mapping": {
                    "root": {"parent": None, "message": None},
                    "a": {
                        "parent": "root",
                        "message": {
                            "author": {"role": "user"},
                            "create_time": 1700000000,
                            "content": {"parts": ["내 진로 결정", {"image": "asset"}]},
                        },
                    },
                    "b": {
                        "parent": "a",
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["제안입니다"]},
                        },
                    },
                    "other": {
                        "parent": "root",
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["이전 분기"]},
                        },
                    },
                },
            }
        ],
        ensure_ascii=False,
    ).encode()
    units, summary = parse_shard(raw, "fixture/conversations.json")
    assert summary["nodes"] == 4 and summary["units"] == 5
    assert summary["messages"] == 3
    assert [
        u["body"]
        for u in units
        if u["speaker"] == "user" and u["outcome"] == "pending_extraction"
    ] == ["내 진로 결정", "이전 분기"]
    assert next(u for u in units if u["body"] == "이전 분기")["active_branch"] is False
    assert sum(u["outcome"] == "unsupported_content" for u in units) == 1
    assert sum(u["outcome"] == "no_message" for u in units) == 1
    assert (
        next(u for u in units if u["body"] == "내 진로 결정")["event_time"] is not None
    )


def test_malformed_and_unknown_branch_are_retained_without_false_current_status():
    raw = b'[{"id":"c","mapping":{"x":{"message":null},"bad":5}}]'
    units, summary = parse_shard(raw, "conversations.json")
    assert len(units) == 2
    assert all(u["active_branch"] is None for u in units)
    assert summary["errors"]


def test_long_parts_have_exact_slices_and_stable_keys_independent_of_json_order():
    conv = {
        "id": "c",
        "current_node": "n",
        "mapping": {
            "n": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["a" * 1801]},
                },
            }
        },
    }
    units, _ = parse_shard(json.dumps([conv]).encode(), "a")
    again, _ = parse_shard(json.dumps([conv], sort_keys=True).encode(), "a")
    assert "".join(u["body"] for u in units) == "a" * 1801
    assert all(len(u["body"]) <= 1600 for u in units)
    assert [u["external_key"] for u in units] == [u["external_key"] for u in again]


def test_attachments_and_part_offsets_have_replayable_original_locators():
    raw = json.dumps(
        [
            {
                "id": "c",
                "current_node": "n",
                "mapping": {
                    "n": {
                        "parent": None,
                        "message": {
                            "id": "m",
                            "author": {"role": "user"},
                            "metadata": {"attachments": [{"id": "asset"}]},
                            "content": {"parts": ["a" * 1801]},
                        },
                    }
                },
            }
        ]
    ).encode()
    units, _summary = parse_shard(raw, "/source/conversations.json")
    assert len(units) == 3
    assert units[1]["provenance"]["char_offset"] == 1600
    assert units[1]["provenance"]["message_id"] == "m"
    assert units[-1]["locator"].endswith("/message/metadata/attachments/0")
    assert units[-1]["outcome"] == "unsupported_content"
