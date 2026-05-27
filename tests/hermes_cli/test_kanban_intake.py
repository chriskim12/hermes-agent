from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.kanban_intake import (
    IntakeLifecycle,
    IntakeOutcome,
    IntakeRequest,
    evaluate_intake_request,
)
from tools import kanban_intake_tool


def test_ready_request_requires_approval_before_handoff() -> None:
    result = evaluate_intake_request(
        IntakeRequest(
            goal="Build a reusable Discord intake command contract implementation with tests",
            project="bo",
            acceptance_criteria=["core logic is independent of Discord", "no executor dispatch"],
        ),
        lifecycle=IntakeLifecycle.DRAFT,
        kanban_available=True,
        approve_admission=False,
    )

    assert result.outcome == IntakeOutcome.APPROVAL_REQUIRED
    assert result.kanban_admission_handoff is None
    assert result.dispatch_allowed is False
    assert result.reply.startswith("Seed Contract draft is ready")
    assert result.draft is not None
    assert result.draft["namespace"] == "BO"
    assert result.draft["executor_dispatch"] == "forbidden_during_admission"


def test_approved_ready_request_produces_admission_handoff_without_executor_dispatch() -> None:
    result = evaluate_intake_request(
        IntakeRequest(
            goal="Create a Hermes Brain OS task intake admission surface with policy gates",
            project="bo",
            tenant="ops-core",
            acceptance_criteria=["structured outcomes", "admission-only handoff"],
            side_effect_boundary="No gateway restart, repo mutation, PR, or worker dispatch.",
        ),
        lifecycle=IntakeLifecycle.ADMIT,
        kanban_available=True,
        approve_admission=True,
    )

    assert result.outcome == IntakeOutcome.SUCCESS
    assert result.dispatch_allowed is False
    assert result.kanban_admission_handoff is not None
    assert result.kanban_admission_handoff["status"] == "triage"
    assert result.kanban_admission_handoff["assignee"] is None
    assert result.kanban_admission_handoff["tenant"] == "kanban"
    assert result.kanban_admission_handoff["metadata"]["executor_dispatch"] == "forbidden_during_admission"
    assert "Invoking this Discord intake command is not execution approval." in result.kanban_admission_handoff["body"]
    assert "gateway reload/restart require separate human approval" in result.kanban_admission_handoff["body"]


def test_ambiguous_input_returns_focused_questions_and_no_card() -> None:
    result = evaluate_intake_request(
        IntakeRequest(
            goal="Fix routing",
            project="dc",
        ),
        lifecycle=IntakeLifecycle.INTERVIEW,
        kanban_available=True,
    )

    assert result.outcome == IntakeOutcome.AMBIGUOUS_INPUT
    assert result.kanban_admission_handoff is None
    assert len(result.questions) >= 2
    assert "카드는 만들지 않았습니다" in result.reply


def test_policy_block_blocks_live_actions_inside_intake() -> None:
    result = evaluate_intake_request(
        IntakeRequest(
            goal="Restart the Hermes gateway and push a PR to ship the Discord intake command",
            project="bo",
            acceptance_criteria=["restart gateway now"],
        ),
        lifecycle=IntakeLifecycle.ADMIT,
        kanban_available=True,
        approve_admission=True,
    )

    assert result.outcome == IntakeOutcome.BLOCKED_POLICY
    assert result.kanban_admission_handoff is None
    assert result.dispatch_allowed is False
    assert any("gateway" in reason.lower() for reason in result.reasons)
    assert "Intake는 실행 승인이 아닙니다" in result.reply


def test_kanban_unavailable_is_explicit_before_approved_handoff() -> None:
    result = evaluate_intake_request(
        IntakeRequest(
            goal="Build a Brain OS admission-only intake card with tests and policy metadata",
            project="bo",
            acceptance_criteria=["return structured outcomes", "do not dispatch"],
        ),
        lifecycle=IntakeLifecycle.ADMIT,
        kanban_available=False,
        approve_admission=True,
    )

    assert result.outcome == IntakeOutcome.KANBAN_UNAVAILABLE
    assert result.kanban_admission_handoff is None
    assert "Kanban is unavailable" in result.reply


def test_tool_wrapper_accepts_parsed_request_and_returns_structured_json() -> None:
    raw = kanban_intake_tool.kanban_intake_tool(
        request={
            "goal": "Design an admission-only intake handoff for Brain OS with lifecycle tests",
            "project": "bo",
            "acceptance_criteria": ["independent core logic", "structured Discord-ready replies"],
        },
        lifecycle="draft",
        kanban_available=True,
        approve_admission=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["outcome"] == "approval_required"
    assert payload["dispatch_allowed"] is False
    assert payload["kanban_admission_handoff"] is None
    assert payload["draft"]["namespace"] == "BO"


def test_tool_default_kanban_probe_does_not_create_missing_db(monkeypatch, tmp_path: Path) -> None:
    missing_db = tmp_path / "nested" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(missing_db))

    raw = kanban_intake_tool.kanban_intake_tool(
        request={
            "goal": "Create an admission-only intake handoff for Brain OS with tests",
            "project": "bo",
            "acceptance_criteria": ["structured unavailable result"],
        },
        lifecycle="admit",
        approve_admission=True,
    )
    payload = json.loads(raw)

    assert payload["outcome"] == "kanban_unavailable"
    assert not missing_db.exists()
    assert not missing_db.parent.exists()
