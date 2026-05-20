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


def test_hard_stop_pauses_lane_and_blocks_eligibility_until_recovered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.kanban_autopilot import evaluate_dispatcher_eligibility, handle_autopilot_command

    handle_autopilot_command("on", actor="tester")
    stopped = handle_autopilot_command("hard-stop prod_action_detected", actor="tester")
    assert stopped.ok is True
    assert stopped.decision["desired_mode"] == "hard_stopped"
    assert stopped.decision["effective_mode"] == "hard_stop"
    assert stopped.decision["dispatch_blocked"] is True
    assert stopped.decision["operator_recovery_required"] is True

    verdict = evaluate_dispatcher_eligibility([_ready_candidate("BO-082")])
    assert verdict["eligible"] == []
    assert verdict["ineligible"][0]["reason_codes"] == ["autopilot_hard_stop_active"]
    assert verdict["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0}

    recovered = handle_autopilot_command("recover acknowledge-no-prod-action", actor="tester")
    assert recovered.ok is True
    assert recovered.decision["desired_mode"] == "paused"
    assert recovered.decision["effective_mode"] == "paused"
    assert recovered.decision["dispatch_blocked"] is True


def test_lane_pause_blocks_matching_tenant_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.kanban_autopilot import evaluate_dispatcher_eligibility, handle_autopilot_command

    handle_autopilot_command("on", actor="tester")
    paused = handle_autopilot_command("pause-lane autopilot overloaded", actor="tester")
    assert paused.ok is True
    assert paused.decision["paused_lanes"] == ["autopilot"]

    blocked = _ready_candidate("BO-082")
    blocked["tenant"] = "autopilot"
    allowed = _ready_candidate("BO-083")
    allowed["tenant"] = "other"
    verdict = evaluate_dispatcher_eligibility([blocked, allowed])

    assert [item["public_id"] for item in verdict["eligible"]] == ["BO-083"]
    assert verdict["ineligible"][0]["public_id"] == "BO-082"
    assert verdict["ineligible"][0]["reason_codes"] == ["autopilot_lane_paused"]
    assert verdict["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0}


def test_closed_loop_operating_contract_documents_state_caps_boundaries_and_future_ralplan_gate():
    from gateway.kanban_autopilot import get_closed_loop_operating_contract

    contract = get_closed_loop_operating_contract()

    assert contract["adr"] == "bounded_controller_not_executor"
    assert contract["authority_ceiling"] == "review_ready_pr"
    assert contract["dispatcher_boundary"]["autopilot_may_directly_claim_or_spawn"] is False
    assert contract["dispatcher_boundary"]["execution_owner"] == "existing_kanban_dispatcher"
    assert contract["state_machine"]["allowed_states"] == [
        "disabled",
        "dry_run",
        "single_flight",
        "bounded_multi_tick",
        "parent_scoped",
        "lane_scoped",
        "paused",
        "hard_stopped",
        "needs_human",
    ]
    assert contract["default_caps"]["max_dispatches_per_tick"] == 1
    assert contract["default_caps"]["max_open_autopilot_prs"] == 2
    assert "gateway_restart_reload" in contract["forbidden_without_current_approval"]
    assert "config_env_secret_provider_billing_pricing_mutation" in contract["forbidden_without_current_approval"]
    assert "policy_file_invalid_or_stale" in contract["stop_conditions"]
    assert "merge_release_deploy_prod_customer_visible_authority" in contract["future_ralplan_required_for"]


def test_closed_loop_policy_contract_validator_rejects_second_dispatcher_and_scope_expansion():
    from gateway.kanban_autopilot import (
        get_closed_loop_operating_contract,
        validate_closed_loop_policy_contract,
    )

    valid = validate_closed_loop_policy_contract(get_closed_loop_operating_contract())
    assert valid["ok"] is True
    assert valid["reason_codes"] == []

    unsafe = get_closed_loop_operating_contract()
    unsafe["dispatcher_boundary"] = {
        **unsafe["dispatcher_boundary"],
        "autopilot_may_directly_claim_or_spawn": True,
        "second_dispatcher_allowed": True,
    }
    unsafe["authority_ceiling"] = "merge_release_deploy"
    unsafe["scope_model"]["scope_can_silently_widen"] = True

    result = validate_closed_loop_policy_contract(unsafe)

    assert result["ok"] is False
    assert "direct_claim_or_spawn_not_allowed" in result["reason_codes"]
    assert "second_dispatcher_not_allowed" in result["reason_codes"]
    assert "authority_ceiling_must_be_review_ready_pr" in result["reason_codes"]
    assert "scope_must_not_silently_widen" in result["reason_codes"]


def test_closed_loop_simulator_selects_one_candidate_and_never_dispatches(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway import kanban_autopilot

    calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        calls.append("forbidden")
        raise AssertionError("read-only simulator must not dispatch, claim, spawn, or mutate Kanban")

    monkeypatch.setattr(kanban_autopilot, "dispatch_once", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "claim_task", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "spawn_worker", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "mutate_kanban", forbidden, raising=False)
    kanban_autopilot.handle_autopilot_command("on", actor="tester")

    report = kanban_autopilot.simulate_closed_loop_ticks(
        [_ready_candidate("BO-092"), {"id": "t_raw", "public_id": "BO-999", "status": "ready", "body": "raw"}],
        max_ticks=2,
    )

    assert report["mode"] == "read_only_simulation"
    assert report["would_select"][0]["public_id"] == "BO-092"
    assert report["would_handoff"][0]["target"] == "existing_kanban_dispatcher"
    assert report["would_handoff"][0]["check_only"] is True
    assert report["would_skip"][0]["public_id"] == "BO-999"
    assert "missing_goal" in report["would_skip"][0]["reason_codes"]
    assert report["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0, "dispatched": 0}
    assert report["mutations_attempted"] == []
    assert calls == []


def test_closed_loop_simulator_pauses_on_hard_stop_lane_pause_and_no_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.kanban_autopilot import handle_autopilot_command, simulate_closed_loop_ticks

    handle_autopilot_command("on", actor="tester")
    handle_autopilot_command("pause-lane autopilot overloaded", actor="tester")
    lane_candidate = _ready_candidate("BO-092")
    lane_candidate["tenant"] = "autopilot"
    lane_report = simulate_closed_loop_ticks([lane_candidate], max_ticks=2)
    assert lane_report["would_pause"][0]["reason_code"] == "no_progress"
    assert lane_report["would_skip"][0]["reason_codes"] == ["autopilot_lane_paused"]

    handle_autopilot_command("hard-stop forbidden_action", actor="tester")
    hard_report = simulate_closed_loop_ticks([_ready_candidate("BO-093")], max_ticks=2)
    assert hard_report["would_pause"][0]["reason_code"] == "hard_stop"
    assert hard_report["would_select"] == []
    assert hard_report["would_handoff"] == []

    handle_autopilot_command("recover acknowledged", actor="tester")
    blocked_report = simulate_closed_loop_ticks([{"id": "t_raw", "public_id": "BO-999", "status": "ready", "body": "raw"}], max_ticks=2)
    assert blocked_report["would_pause"][0]["reason_code"] == "no_progress"
    assert blocked_report["next_state"] == "needs_human"


def test_single_flight_activation_runs_one_check_only_handoff_and_no_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway import kanban_autopilot

    forbidden_calls: list[str] = []

    def forbidden(*_args, **_kwargs):
        forbidden_calls.append("forbidden")
        raise AssertionError("single-flight activation must not directly dispatch, claim, spawn, or mutate Kanban")

    monkeypatch.setattr(kanban_autopilot, "dispatch_once", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "claim_task", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "spawn_worker", forbidden, raising=False)
    monkeypatch.setattr(kanban_autopilot, "mutate_kanban", forbidden, raising=False)
    kanban_autopilot.handle_autopilot_command("on", actor="tester")
    checks: list[dict] = []

    def check_only(payload: dict) -> dict:
        checks.append(payload)
        return {"allowed": True, "reason": "mock_check_passed"}

    result = kanban_autopilot.activate_single_flight([_ready_candidate("BO-093"), _ready_candidate("BO-094")], check_only_handoff=check_only)

    assert result["status"] == "handoff_check_passed"
    assert result["selected"]["public_id"] == "BO-093"
    assert result["handoff"]["target"] == "existing_kanban_dispatcher"
    assert result["handoff"]["check_only"] is True
    assert result["handoff"]["would_dispatch"] is False
    assert result["handoff_success_is_worker_completion"] is False
    assert len(checks) == 1
    assert checks[0]["public_id"] == "BO-093"
    assert result["skipped"][0]["reason_codes"] == ["single_flight_limit_reached"]
    assert result["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0, "dispatched": 0}
    assert forbidden_calls == []


def test_single_flight_activation_blocks_on_failed_check_without_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from gateway.kanban_autopilot import activate_single_flight, handle_autopilot_command

    handle_autopilot_command("on", actor="tester")

    result = activate_single_flight(
        [_ready_candidate("BO-093")],
        check_only_handoff=lambda payload: {"allowed": False, "reason": "mock_dispatcher_unavailable"},
    )

    assert result["status"] == "handoff_check_blocked"
    assert result["next_state"] == "needs_human"
    assert result["handoff_success_is_worker_completion"] is False
    assert result["worker_done_observed"] is False
    assert result["dry_run_side_effects"] == {"claimed": 0, "spawned": 0, "mutated": 0, "dispatched": 0}


def test_closeout_progress_gate_blocks_missing_evidence_and_pr_backlog():
    from gateway.kanban_autopilot import evaluate_autopilot_closeout_progress

    missing = evaluate_autopilot_closeout_progress({"work_id": "BO-094", "kanban_worker_done": True})
    assert missing["may_continue"] is False
    assert "missing_review_ready_contract" in missing["reason_codes"]

    good = {
        "work_id": "BO-094",
        "repo_full_name": "chriskim12/hermes-agent",
        "commit": "8f0cbc56db6498e16c628e42430dbd6156d99fb3",
        "task_branch": "bo-094",
        "pr_url": "https://github.com/chriskim12/hermes-agent/pull/123",
        "pr_base": "main",
        "pr_head": "bo-094",
        "checks_passed": True,
        "worktree_clean": True,
        "kanban_worker_done": True,
        "boundaries_confirmed": True,
    }
    backlog_blocked = evaluate_autopilot_closeout_progress(good, open_autopilot_prs=2, max_open_autopilot_prs=2)
    assert backlog_blocked["may_continue"] is False
    assert "pr_backlog_cap_reached" in backlog_blocked["reason_codes"]

    allowed = evaluate_autopilot_closeout_progress(good, open_autopilot_prs=1, max_open_autopilot_prs=2)
    assert allowed["may_continue"] is True
    assert allowed["worker_done_observed"] is True
    assert allowed["review_ready_contract"]["review_ready"] is True
    assert allowed["merge_allowed"] is False


def test_closeout_progress_gate_allows_explicit_no_code_evidence_without_pr():
    from gateway.kanban_autopilot import evaluate_autopilot_closeout_progress

    result = evaluate_autopilot_closeout_progress({
        "work_id": "BO-094",
        "no_code_task": True,
        "artifact_path": "docs/closed-loop-kanban-autopilot.md",
        "verification": "documentation reviewed and tests not applicable",
        "kanban_worker_done": True,
        "boundaries_confirmed": True,
    })

    assert result["may_continue"] is True
    assert result["review_ready_equivalent"] == "no_code_evidence"
    assert result["merge_allowed"] is False
