"""Durable Kanban-Ultragoal controller tests.

These tests encode RALPLAN v2: Kanban remains authority, a controller run
checkpoints/resumes across bounded ticks, and PR readiness is impossible before
verifier + reviewer + PR/CI evidence exists.
"""

from __future__ import annotations

import json

import pytest


def _authority(task_id="BO-203", *, snapshot_hash="sha256:a", done_hash="sha256:d", status="triage"):
    return {
        "authority": "kanban",
        "taskId": task_id,
        "publicId": task_id,
        "status": status,
        "routingVerdict": "direct-kanban",
        "executionApproved": True,
        "snapshotHash": snapshot_hash,
        "doneCriteriaHash": done_hash,
    }


def test_start_creates_canonical_run_root_with_subordinate_ultragoal_root(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    run = store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")

    root = tmp_path / ".hermes" / "goal-runs" / "BO-203"
    assert run.run_id == "BO-203"
    assert (root / "run.json").exists()
    assert (root / "authority.json").exists()
    assert (root / "ledger.jsonl").exists()
    assert (root / "ultragoal" / "goals.json").exists()
    assert json.loads((root / "authority.json").read_text())["snapshotHash"] == "sha256:a"


def test_run_id_rejects_path_traversal(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        store.start("../../escape", authority=_authority("../../escape"), root_objective="bad")


def test_mutating_tick_requires_fresh_kanban_authority_match(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")

    with pytest.raises(ValueError, match="snapshotHash"):
        store.tick("BO-203", authority=_authority(snapshot_hash="sha256:changed"))

    with pytest.raises(ValueError, match="taskId"):
        store.tick("BO-203", authority=_authority("BO-999"))


def test_budget_exhaustion_writes_resumable_pending_action(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")
    run = store.tick("BO-203", authority=_authority(), budget_remaining=0)

    assert run.state == "running"
    assert run.resumable is True
    assert run.pending_action is not None
    assert run.pending_action["phase"] == "prepared"
    assert run.pending_action["stepId"].startswith("BO-203:tick-")
    events = [json.loads(line)["event"] for line in store.ledger_path("BO-203").read_text().splitlines()]
    assert events[-1] == "checkpoint_budget_near_limit"


def test_controller_transitions_block_pr_until_verifier_and_reviewer_pass(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")

    run = store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": []})
    assert run.state == "worker_done"

    with pytest.raises(ValueError, match="reviewed PR gate"):
        store.record_pr_created("BO-203", authority=_authority(), pr={"url": "https://example.invalid/pr/1"})

    run = store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": False, "missing": [{"criterionId": "DC-1", "reason": "no test"}]},
    )
    assert run.state == "verification_failed"
    assert run.current_goal_id is not None

    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": ["pytest"]})
    run = store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": True, "doneCriteriaEvidence": [{"criterionId": "DC-1", "evidence": "pytest"}]},
    )
    assert run.state == "verification_passed"

    run = store.record_reviewer_result(
        "BO-203",
        authority=_authority(),
        result={"recommendation": "REQUEST_CHANGES", "securityConcerns": [], "logicErrors": ["missing resume test"]},
    )
    assert run.state == "review_failed"
    assert run.current_goal_id is not None

    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": ["pytest"]})
    store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": True, "doneCriteriaEvidence": [{"criterionId": "DC-1", "evidence": "pytest"}]},
    )
    run = store.record_reviewer_result(
        "BO-203",
        authority=_authority(),
        result={"recommendation": "APPROVE", "securityConcerns": [], "logicErrors": []},
    )
    assert run.state == "review_passed"

    run = store.record_pr_created(
        "BO-203",
        authority=_authority(),
        pr={"url": "https://github.com/chriskim12/hermes-agent/pull/1", "number": 1, "headSha": "abc"},
    )
    assert run.state == "pr_created"


def test_pr_ready_requires_complete_artifact_package(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")
    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": ["pytest"]})
    store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": True, "doneCriteriaEvidence": [{"criterionId": "DC-1", "evidence": "pytest"}]},
    )
    store.record_reviewer_result(
        "BO-203",
        authority=_authority(),
        result={"recommendation": "APPROVE", "securityConcerns": [], "logicErrors": []},
    )
    store.record_pr_created(
        "BO-203",
        authority=_authority(),
        pr={"url": "https://github.com/chriskim12/hermes-agent/pull/1", "number": 1, "headSha": "abc"},
    )

    with pytest.raises(ValueError, match="CI"):
        store.mark_review_ready("BO-203", authority=_authority())

    run = store.record_ci_result("BO-203", authority=_authority(), ci={"state": "success", "headSha": "abc"})
    assert run.state == "ci_passed"
    run = store.mark_review_ready("BO-203", authority=_authority())
    assert run.state == "review_ready"
    package = json.loads((tmp_path / ".hermes" / "goal-runs" / "BO-203" / "pr.json").read_text())
    assert package["url"].endswith("/1")


def test_force_start_clears_stale_ledger_and_evidence(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="first")
    stale = tmp_path / ".hermes" / "goal-runs" / "BO-203" / "evidence" / "old.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": []})

    store.start("BO-203", authority=_authority(), root_objective="second", force=True)

    assert not stale.exists()
    events = [json.loads(line)["event"] for line in store.ledger_path("BO-203").read_text().splitlines()]
    assert events == ["run_started"]


