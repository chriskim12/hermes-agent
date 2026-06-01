from __future__ import annotations

from hermes_cli.kanban_ready_contract import READY_CONTRACT_SCHEMA, validate_ready_contract


def valid_ready_contract(**overrides):
    contract = {
        "schema": READY_CONTRACT_SCHEMA,
        "goal": "Implement a bounded Kanban task.",
        "end_state": "Worker evidence reaches verifier handoff.",
        "scope": ["repo-local implementation"],
        "non_goals": ["gateway restart", "prod mutation"],
        "repo_lane_truth": {"repository": "chriskim12/hermes-agent", "branch": "main", "workspace": "worktree"},
        "routing_verdict": {"verdict": "direct-kanban", "assignee": "arisu", "reason": "ready contract complete"},
        "authority_boundary": {"allowed": ["code edits", "tests", "PR"], "forbidden": ["gateway restart", "prod mutation"]},
        "risk_flags": {"env": "none", "secret": "none", "prod": "none", "customer_visible": "none", "restart": "forbidden"},
        "dependencies_blockers": {"kind": "none"},
        "acceptance_criteria": [{"id": "ac-01", "text": "Contract validates."}],
        "done_criteria": [{"id": "dc-01", "text": "Tests pass."}],
        "verification_requirements": [{"id": "vr-01", "command_or_proof": "pytest"}],
        "review_package_expectation": {"changed_files_expected": True, "pr_expected": True},
        "reviewer_loop": {"required": True, "reviewer_profile": "verifier"},
    }
    contract.update(overrides)
    return contract


def test_validate_ready_contract_accepts_complete_governed_contract():
    result = validate_ready_contract(
        valid_ready_contract(),
        goal_mode=True,
        assignee="arisu",
        profile_exists=lambda name: name in {"arisu", "verifier"},
    )

    assert result.accepted is True
    assert result.reason_codes == []
    assert result.ready_contract["schema"] == READY_CONTRACT_SCHEMA


def test_validate_ready_contract_rejects_reviewer_loop_without_goal_mode():
    result = validate_ready_contract(
        valid_ready_contract(),
        goal_mode=False,
        assignee="arisu",
        profile_exists=lambda name: True,
    )

    assert result.accepted is False
    assert "ready_contract_reviewer_loop_requires_goal_mode" in result.reason_codes


def test_validate_ready_contract_rejects_unknown_assignee_and_reviewer():
    result = validate_ready_contract(
        valid_ready_contract(),
        goal_mode=True,
        assignee="missing-worker",
        profile_exists=lambda name: False,
    )

    assert result.accepted is False
    assert "unknown_ready_contract_assignee" in result.reason_codes
    assert "unknown_ready_contract_reviewer_profile" in result.reason_codes


def test_validate_ready_contract_rejects_empty_marker_only_payload():
    result = validate_ready_contract({}, goal_mode=True, assignee="arisu")

    assert result.accepted is False
    assert result.reason_codes == ["missing_structured_ready_contract"]
