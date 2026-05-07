from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli.kanban_work_state import project_work_state_to_kanban_run


def _record(**overrides):
    values = {
        "work_id": "CH-411",
        "title": "CH-411 title",
        "objective": "Map work_state outcomes into Kanban run metadata",
        "owner": "hermes",
        "executor": "omx",
        "mode": "delegated",
        "owner_session_id": "owner-session",
        "executor_session_id": "exec-session",
        "tmux_session": "omx-CH-411",
        "repo_path": "/repo/hermes-agent",
        "worktree_path": "/repo/hermes-agent/.worktrees/ch411",
        "state": "running",
        "next_action": "Continue bounded slice",
        "proof": "work_state:running",
        "current_lane": "omx_exec",
        "planning_gate": "closed",
        "next_execution_branch": "none",
        "close_authority": "hermes",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _project(record, **kwargs):
    return project_work_state_to_kanban_run(record, **kwargs)


def _run(projection):
    return projection["task_run"]


def test_running_work_state_maps_to_running_task_and_open_run_metadata():
    projection = _project(_record(state="running"))

    assert projection["status"] == "mapped"
    assert projection["task_status"] == "running"
    assert _run(projection)["status"] == "running"
    assert _run(projection)["outcome"] is None
    metadata = _run(projection)["metadata"]
    assert metadata["schema"] == "work_state_kanban_run_projection.v1"
    assert metadata["work_state"]["work_id"] == "CH-411"
    assert metadata["work_state"]["state"] == "running"
    assert metadata["kanban_done_projection"] == "forbidden"


@pytest.mark.parametrize(
    ("state", "run_status", "outcome"),
    [
        ("blocked", "blocked", "blocked"),
        ("stale", "blocked", "blocked"),
        ("retry_needed", "blocked", "blocked"),
        ("handoff_needed", "blocked", "blocked"),
        ("failed", "failed", "gave_up"),
    ],
)
def test_recoverable_work_state_outcomes_block_task_and_record_canonical_run_outcome(
    state, run_status, outcome
):
    projection = _project(_record(state=state, proof=f"work_state:{state}"))

    assert projection["status"] == "mapped"
    assert projection["task_status"] == "blocked"
    assert _run(projection)["status"] == run_status
    assert _run(projection)["outcome"] == outcome
    metadata = _run(projection)["metadata"]
    assert metadata["work_state"]["state"] == state
    assert metadata["work_state_outcome_preserved_in_metadata"] is True
    assert "metadata" not in projection.get("task", {})


def test_projected_run_statuses_and_outcomes_match_kanban_run_ledger_vocab():
    allowed_status = {"running", "done", "blocked", "crashed", "timed_out", "failed", "released"}
    allowed_outcome = {"completed", "blocked", "crashed", "timed_out", "spawn_failed", "gave_up", "reclaimed", None}

    for state in ["running", "finished", "blocked", "stale", "retry_needed", "handoff_needed", "failed"]:
        projection = _project(
            _record(
                state=state,
                usable_outcome="usable_output" if state == "finished" else None,
                close_disposition="close" if state == "finished" else None,
            )
        )
        run = _run(projection)
        assert run["status"] in allowed_status
        assert run["outcome"] in allowed_outcome


def test_finished_usable_output_records_completed_run_without_kanban_done_projection():
    projection = _project(
        _record(
            state="finished",
            usable_outcome="usable_output",
            close_disposition="close",
            next_action="Review output and update Linear SSOT",
            proof="tests passed and artifacts ready",
        )
    )

    assert projection["status"] == "mapped"
    assert projection["task_status"] == "blocked"
    assert _run(projection)["status"] == "done"
    assert _run(projection)["outcome"] == "completed"
    metadata = _run(projection)["metadata"]
    assert metadata["usable_output_recorded"] is True
    assert metadata["kanban_done_projection"] == "forbidden"
    assert metadata["kanban_task_status_reason"] == "linear_ssot_no_kanban_done_projection"


def test_ambiguous_resolution_fails_closed_to_blocked_run():
    projection = _project(
        _record(state="retry_needed"),
        resolution={"status": "ambiguous", "matches": [{"work_id": "a"}, {"work_id": "b"}]},
    )

    assert projection["status"] == "fail_closed"
    assert projection["reason"] == "ambiguous_recovery_metadata_fail_closed"
    assert projection["task_status"] == "blocked"
    assert _run(projection)["status"] == "blocked"
    assert _run(projection)["outcome"] == "blocked"
    assert _run(projection)["metadata"]["projection_failed_closed"] is True


def test_missing_recovery_metadata_fails_closed_to_blocked_run():
    projection = _project(
        _record(
            state="stale",
            next_action="",
            proof="",
            executor_session_id=None,
            tmux_session=None,
            repo_path=None,
            worktree_path=None,
        )
    )

    assert projection["status"] == "fail_closed"
    assert projection["reason"] == "missing_recovery_metadata_fail_closed"
    assert projection["task_status"] == "blocked"
    assert _run(projection)["status"] == "blocked"
    assert _run(projection)["outcome"] == "blocked"
    assert _run(projection)["metadata"]["missing"] == [
        "next_action",
        "proof",
        "executor_session_or_workspace_locator",
    ]


def test_missing_record_fails_closed_without_task_metadata():
    projection = _project(None)

    assert projection["status"] == "fail_closed"
    assert projection["reason"] == "missing_work_state_record_fail_closed"
    assert projection["task_status"] == "blocked"
    assert _run(projection)["metadata"]["projection_failed_closed"] is True
    assert "task" not in projection