def test_state_regressions_are_rejected_after_later_phases(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")
    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": ["pytest"]})
    store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": True, "doneCriteriaEvidence": [{"criterionId": "DC-1", "evidence": "pytest"}]},
    )

    with pytest.raises(ValueError, match="worker_done transition"):
        store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": []})


def test_reviewer_approval_requires_explicit_empty_blocker_lists(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")
    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": ["pytest"]})
    store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": True, "doneCriteriaEvidence": [{"criterionId": "DC-1", "evidence": "pytest"}]},
    )

    with pytest.raises(ValueError, match="securityConcerns"):
        store.record_reviewer_result("BO-203", authority=_authority(), result={"recommendation": "APPROVE"})


def test_start_with_stale_root_without_run_json_is_fail_closed_and_force_cleans(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    root = tmp_path / ".hermes" / "goal-runs" / "BO-203"
    stale = root / "evidence" / "old.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")

    with pytest.raises(FileExistsError):
        store.start("BO-203", authority=_authority(), root_objective="first")

    store.start("BO-203", authority=_authority(), root_objective="clean", force=True)
    assert not stale.exists()
    assert (root / "run.json").exists()


def test_rejected_reviewer_payload_is_not_persisted(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")
    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": ["pytest"]})
    store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": True, "doneCriteriaEvidence": [{"criterionId": "DC-1", "evidence": "pytest"}]},
    )

    with pytest.raises(ValueError, match="securityConcerns"):
        store.record_reviewer_result("BO-203", authority=_authority(), result={"recommendation": "APPROVE"})
    assert not (tmp_path / ".hermes" / "goal-runs" / "BO-203" / "reviews" / "final.json").exists()


def test_authority_requires_exact_direct_kanban_routing(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    bad = _authority()
    bad["routingVerdict"] = "kanban-ultragoal"
    with pytest.raises(ValueError, match="routing"):
        store.start("BO-203", authority=bad, root_objective="Bring one reviewed PR")


def test_successful_resume_clears_stale_pending_action(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")
    run = store.tick("BO-203", authority=_authority(), budget_remaining=0)
    assert run.pending_action is not None

    run = store.tick("BO-203", authority=_authority(), budget_remaining=20)
    assert run.pending_action is None
    assert run.resumable is False


def test_ci_success_must_match_pr_head_sha_and_success_clears_repair_goal(tmp_path):
    from hermes_cli.kanban_ultragoal import KanbanUltragoalStore

    store = KanbanUltragoalStore(tmp_path)
    store.start("BO-203", authority=_authority(), root_objective="Bring one reviewed PR")
    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": ["pytest"]})
    store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": False, "missing": [{"criterionId": "DC-1", "reason": "first fail"}]},
    )
    assert store.load_run("BO-203").current_goal_id is not None
    store.record_worker_done("BO-203", authority=_authority(), evidence={"commandsRun": ["pytest"]})
    run = store.record_verifier_result(
        "BO-203",
        authority=_authority(),
        result={"passed": True, "doneCriteriaEvidence": [{"criterionId": "DC-1", "evidence": "pytest"}]},
    )
    assert run.current_goal_id is None
    store.record_reviewer_result(
        "BO-203",
        authority=_authority(),
        result={"recommendation": "APPROVE", "securityConcerns": [], "logicErrors": []},
    )
    store.record_pr_created(
        "BO-203",
        authority=_authority(),
        pr={"url": "https://github.com/chriskim12/hermes-agent/pull/1", "number": 1, "headSha": "abc"},
    )

    with pytest.raises(ValueError, match="headSha"):
        store.record_ci_result("BO-203", authority=_authority(), ci={"state": "success"})
    with pytest.raises(ValueError, match="headSha"):
        store.record_ci_result("BO-203", authority=_authority(), ci={"state": "success", "headSha": "other"})

    run = store.record_ci_result("BO-203", authority=_authority(), ci={"state": "success", "headSha": "abc"})
    assert run.state == "ci_passed"
    assert run.current_goal_id is None


def test_cli_start_status_and_tick_json(tmp_path, capsys):
    from hermes_cli.kanban_ultragoal import build_parser, kanban_ultragoal_command
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    build_parser(sub)
    authority = json.dumps(_authority())

    args = parser.parse_args([
        "kanban-ultragoal",
        "--workdir",
        str(tmp_path),
        "--json",
        "start",
        "BO-203",
        "--authority-json",
        authority,
        "--root-objective",
        "Bring one reviewed PR",
    ])
    assert kanban_ultragoal_command(args) == 0
    start_out = json.loads(capsys.readouterr().out)
    assert start_out["state"] == "admitted"

    args = parser.parse_args([
        "kanban-ultragoal",
        "--workdir",
        str(tmp_path),
        "--json",
        "tick",
        "BO-203",
        "--authority-json",
        authority,
        "--budget-remaining",
        "0",
    ])
    assert kanban_ultragoal_command(args) == 0
    tick_out = json.loads(capsys.readouterr().out)
    assert tick_out["resumable"] is True
