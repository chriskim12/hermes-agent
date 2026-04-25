import pytest

from tools.omx_ralph_session_surface import (
    build_omx_leader_command,
    build_ralph_in_session_message,
    launch_pty_ralph_session,
    materialize_ralph_session_surface,
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
                "use_pty": use_pty,
            }
        )
        return _FakeSession()

    def submit_stdin(self, session_id, data):
        self.submitted.append({"session_id": session_id, "data": data})
        return {"status": "ok"}


def test_builds_upstream_interactive_leader_and_in_session_ralph_message():
    assert build_omx_leader_command() == "omx --madmax --high"
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
    assert surface.command == "omx --madmax --high"
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


def test_launch_pty_ralph_session_spawns_omx_leader_and_injects_ralph(tmp_path):
    registry = _FakeRegistry()
    surface = launch_pty_ralph_session(
        task="throwaway smoke",
        repo_path=str(tmp_path),
        session_key="agent:main:discord:thread:1",
        process_registry=registry,
    )

    assert registry.spawned == [
        {
            "command": "omx --madmax --high",
            "cwd": str(tmp_path.resolve()),
            "session_key": "agent:main:discord:thread:1",
            "use_pty": True,
        }
    ]
    assert registry.submitted == [
        {"session_id": "proc-real-ralph-1", "data": "$ralph 'throwaway smoke'"}
    ]
    assert surface.executor_session_id == "proc-real-ralph-1"
    assert surface.injected_message == "$ralph 'throwaway smoke'"
