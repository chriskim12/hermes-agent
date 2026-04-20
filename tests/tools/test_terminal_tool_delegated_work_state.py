import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from gateway.work_state import WorkRecord, WorkStateStore


def _make_env_config():
    return {
        "env_type": "local",
        "env_name": "default",
        "cwd": "/repo/demo",
        "timeout": 180,
        "cleanup_timeout": 600,
        "default_timeout": 180,
    }


def _seed_direct_record(tmp_path, session_key: str):
    store = WorkStateStore(tmp_path / "gateway_work_state.json")
    now = datetime.now(timezone.utc)
    store.upsert(
        WorkRecord(
            work_id="wk-direct-live-1",
            title="delegate current work to OMX",
            objective="prove terminal background OMX handoff marks delegated work-state",
            owner="hermes",
            executor="hermes",
            mode="direct",
            owner_session_id=session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Delegate to OMX",
            proof="message_ingress:discord",
        )
    )
    return store


def test_background_omx_command_marks_current_gateway_work_record_delegated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.terminal_tool import terminal_tool

    session_key = "agent:discord:thread:test"
    store = _seed_direct_record(tmp_path, session_key)

    mock_env = MagicMock()
    mock_env.env = {}

    mock_proc_session = MagicMock()
    mock_proc_session.id = "proc-omx-1"
    mock_proc_session.pid = 4321

    mock_registry = MagicMock()
    mock_registry.spawn_local.return_value = mock_proc_session

    command = "tmux new-session -d -s omx-ch36 'bash -lc \"cd /repo/demo && omx exec -C /repo/demo --json\"'"

    with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
         patch("tools.process_registry.process_registry", mock_registry), \
         patch("tools.approval.get_current_session_key", return_value=session_key):
        result = json.loads(
            terminal_tool(
                command=command,
                background=True,
                workdir="/repo/demo",
            )
        )

    assert result["session_id"] == "proc-omx-1"
    fresh_store = WorkStateStore(tmp_path / "gateway_work_state.json")
    records = fresh_store.list_records()
    assert len(records) == 1
    record = records[0]
    assert record.work_id == "wk-direct-live-1"
    assert record.executor == "omx"
    assert record.mode == "delegated"
    assert record.executor_session_id is None
    assert record.tmux_session == "omx-ch36"
    assert record.repo_path == "/repo/demo"
    assert record.worktree_path == "/repo/demo"
    assert record.next_action == "Resume the delegated OMX work"
    assert record.proof == "terminal_background:omx_exec"


def test_background_non_omx_command_does_not_rewrite_work_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.terminal_tool import terminal_tool

    session_key = "agent:discord:thread:test"
    store = _seed_direct_record(tmp_path, session_key)

    mock_env = MagicMock()
    mock_env.env = {}

    mock_proc_session = MagicMock()
    mock_proc_session.id = "proc-shell-1"
    mock_proc_session.pid = 9999

    mock_registry = MagicMock()
    mock_registry.spawn_local.return_value = mock_proc_session

    with patch("tools.terminal_tool._get_env_config", return_value=_make_env_config()), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
         patch("tools.process_registry.process_registry", mock_registry), \
         patch("tools.approval.get_current_session_key", return_value=session_key):
        result = json.loads(
            terminal_tool(
                command="python server.py",
                background=True,
                workdir="/repo/demo",
            )
        )

    assert result["session_id"] == "proc-shell-1"
    fresh_store = WorkStateStore(tmp_path / "gateway_work_state.json")
    records = fresh_store.list_records()
    assert len(records) == 1
    record = records[0]
    assert record.executor == "hermes"
    assert record.mode == "direct"
    assert record.executor_session_id is None
    assert record.tmux_session is None
    assert record.repo_path is None
    assert record.worktree_path is None
    assert record.proof == "message_ingress:discord"
