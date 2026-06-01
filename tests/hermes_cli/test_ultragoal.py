"""Source-compatibility tests for Hermes Ultragoal port."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _authority(run_id="default"):
    return {
        "taskId": run_id,
        "runId": run_id,
        "status": "in_progress",
        "routingVerdict": "direct-kanban",
        "authority": "kanban",
    }


def _store(tmp_path, run_id="default"):
    from hermes_cli.ultragoal import UltragoalStore

    return UltragoalStore(tmp_path, run_id=run_id, kanban_snapshot=_authority(run_id))


def test_create_goals_writes_source_compatible_artifacts(tmp_path):
    from hermes_cli.ultragoal import DEFAULT_AGGREGATE_OBJECTIVE

    store = _store(tmp_path)
    plan = store.create_plan(
        brief="""# Launch plan\n\n## Story: First milestone\nShip the first thing.\n\n## Story: Second milestone\nVerify the second thing.\n""",
        force=False,
    )

    root = tmp_path / ".hermes" / "ultragoal" / "runs" / "default"
    assert (root / "brief.md").read_text() == plan.brief
    assert (root / "goals.json").exists()
    assert (root / "ledger.jsonl").exists()
    assert plan.version == 1
    assert plan.brief_path == ".hermes/ultragoal/runs/default/brief.md"
    assert plan.codex_goal_mode == "aggregate"
    assert plan.codex_objective == DEFAULT_AGGREGATE_OBJECTIVE
    assert [g.id for g in plan.goals] == [
        "G001-first-milestone",
        "G002-second-milestone",
    ]
    assert [json.loads(line)["event"] for line in (root / "ledger.jsonl").read_text().splitlines()] == [
        "plan_created"
    ]


def test_create_goals_refuses_overwrite_without_force(tmp_path):
    store = _store(tmp_path)
    store.create_plan(brief="- one", force=False)

    with pytest.raises(FileExistsError):
        store.create_plan(brief="- two", force=False)


def test_complete_goals_resumes_active_and_skips_superseded_blocked(tmp_path):
    store = _store(tmp_path)
    store.create_plan(brief="- One\n- Two\n- Three", force=False)
    first = store.start_next_goal()
    second_call = store.start_next_goal()
    assert first.id == second_call.id
    assert second_call.status == "in_progress"

    plan = store.load_plan()
    plan.goals[0].status = "complete"
    plan.goals[1].status = "pending"
    plan.goals[1].steering_status = "superseded"
    plan.goals[2].status = "pending"
    plan.goals[2].steering_status = "blocked"
    store.save_plan(plan)

    assert store.start_next_goal() is None


def test_final_checkpoint_requires_quality_gate_and_complete_goal_snapshot(tmp_path):
    store = _store(tmp_path)
    goal = store.create_plan(brief="- Only story", force=False).goals[0]
    store.start_next_goal()

    with pytest.raises(ValueError, match="quality gate"):
        store.checkpoint(
            goal_id=goal.id,
            status="complete",
            evidence="done",
            hermes_goal_snapshot={"status": "complete", "goal": "Hermes Ultragoal"},
        )

    with pytest.raises(ValueError, match="complete Hermes goal snapshot"):
        store.checkpoint(
            goal_id=goal.id,
            status="complete",
            evidence="done",
            hermes_goal_snapshot={"status": "active", "goal": "Hermes Ultragoal"},
            quality_gate={
                "aiSlopCleaner": {"status": "passed", "evidence": "clean"},
                "verification": {"status": "passed", "commands": ["pytest"], "evidence": "ok"},
                "codeReview": {
                    "recommendation": "APPROVE",
                    "architectStatus": "CLEAR",
                    "evidence": "ok",
                    "independentReview": {
                        "codeReviewer": {"agentRole": "code-reviewer", "evidence": "ok"},
                        "architect": {"agentRole": "architect", "evidence": "ok"},
                    },
                },
            },
        )


def test_checkpoint_completes_aggregate_with_quality_gate(tmp_path):
    store = _store(tmp_path)
    goal = store.create_plan(brief="- Only story", force=False).goals[0]
    store.start_next_goal()
    plan = store.checkpoint(
        goal_id=goal.id,
        status="complete",
        evidence="done",
        hermes_goal_snapshot={"status": "complete", "goal": "Hermes Ultragoal"},
        quality_gate={
            "aiSlopCleaner": {"status": "passed", "evidence": "clean"},
            "verification": {"status": "passed", "commands": ["pytest"], "evidence": "ok"},
            "codeReview": {
                "recommendation": "APPROVE",
                "architectStatus": "CLEAR",
                "evidence": "ok",
                "independentReview": {
                    "codeReviewer": {"agentRole": "code-reviewer", "evidence": "ok"},
                    "architect": {"agentRole": "architect", "evidence": "ok"},
                },
            },
        },
    )

    assert plan.goals[0].status == "complete"
    assert plan.aggregate_completion is not None
    assert plan.aggregate_completion["status"] == "complete"
    ledger_events = [json.loads(line)["event"] for line in store.ledger_path.read_text().splitlines()]
    assert ledger_events[-1] == "aggregate_completed"


def test_slug_collisions_preserve_distinct_goals(tmp_path):
    store = _store(tmp_path)
    plan = store.create_plan(
        brief="irrelevant",
        goals=[("A/B", "first objective"), ("A B", "second objective")],
        force=False,
    )

    assert [g.title for g in plan.goals] == ["A/B", "A B"]
    assert [g.id for g in plan.goals] == ["G001-a-b", "G002-a-b"]



