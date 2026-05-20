"""Regression tests for the Kanban-first /autopilot surface."""

from __future__ import annotations

import json


def test_autopilot_status_imports_and_reports_degraded_effective_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "gateway_autopilot_state.json").write_text(
        json.dumps({"version": 1, "enabled": True, "updated_by": "test"}),
        encoding="utf-8",
    )

    from gateway.kanban_autopilot import handle_autopilot_command

    result = handle_autopilot_command("status", actor="tester")

    assert result.ok is True
    assert result.decision["desired_mode"] == "enabled"
    assert result.decision["effective_mode"] in {"blocked", "degraded"}
    assert result.decision["effective_mode"] != "enabled"
    assert result.decision["state_file_enabled_is_execution_proof"] is False
    assert "desired_mode=enabled" in result.message
    assert "effective_mode=" in result.message
    assert "State file enabled=true is not execution proof" in result.message


def test_autopilot_status_is_read_only_and_does_not_touch_dispatch_or_kanban(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway import kanban_autopilot

    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden")
        raise AssertionError("status path must not dispatch, claim, spawn, or mutate Kanban")

    monkeypatch.setattr(kanban_autopilot, "dispatch_once", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "claim_task", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "spawn_worker", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "mutate_kanban", forbidden, raising=False)

    result = kanban_autopilot.handle_autopilot_command("status", actor="tester")

    assert result.ok is True
    assert result.decision["read_only"] is True
    assert result.decision["mutations_attempted"] == []
    assert calls == []


def test_autopilot_control_actions_persist_desired_state_without_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.kanban_autopilot import handle_autopilot_command

    result = handle_autopilot_command("on", actor="tester")

    assert result.ok is True
    assert result.fail_closed is False
    assert result.decision["desired_mode"] == "on"
    assert result.decision["effective_mode"] == "blocked"
    assert result.decision["state_file_enabled_is_execution_proof"] is False
    assert result.decision["mutations_attempted"] == []
    state = json.loads((tmp_path / "gateway_autopilot_state.json").read_text(encoding="utf-8"))
    assert state["desired_mode"] == "on"
    assert state["enabled"] is True
    assert state["updated_by"] == "tester"


def test_autopilot_pause_and_focus_update_controller_state_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway import kanban_autopilot

    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden")
        raise AssertionError("controller state changes must not dispatch, claim, spawn, or mutate Kanban")

    monkeypatch.setattr(kanban_autopilot, "dispatch_once", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "claim_task", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "spawn_worker", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "mutate_kanban", forbidden, raising=False)

    pause = kanban_autopilot.handle_autopilot_command("pause waiting-for-review", actor="tester")
    focus = kanban_autopilot.handle_autopilot_command("focus BO-076", actor="tester")
    status = kanban_autopilot.handle_autopilot_command("status", actor="tester")

    assert pause.ok is True
    assert pause.decision["desired_mode"] == "paused"
    assert pause.decision["pause_reason"] == "waiting-for-review"
    assert focus.ok is True
    assert focus.decision["focus"] == "BO-076"
    assert status.decision["desired_mode"] == "paused"
    assert status.decision["focus"] == "BO-076"
    assert status.decision["effective_mode"] == "paused"
    assert status.decision["mutations_attempted"] == []
    assert calls == []


def test_autopilot_stop_clears_enabled_without_claiming_runtime_proof(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.kanban_autopilot import handle_autopilot_command

    handle_autopilot_command("on", actor="tester")
    result = handle_autopilot_command("stop", actor="tester")

    assert result.ok is True
    assert result.decision["desired_mode"] == "stopped"
    assert result.decision["effective_mode"] == "stopped"
    assert result.decision["state_file_enabled_is_execution_proof"] is False
    state = json.loads((tmp_path / "gateway_autopilot_state.json").read_text(encoding="utf-8"))
    assert state["desired_mode"] == "stopped"
    assert state["enabled"] is False


def test_ready_gate_rejects_raw_kanban_ready_without_executable_contract():
    from gateway.kanban_autopilot import evaluate_autopilot_ready_gate

    candidate = {
        "id": "t_raw",
        "public_id": "BO-999",
        "status": "ready",
        "title": "raw ready task",
        "body": "Please handle this later.",
    }

    result = evaluate_autopilot_ready_gate(candidate)

    assert result["autopilot_ready"] is False
    assert result["status"] == "rejected"
    assert "missing_goal" in result["reason_codes"]
    assert "missing_acceptance_criteria" in result["reason_codes"]
    assert "missing_verification_requirements" in result["reason_codes"]
    assert result["human_reason"]


def test_ready_gate_accepts_native_contract_but_never_claims_or_spawns(monkeypatch):
    from gateway import kanban_autopilot

    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden")
        raise AssertionError("ready-gate dry-run must not claim, spawn, dispatch, or mutate Kanban")

    monkeypatch.setattr(kanban_autopilot, "dispatch_once", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "claim_task", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "spawn_worker", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "mutate_kanban", forbidden, raising=False)
    candidate = {
        "id": "t_good",
        "public_id": "BO-100",
        "status": "ready",
        "title": "Implement bounded thing",
        "body": """
Goal: implement a bounded local validator.
End-state/output: local commit candidate and Kanban evidence.
Scope/non-goals: no gateway restart and no production action.
Acceptance criteria: validator returns machine-readable reason codes.
Verification requirements: focused tests and git diff --check.
Authority boundary: Kanban BO-100 controls execution; PR/push forbidden.
Repo/lane truth: chriskim12/hermes-agent on a task branch.
Risk flags: no env, secret, prod, billing, customer-visible, or restart action.
Dependencies/blockers: none.
Review package expectation: changed files, tests, commit, boundaries.
""",
        "routing_verdict": {"verdict": "Hermes direct"},
        "admission_snapshot": {"repo_full_name": "chriskim12/hermes-agent"},
    }

    result = kanban_autopilot.evaluate_autopilot_ready_gate(candidate)

    assert result["autopilot_ready"] is True
    assert result["status"] == "accepted"
    assert result["reason_codes"] == []
    assert result["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0}
    assert calls == []


def test_autopilot_queue_dry_run_reports_gate_results_without_spawning(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.kanban_autopilot import handle_autopilot_command

    result = handle_autopilot_command("queue", actor="tester", candidates=[
        {"id": "t_raw", "public_id": "BO-999", "status": "ready", "title": "raw", "body": "raw"}
    ])

    assert result.ok is True
    assert result.decision["read_only"] is True
    assert result.decision["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0}
    assert result.decision["candidates"][0]["autopilot_ready"] is False
    assert "missing_goal" in result.decision["candidates"][0]["reason_codes"]


def _ready_candidate(public_id: str = "BO-100") -> dict:
    return {
        "id": "t_good",
        "public_id": public_id,
        "status": "ready",
        "title": "Implement bounded thing",
        "body": """
Goal: implement a bounded local validator.
End-state/output: local commit candidate and Kanban evidence.
Scope/non-goals: no gateway restart and no production action.
Acceptance criteria: validator returns machine-readable reason codes.
Verification requirements: focused tests and git diff --check.
Authority boundary: Kanban controls execution; PR/push forbidden.
Repo/lane truth: chriskim12/hermes-agent on a task branch.
Risk flags: no env, secret, prod, billing, customer-visible, or restart action.
Dependencies/blockers: none.
Review package expectation: changed files, tests, commit, boundaries.
""",
        "routing_verdict": {"verdict": "Hermes direct"},
        "admission_snapshot": {"repo_full_name": "chriskim12/hermes-agent"},
    }


def test_dispatcher_eligibility_bridge_requires_controller_on_and_ready_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.kanban_autopilot import evaluate_dispatcher_eligibility, handle_autopilot_command

    stopped = evaluate_dispatcher_eligibility([_ready_candidate()])
    assert stopped["eligible"] == []
    assert stopped["ineligible"][0]["reason_codes"] == ["autopilot_effective_mode_not_blocked_pending_dispatch_gate"]

    handle_autopilot_command("on", actor="tester")
    verdict = evaluate_dispatcher_eligibility([_ready_candidate(), {"id": "t_raw", "public_id": "BO-999", "status": "ready", "body": "raw"}])

    assert [item["public_id"] for item in verdict["eligible"]] == ["BO-100"]
    assert verdict["ineligible"][0]["public_id"] == "BO-999"
    assert "missing_goal" in verdict["ineligible"][0]["reason_codes"]
    assert verdict["handoff_target"] == "existing_kanban_dispatcher"
    assert verdict["second_dispatcher_created"] is False
    assert verdict["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0}


def test_autopilot_queue_includes_dispatcher_eligibility_without_dispatching(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway import kanban_autopilot

    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden")
        raise AssertionError("eligibility bridge must not call the dispatcher")

    monkeypatch.setattr(kanban_autopilot, "dispatch_once", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "claim_task", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "spawn_worker", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "mutate_kanban", forbidden, raising=False)

    kanban_autopilot.handle_autopilot_command("on", actor="tester")
    result = kanban_autopilot.handle_autopilot_command("queue", actor="tester", candidates=[_ready_candidate("BO-101")])

    assert result.ok is True
    assert result.decision["dispatcher_eligibility"]["eligible"][0]["public_id"] == "BO-101"
    assert result.decision["dispatcher_eligibility"]["handoff_target"] == "existing_kanban_dispatcher"
    assert result.decision["dispatcher_eligibility"]["second_dispatcher_created"] is False
    assert result.decision["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0}
    assert calls == []


def test_review_ready_contract_requires_pr_and_checks_without_merging():
    from gateway.kanban_autopilot import evaluate_review_ready_contract

    missing = evaluate_review_ready_contract({"work_id": "BO-081", "commit": "abc1234"})
    assert missing["review_ready"] is False
    assert "missing_pr_url" in missing["reason_codes"]
    assert "missing_checks_passed" in missing["reason_codes"]
    assert missing["merge_allowed"] is False
    assert missing["release_allowed"] is False

    satisfied = evaluate_review_ready_contract({
        "work_id": "BO-081",
        "repo_full_name": "chriskim12/hermes-agent",
        "commit": "8f0cbc56db6498e16c628e42430dbd6156d99fb3",
        "task_branch": "yuuka/bo-081-autopilot-review-ready-contract",
        "pr_url": "https://github.com/chriskim12/hermes-agent/pull/123",
        "pr_base": "main",
        "pr_head": "yuuka/bo-081-autopilot-review-ready-contract",
        "checks_passed": True,
        "worktree_clean": True,
        "kanban_worker_done": True,
        "boundaries_confirmed": True,
    })
    assert satisfied["review_ready"] is True
    assert satisfied["reason_codes"] == []
    assert satisfied["merge_allowed"] is False
    assert satisfied["release_allowed"] is False


def test_review_ready_contract_rejects_wrong_repo_and_release_base():
    from gateway.kanban_autopilot import evaluate_review_ready_contract

    result = evaluate_review_ready_contract({
        "work_id": "BO-081",
        "repo_full_name": "evil/repo",
        "commit": "8f0cbc56db6498e16c628e42430dbd6156d99fb3",
        "task_branch": "feature",
        "pr_url": "https://github.com/chriskim12/hermes-agent/pull/123",
        "pr_base": "prod",
        "pr_head": "other",
        "checks_passed": True,
        "worktree_clean": True,
        "kanban_worker_done": True,
        "boundaries_confirmed": True,
    }, expected_repo_full_name="chriskim12/hermes-agent")

    assert result["review_ready"] is False
    assert "repo_full_name_mismatch" in result["reason_codes"]
    assert "release_base_requires_separate_approval" in result["reason_codes"]
    assert "pr_head_task_branch_mismatch" in result["reason_codes"]
    assert result["merge_allowed"] is False
