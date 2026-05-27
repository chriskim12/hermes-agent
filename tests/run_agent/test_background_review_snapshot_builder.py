"""Tests for the bounded background-review snapshot builder."""

from __future__ import annotations

import json

from agent.background_review import build_review_snapshot


def _tool_msg(*, name: str = "memory", tool_call_id: str = "tc1", payload: dict | None = None):
    return {
        "role": "tool",
        "name": name,
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload or {"success": True, "message": "ok"}),
    }


def _assistant(text: str):
    return {"role": "assistant", "content": text}


def _user(text: str):
    return {"role": "user", "content": text}


def test_snapshot_under_limits_keeps_all_sections_intact():
    messages = [
        _user("please update the docs"),
        _assistant("draft response"),
        _tool_msg(payload={
            "success": True,
            "message": "Entry added",
            "target": "memory",
            "files_modified": ["docs/guide.md"],
        }),
        _assistant("final response"),
    ]

    snapshot = build_review_snapshot(
        messages,
        final_response="final response",
        max_messages=20,
        max_chars=10_000,
        max_tokens=10_000,
    )

    assert list(snapshot.keys()) == [
        "recent_delta",
        "final_response",
        "tool_summary",
        "artifact_changes",
        "history",
        "truncation",
    ]
    assert snapshot["truncation"]["truncated"] is False
    assert snapshot["truncation"]["reason"] is None
    assert [msg["role"] for msg in snapshot["recent_delta"]] == ["user", "assistant"]
    assert snapshot["final_response"]["content"] == "final response"
    assert snapshot["tool_summary"][0]["summary"] == "Entry added"
    assert snapshot["artifact_changes"][0]["path"] == "docs/guide.md"
    assert snapshot["history"] == []


def test_snapshot_over_message_limit_truncates_history_first():
    messages = [
        _user("old 1"),
        _assistant("old 2"),
        _user("old 3"),
        _assistant("old 4"),
        _user("latest request"),
        _assistant("latest draft"),
        _tool_msg(payload={"success": True, "message": "Entry added", "target": "memory"}),
        _assistant("latest final"),
    ]

    snapshot = build_review_snapshot(
        messages,
        final_response="latest final",
        max_messages=5,
        max_chars=10_000,
        max_tokens=10_000,
    )

    assert snapshot["truncation"]["used"]["messages"] <= 5
    assert len(snapshot["history"]) == 1  # oldest user message preserved; rest truncated
    assert snapshot["tool_summary"], "tool_summary should survive before lower-priority history"
    assert snapshot["final_response"]["content"] == "latest final"


def test_snapshot_over_char_limit_preserves_recent_delta():
    messages = [
        _user("old context " + ("x" * 1800)),
        _assistant("older assistant"),
        _user("current ask"),
        _assistant("current draft"),
        _assistant("current final"),
    ]

    snapshot = build_review_snapshot(
        messages,
        final_response="current final",
        max_messages=20,
        max_chars=700,
        max_tokens=10_000,
    )

    assert snapshot["truncation"]["used"]["chars"] <= 700
    assert snapshot["recent_delta"][0]["content"] == "current ask"
    assert snapshot["final_response"]["content"] == "current final"
    assert snapshot["truncation"]["hit_limits"]
    assert "history" in snapshot["truncation"]["sections"]


def test_snapshot_over_token_limit_never_exceeds_ceiling():
    messages = [
        _user("u" + ("x" * 800)),
        _assistant("a" + ("y" * 800)),
        _tool_msg(payload={"success": True, "message": "Entry added", "target": "memory"}),
        _assistant("final"),
    ]

    snapshot = build_review_snapshot(
        messages,
        final_response="final",
        max_messages=20,
        max_chars=10_000,
        max_tokens=220,
    )

    assert snapshot["truncation"]["used"]["tokens"] <= 220
    assert snapshot["truncation"]["truncated"] is True
    assert snapshot["final_response"] is not None
    assert snapshot["final_response"].get("content")


def test_snapshot_with_only_tool_output_is_valid_minimal_snapshot():
    snapshot = build_review_snapshot(
        [
            _tool_msg(
                payload={
                    "success": True,
                    "message": "Entry added",
                    "target": "memory",
                }
            )
        ],
        max_messages=10,
        max_chars=10_000,
        max_tokens=10_000,
    )

    assert snapshot["recent_delta"] == []
    assert snapshot["final_response"] is None
    assert snapshot["tool_summary"]
    assert snapshot["tool_summary"][0]["tool"] == "memory"
    assert snapshot["history"] == []


def test_snapshot_with_artifacts_but_no_messages_keeps_artifact_changes():
    snapshot = build_review_snapshot(
        [],
        artifact_changes=[
            {"kind": "modified", "path": "src/app.py", "summary": "refined helper"}
        ],
        max_messages=1,
        max_chars=1_000,
        max_tokens=1_000,
    )

    assert snapshot["recent_delta"] == []
    assert snapshot["final_response"] is None
    assert snapshot["artifact_changes"][0]["path"] == "src/app.py"
    assert snapshot["truncation"]["reason"] is None


def test_snapshot_ordering_stays_stable_after_truncation():
    messages = [
        _user("older one"),
        _assistant("older two"),
        _user("current ask"),
        _assistant("current draft"),
        _assistant("current final"),
    ]

    snapshot = build_review_snapshot(
        messages,
        final_response="current final",
        max_messages=4,
        max_chars=1_200,
        max_tokens=1_200,
    )

    assert list(snapshot.keys()) == [
        "recent_delta",
        "final_response",
        "tool_summary",
        "artifact_changes",
        "history",
        "truncation",
    ]
    assert [msg["content"] for msg in snapshot["history"]] == ["older one"]
