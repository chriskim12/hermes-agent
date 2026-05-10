"""Kanban-native closeout verifier tests."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_closeout as closeout
from hermes_cli import kanban_db as kb
from hermes_cli.kanban import run_slash


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _review_ready_evidence(repo: Path, **overrides):
    evidence = {
        "summary": "implementation completed with regression coverage",
        "pr": {
            "number": 421,
            "url": "https://github.com/NousResearch/hermes-agent/pull/421",
            "state": "OPEN",
            "is_draft": False,
            "head_sha": _head(repo),
            "live": True,
        },
        "checks": [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        "cleanup": {"proof": "worktree cleanup verified", "worktree_clean": True},
        "evidence": {"changed_files": ["hermes_cli/kanban_closeout.py"], "tests_run": ["targeted"]},
    }
    evidence.update(overrides)
    return evidence


def test_worker_done_transition_does_not_final_close(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="native governed work",
            closeout_evidence={"evidence_status": "not_started"},
        )
        result = closeout.transition_task_closeout(
            conn,
            task_id,
            "worker_done",
            {"summary": "worker finished; reviewer still required"},
        )
        task = kb.get_task(conn, task_id)

    assert result["status"] == "transitioned"
    assert task.review_phase == "worker_done"
    assert task.status == "ready"
    assert task.review_phase != "closed"
    assert task.closeout_evidence["verification"]["linear_done_mutated"] is False


def test_complete_task_on_governed_task_sets_worker_done_not_closed(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="native governed work",
            closeout_evidence={"evidence_status": "not_started"},
        )
        assert kb.complete_task(conn, task_id, result="worker result")
        task = kb.get_task(conn, task_id)

    assert task.status == "done"
    assert task.review_phase == "worker_done"
    assert task.review_phase != "closed"


def test_review_ready_requires_live_pr_checks_evidence_and_cleanup(kanban_home, git_repo):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="review me", workspace_kind="dir", workspace_path=str(git_repo))
        closeout.transition_task_closeout(conn, task_id, "worker_done", {"summary": "done"})
        result = closeout.transition_task_closeout(
            conn,
            task_id,
            "review_ready",
            _review_ready_evidence(git_repo),
            repo_path=git_repo,
        )
        task = kb.get_task(conn, task_id)

    assert result["status"] == "transitioned"
    assert task.review_phase == "review_ready"
    assert task.status == "ready"
    assert task.closeout_evidence["verification"]["allowed"] is True
    assert task.closeout_evidence["verification"]["gateway_restarted_or_reloaded"] is False
    assert task.closeout_evidence["verification"]["pr_merged"] is False


def test_review_ready_requires_worker_done_phase(git_repo):
    result = closeout.verify_closeout_transition(
        "review_ready",
        _review_ready_evidence(git_repo),
        current_phase=None,
        repo_path=git_repo,
    )

    assert result.allowed is False
    assert "review_ready_requires_worker_done" in result.blockers


@pytest.mark.parametrize(
    "mutate,blocker",
    [
        (lambda repo, ev: ev["pr"].update({"head_sha": "0" * 40}), "stale_pr"),
        (
            lambda repo, ev: ev.update(
                {"checks": [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]}
            ),
            "failed_checks",
        ),
        (lambda repo, ev: ev.update({"cleanup": {"worktree_clean": True}}), "missing_cleanup_proof"),
        (
            lambda repo, ev: ev.update(
                {"pr_candidates": [ev.pop("pr"), {"number": 422, "live": True}]}
            ),
            "ambiguous_pr_evidence",
        ),
    ],
)
def test_review_ready_fails_closed_for_stale_failed_missing_or_ambiguous_evidence(git_repo, mutate, blocker):
    evidence = _review_ready_evidence(git_repo)
    mutate(git_repo, evidence)

    result = closeout.verify_closeout_transition(
        "review_ready",
        evidence,
        current_phase="worker_done",
        repo_path=git_repo,
    )

    assert result.allowed is False
    assert blocker in result.blockers
    assert result.evidence["verification"]["allowed"] is False


def test_review_ready_fails_closed_for_dirty_worktree(git_repo):
    (git_repo / "DIRTY.txt").write_text("dirty")
    evidence = _review_ready_evidence(git_repo)

    result = closeout.verify_closeout_transition(
        "review_ready",
        evidence,
        current_phase="worker_done",
        repo_path=git_repo,
    )

    assert result.allowed is False
    assert "dirty_worktree" in result.blockers


def test_closed_requires_review_ready_and_explicit_approval(kanban_home, git_repo):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="close me", workspace_kind="dir", workspace_path=str(git_repo))
        closeout.transition_task_closeout(conn, task_id, "worker_done", {"summary": "done"})

        blocked = closeout.transition_task_closeout(
            conn,
            task_id,
            "closed",
            _review_ready_evidence(git_repo),
            repo_path=git_repo,
        )
        assert blocked["status"] == "blocked"
        assert "closed_requires_review_ready" in blocked["blockers"]
        assert "missing_close_approval" in blocked["blockers"]

        closeout.transition_task_closeout(
            conn,
            task_id,
            "review_ready",
            _review_ready_evidence(git_repo),
            repo_path=git_repo,
        )
        approved = _review_ready_evidence(git_repo, approval={"decision": "approved", "approved_by": "reviewer"})
        result = closeout.transition_task_closeout(conn, task_id, "closed", approved, repo_path=git_repo)
        task = kb.get_task(conn, task_id)

    assert result["status"] == "transitioned"
    assert task.review_phase == "closed"
    assert task.status == "done"
    assert task.closeout_evidence["approval"]["approved_by"] == "reviewer"


def test_closed_allows_documented_no_pr_exception_policy(kanban_home, git_repo):
    evidence = {
        "summary": "documentation-only cleanup completed",
        "cleanup": {"proof": "no worktree residue", "worktree_clean": True},
        "no_pr_exception": {
            "policy": "docs-only-no-pr-closeout",
            "reason": "operator documented no repository diff requiring PR",
        },
    }
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="no pr close", workspace_kind="dir", workspace_path=str(git_repo))
        closeout.transition_task_closeout(conn, task_id, "worker_done", {"summary": "done"})
        result = closeout.transition_task_closeout(conn, task_id, "closed", evidence, repo_path=git_repo)
        task = kb.get_task(conn, task_id)

    assert result["status"] == "transitioned"
    assert task.review_phase == "closed"
    assert task.status == "done"
    assert task.closeout_evidence["no_pr_exception"]["policy"] == "docs-only-no-pr-closeout"


def test_closeout_cli_check_only_blocks_without_writing(kanban_home, git_repo):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="cli closeout", workspace_kind="dir", workspace_path=str(git_repo))
        kb.set_task_authority(conn, task_id, review_phase="worker_done")

    out = run_slash(
        "closeout "
        f"{task_id} review_ready --repo {git_repo} --check-only --json --evidence "
        + shlex.quote(json.dumps({"summary": "missing pr"}))
    )
    payload = json.loads(out)

    assert payload["status"] == "blocked"
    assert "missing_live_pr" in payload["blockers"]
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).review_phase == "worker_done"