def test_steering_requires_structured_evidence_and_audits_rejection(tmp_path):
    store = _store(tmp_path)
    store.create_plan(brief="- One", force=False)

    with pytest.raises(ValueError, match="evidence"):
        store.apply_steering({"kind": "add_subgoal", "title": "Two", "objective": "Do two"})

    events = [json.loads(line)["event"] for line in store.ledger_path.read_text().splitlines()]
    assert events[-1] == "steering_rejected"

    plan = store.apply_steering(
        {
            "kind": "add_subgoal",
            "title": "Two",
            "objective": "Do two",
            "evidence": "new requirement from review",
            "rationale": "needed to resolve blocker",
            "idempotencyKey": "add-two",
        }
    )
    assert plan.goals[-1].id == "G002-two"
    again = store.apply_steering(
        {
            "kind": "add_subgoal",
            "title": "Two duplicate",
            "objective": "Do two again",
            "evidence": "new requirement from review",
            "rationale": "needed to resolve blocker",
            "idempotencyKey": "add-two",
        }
    )
    assert len(again.goals) == 2

    annotated = store.apply_steering(
        {
            "kind": "annotate_ledger",
            "message": "Reviewer approved current evidence",
            "evidence": "review transcript",
            "rationale": "preserve review note",
            "idempotencyKey": "annotate-review",
        }
    )
    assert len(annotated.goals) == 2
    ledger = [json.loads(line) for line in store.ledger_path.read_text().splitlines()]
    assert ledger[-2]["event"] == "ledger_annotated"
    assert ledger[-1]["event"] == "steering_accepted"


def test_record_review_blockers_creates_blocker_goal(tmp_path):
    store = _store(tmp_path)
    goal = store.create_plan(brief="- Final story", force=False).goals[0]
    store.start_next_goal()
    plan = store.record_review_blockers(
        goal_id=goal.id,
        title="Fix reviewer blockers",
        objective="Resolve independent review findings",
        evidence="reviewer requested changes",
        hermes_goal_snapshot={"status": "active", "goal": "Hermes Ultragoal"},
    )

    assert plan.goals[0].status == "review_blocked"
    assert plan.goals[1].status == "pending"
    assert plan.goals[1].title == "Fix reviewer blockers"
    assert plan.active_goal_id is None


def test_failed_and_blocked_checkpoints_clear_active_goal(tmp_path):
    store = _store(tmp_path)
    first, second = store.create_plan(brief="- One\n- Two", force=False).goals
    store.start_next_goal()

    failed_plan = store.checkpoint(goal_id=first.id, status="failed", evidence="tooling failed")
    assert failed_plan.active_goal_id is None
    assert failed_plan.goals[0].status == "failed"

    next_goal = store.start_next_goal()
    assert next_goal.id == second.id
    blocked_plan = store.checkpoint(goal_id=second.id, status="blocked", evidence="external auth blocked")
    assert blocked_plan.active_goal_id is None
    assert blocked_plan.goals[1].status == "failed"
    assert blocked_plan.goals[1].steering_status == "blocked"
    assert store.start_next_goal() is None



def test_kanban_authority_snapshot_is_fail_closed(tmp_path):
    from hermes_cli.ultragoal import UltragoalStore, reconcile_kanban_authority

    store = _store(tmp_path, run_id="BO-203")
    store.create_plan(brief="- One", force=False)

    bare_store = UltragoalStore(tmp_path / "bare", run_id="BO-204")
    with pytest.raises(ValueError, match="Kanban authority snapshot"):
        bare_store.create_plan(brief="- One", force=False)

    with pytest.raises(ValueError, match="Kanban authority snapshot"):
        reconcile_kanban_authority(store.load_plan(), None)

    with pytest.raises(ValueError, match="runId mismatch"):
        reconcile_kanban_authority(
            store.load_plan(),
            {"taskId": "BO-203", "runId": "other", "status": "in_progress"},
        )

    with pytest.raises(ValueError, match="routing"):
        reconcile_kanban_authority(
            store.load_plan(),
            {"taskId": "BO-203", "runId": "BO-203", "status": "in_progress", "authority": "kanban"},
        )

    with pytest.raises(ValueError, match="authority"):
        reconcile_kanban_authority(
            store.load_plan(),
            {"taskId": "BO-203", "runId": "BO-203", "status": "in_progress", "routingVerdict": "direct-kanban"},
        )

    with pytest.raises(ValueError, match="run_id"):
        UltragoalStore(tmp_path, run_id="../../escape", kanban_snapshot=_authority("../../escape"))

    nested_runs = tmp_path / "outer" / "runs" / "workspace"
    nested_store = _store(nested_runs, run_id="BO-205")
    nested_store.create_plan(brief="- One", force=False)
    assert reconcile_kanban_authority(nested_store.load_plan(), _authority("BO-205"))["runId"] == "BO-205"

    assert reconcile_kanban_authority(
        store.load_plan(),
        {
            "taskId": "BO-203",
            "runId": "BO-203",
            "status": "in_progress",
            "routingVerdict": "direct-kanban",
            "authority": "kanban",
        },
    )["authority"] == "kanban"



def test_cli_help_preserves_ultragoal_source_contract(capsys):
    from hermes_cli.ultragoal import build_parser
    import argparse

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    build_parser(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["ultragoal", "--help"])
    help_text = capsys.readouterr().out
    assert "create-goals" in help_text
    assert "complete-goals" in help_text
    assert "record-review-blockers" in help_text
    assert "aggregate mode is the default" in help_text
    assert "/goal clear" in help_text
