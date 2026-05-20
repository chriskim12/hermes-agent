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
