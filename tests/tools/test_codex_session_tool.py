from __future__ import annotations

import json


def test_codex_session_tool_registered_in_codex_toolset(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)

    from model_tools import get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    tools = get_tool_definitions(enabled_toolsets=["codex"], quiet_mode=True)
    names = {tool["function"]["name"] for tool in tools}

    assert "codex_session" in names


def test_codex_session_tool_hidden_when_codex_cli_missing(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)

    from model_tools import get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    tools = get_tool_definitions(enabled_toolsets=["codex"], quiet_mode=True)
    names = {tool["function"]["name"] for tool in tools}

    assert "codex_session" not in names


def test_codex_session_tool_handler_returns_json_evidence(monkeypatch, tmp_path):
    from agent.transports.codex_app_server_session import TurnResult
    import tools.codex_session_tool as tool

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_turn(self, user_input, **kwargs):
            return TurnResult(final_text=json.dumps({"summary": "done", "changed_files": ["a.py"]}))

        def close(self):
            pass

    monkeypatch.setattr(tool, "CodexAppServerSession", FakeSession)
    payload = json.loads(
        tool.codex_session(
            task="Make the focused change",
            cwd=str(tmp_path),
            turn_timeout=3,
        )
    )

    assert payload["success"] is True
    assert payload["summary"] == "done"
    assert payload["changed_files"] == ["a.py"]
    assert payload["user_facing_final"] is False
    assert payload["requires_hermes_verification"] is True


def test_codex_session_tool_schema_documents_hermes_worktree_write_profile():
    import tools.codex_session_tool as tool

    profile_description = tool.CODEX_SESSION_SCHEMA["parameters"]["properties"]["permission_profile"]["description"]

    assert "hermes-worktree-write" in profile_description


def test_codex_session_tool_schema_smoke_preserves_evidence_only_contract(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)

    from model_tools import get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    tools = get_tool_definitions(enabled_toolsets=["codex"], quiet_mode=True)
    schema = next(tool["function"] for tool in tools if tool["function"]["name"] == "codex_session")

    assert schema["description"]
    assert "structured execution evidence" in schema["description"]
    assert "not a user-facing final answer" in schema["description"]
    assert schema["parameters"]["required"] == ["task"]
    assert set(schema["parameters"]["properties"]) == {"task", "cwd", "turn_timeout", "permission_profile"}
