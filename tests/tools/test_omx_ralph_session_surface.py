import pytest

from tools.omx_ralph_session_surface import (
    build_omx_leader_command,
    build_omx_ralph_command,
    build_ralph_in_session_message,
    launch_pty_ralph_session,
    materialize_ralph_session_surface,
    start_omx_ralph_lane,
    validate_ralph_session_surface,
)


class _FakeSession:
    id = "proc-real-ralph-1"


class _FakeRegistry:
    def __init__(self):
        self.spawned = []
        self.submitted = []

    def spawn_local(self, command, cwd=None, task_id="", session_key="", env_vars=None, use_pty=False):
        self.spawned.append(
            {
                "command": command,
                "cwd": cwd,
                "session_key": session_key,
                "env_vars": env_vars,
                "use_pty": use_pty,
            }
        )
        return _FakeSession()

    def submit_stdin(self, session_id, data):
        self.submitted.append({"session_id": session_id, "data": data})
        return {"status": "ok"}


class _FakeWorkStateStore:
    def __init__(self):
        self.updates = []

    def update_record(self, work_id, owner_session_id, **updates):
        self.updates.append(
            {
                "work_id": work_id,
                "owner_session_id": owner_session_id,
                "updates": updates,
            }
        )
        return True


def test_builds_upstream_interactive_leader_and_ralph_commands():
    assert build_omx_leader_command() == "omx --madmax --high"
    assert build_omx_ralph_command("fix owner ingress") == "omx ralph 'fix owner ingress'"
    assert build_ralph_in_session_message("fix owner ingress") == "$ralph 'fix owner ingress'"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({}, "missing_real_ralph_session_surface"),
        ({"executor_session_id": "proc-1", "pty": False}, "missing_real_ralph_session_surface"),
    ],
)
def test_validate_ralph_session_surface_rejects_noninteractive_or_unbacked_surfaces(kwargs, reason):
    result = validate_ralph_session_surface(**kwargs)
    assert result["status"] == "error"
    assert result["reason"] == reason


def test_materialize_ralph_session_surface_accepts_pty_process_with_lane_truth(tmp_path):
    surface = materialize_ralph_session_surface(
        task="continue CH-232",
        repo_path=str(tmp_path),
        executor_session_id="proc-pty-1",
        pty=True,
    )

    assert surface.executor_session_id == "proc-pty-1"
    assert surface.tmux_session is None
    assert surface.current_lane == "ralph"
    assert surface.planning_gate == "closed"
    assert surface.next_execution_branch == "ralph"
    assert surface.close_authority == "hermes"
    assert surface.command == "omx ralph 'continue CH-232'"
    assert surface.injected_message == "$ralph 'continue CH-232'"


def test_materialize_ralph_session_surface_accepts_existing_tmux_leader(tmp_path):
    surface = materialize_ralph_session_surface(
        task="continue CH-232",
        repo_path=str(tmp_path),
        tmux_session="omx-leader-1",
    )

    assert surface.executor_session_id is None
    assert surface.tmux_session == "omx-leader-1"
    assert surface.injected_message == "$ralph 'continue CH-232'"


def test_launch_pty_ralph_session_spawns_official_omx_ralph_entrypoint(tmp_path):
    registry = _FakeRegistry()
    surface = launch_pty_ralph_session(
        task="throwaway smoke",
        repo_path=str(tmp_path),
        session_key="agent:main:discord:thread:1",
        process_registry=registry,
    )

    assert registry.spawned == [
        {
            "command": "omx ralph 'throwaway smoke'",
            "cwd": str(tmp_path.resolve()),
            "session_key": "agent:main:discord:thread:1",
            "env_vars": {"TERM": "xterm-256color"},
            "use_pty": True,
        }
    ]
    assert registry.submitted == []
    assert surface.executor_session_id == "proc-real-ralph-1"
    assert surface.command == "omx ralph 'throwaway smoke'"
    assert surface.injected_message == "$ralph 'throwaway smoke'"


def test_start_omx_ralph_lane_is_operator_path_and_records_work_state(tmp_path):
    registry = _FakeRegistry()
    store = _FakeWorkStateStore()

    result = start_omx_ralph_lane(
        task="continue CH-232",
        repo_path=str(tmp_path),
        session_key="agent:main:discord:thread:1",
        work_id="wk-ch232",
        owner_session_id="owner-session-1",
        process_registry=registry,
        work_state_store=store,
    )

    assert result["status"] == "accepted"
    assert result["work_state_updated"] is True
    assert result["surface"]["command"] == "omx ralph 'continue CH-232'"
    assert result["surface"]["injected_message"] == "$ralph 'continue CH-232'"
    assert registry.spawned[0]["use_pty"] is True
    assert registry.submitted == []
    assert store.updates[0]["work_id"] == "wk-ch232"
    assert store.updates[0]["owner_session_id"] == "owner-session-1"
    updates = store.updates[0]["updates"]
    assert updates["executor"] == "omx"
    assert updates["mode"] == "delegated"
    assert updates["state"] == "running"
    assert updates["executor_session_id"] == "proc-real-ralph-1"
    assert updates["proof"] == "ralph_session_surface:pty_process"
    assert updates["current_lane"] == "ralph"
    assert updates["planning_gate"] == "closed"
    assert updates["next_execution_branch"] == "ralph"
    assert updates["close_authority"] == "hermes"
