from __future__ import annotations

import sqlite3

from hermes_cli.kanban_workspace_janitor import (
    CleanupAction,
    apply_artifact_cleanup_actions,
    classify_workspace,
    classify_workspaces,
    discover_artifacts,
    path_size,
    plan_artifact_cleanup_actions,
)


def test_discover_artifacts_counts_allowlisted_without_descending(tmp_path):
    workspace = tmp_path / "t_done"
    nested = workspace / "app" / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / "index.js").write_text("x" * 10)
    ignored = workspace / "app" / "important"
    ignored.mkdir()
    (ignored / "note.txt").write_text("keep")

    artifacts = discover_artifacts(workspace)

    assert len(artifacts) == 1
    assert artifacts[0]["kind"] == "node_modules"
    assert artifacts[0]["path"].endswith("app/node_modules")
    assert artifacts[0]["size_bytes"] >= 10


def test_classify_non_terminal_workspace_as_blocked_active(tmp_path):
    workspace = tmp_path / "t_running"
    (workspace / "node_modules").mkdir(parents=True)

    report = classify_workspace(
        workspace,
        {"id": "t_running", "status": "running", "completed_at": None},
        now=10_000,
        proc_cwds=[],
        pane_cwds=[],
    )

    assert report.state == "blocked-active"
    assert report.gates["terminal_status"] is False


def test_classify_active_cwd_under_workspace_as_blocked_active(tmp_path):
    workspace = tmp_path / "t_done"
    active_dir = workspace / "subdir"
    active_dir.mkdir(parents=True)

    report = classify_workspace(
        workspace,
        {
            "id": "t_done",
            "status": "done",
            "completed_at": 1,
            "result": "preserved",
        },
        now=10_000,
        proc_cwds=[str(active_dir)],
        pane_cwds=[],
    )

    assert report.state == "blocked-active"
    assert report.gates["active_refs"] == [str(active_dir)]


def test_classify_terminal_old_artifact_as_safe_artifact_candidate(tmp_path):
    workspace = tmp_path / "t_done"
    artifact = workspace / "node_modules"
    artifact.mkdir(parents=True)
    (artifact / "package.js").write_text("x" * 20)

    report = classify_workspace(
        workspace,
        {
            "id": "t_done",
            "status": "done",
            "completed_at": 1,
            "result": "summary preserved",
        },
        now=200_000,
        proc_cwds=[],
        pane_cwds=[],
    )

    assert report.state == "safe-artifact-candidate"
    assert report.gates["artifact_ttl_met"] is True
    assert report.artifacts[0]["kind"] == "node_modules"


def test_classify_terminal_without_evidence_requires_approval(tmp_path):
    workspace = tmp_path / "t_done"
    (workspace / "node_modules").mkdir(parents=True)

    report = classify_workspace(
        workspace,
        {"id": "t_done", "status": "done", "completed_at": 1},
        now=200_000,
        proc_cwds=[],
        pane_cwds=[],
    )

    assert report.state == "approval-required"
    assert "lacks preserved" in report.reason


def test_classify_workspaces_loads_task_metadata_from_db(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    workspaces = tmp_path / "workspaces"
    workspace = workspaces / "t_db"
    artifact = workspace / "node_modules"
    artifact.mkdir(parents=True)
    (artifact / "package.js").write_text("x" * 12)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            public_id TEXT,
            title TEXT,
            status TEXT,
            review_phase TEXT,
            completed_at INTEGER,
            result TEXT,
            closeout_evidence TEXT,
            current_run_id INTEGER,
            worker_pid INTEGER,
            assignee TEXT,
            workspace_kind TEXT,
            workspace_path TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_db",
            "BO-999",
            "Fixture task",
            "done",
            "worker_done",
            1,
            "summary preserved",
            None,
            None,
            None,
            None,
            "scratch",
            None,
        ),
    )
    conn.commit()

    monkeypatch.setattr("hermes_cli.kanban_workspace_janitor.process_cwds", lambda: [])
    monkeypatch.setattr("hermes_cli.kanban_workspace_janitor.tmux_cwds", lambda: [])

    reports = classify_workspaces(db_path, workspaces, now=200_000)

    assert len(reports) == 1
    assert reports[0].task["public_id"] == "BO-999"
    assert reports[0].state == "safe-artifact-candidate"
    assert reports[0].size_bytes == path_size(workspace)


def test_plan_artifact_cleanup_actions_only_uses_safe_candidates(tmp_path):
    safe_workspace = tmp_path / "safe"
    safe_artifact = safe_workspace / "node_modules"
    safe_artifact.mkdir(parents=True)
    (safe_artifact / "pkg.js").write_text("x")
    blocked_workspace = tmp_path / "blocked"
    blocked_artifact = blocked_workspace / "node_modules"
    blocked_artifact.mkdir(parents=True)

    safe_report = classify_workspace(
        safe_workspace,
        {"id": "safe", "status": "done", "completed_at": 1, "result": "kept"},
        now=200_000,
        proc_cwds=[],
        pane_cwds=[],
    )
    blocked_report = classify_workspace(
        blocked_workspace,
        {"id": "blocked", "status": "blocked", "completed_at": None},
        now=200_000,
        proc_cwds=[],
        pane_cwds=[],
    )

    actions = plan_artifact_cleanup_actions([blocked_report, safe_report])

    assert len(actions) == 1
    assert actions[0].task_id == "safe"
    assert actions[0].artifact_path == str(safe_artifact)


def test_apply_artifact_cleanup_actions_dry_run_never_deletes(tmp_path):
    workspace = tmp_path / "safe"
    artifact = workspace / "node_modules"
    artifact.mkdir(parents=True)
    (artifact / "pkg.js").write_text("x")
    action = CleanupAction(
        task_id="safe",
        workspace_path=str(workspace),
        artifact_path=str(artifact),
        kind="node_modules",
        size_bytes=1,
        candidate_state="safe-artifact-candidate",
        reason="fixture",
    )

    result = apply_artifact_cleanup_actions([action])

    assert result == [
        {
            "task_id": "safe",
            "workspace_path": str(workspace),
            "artifact_path": str(artifact),
            "kind": "node_modules",
            "size_bytes": 1,
            "candidate_state": "safe-artifact-candidate",
            "reason": "fixture",
            "dry_run": True,
            "deleted": False,
            "guard_errors": [],
        }
    ]
    assert artifact.exists()


def test_apply_artifact_cleanup_actions_apply_requires_exact_guards(tmp_path):
    workspace = tmp_path / "safe"
    artifact = workspace / "node_modules"
    artifact.mkdir(parents=True)
    (artifact / "pkg.js").write_text("x")
    bad = CleanupAction(
        task_id="safe",
        workspace_path=str(workspace),
        artifact_path=str(tmp_path / "outside" / "node_modules"),
        kind="node_modules",
        size_bytes=1,
        candidate_state="safe-artifact-candidate",
        reason="fixture",
    )
    good = CleanupAction(
        task_id="safe",
        workspace_path=str(workspace),
        artifact_path=str(artifact),
        kind="node_modules",
        size_bytes=1,
        candidate_state="safe-artifact-candidate",
        reason="fixture",
    )

    results = apply_artifact_cleanup_actions([bad, good], dry_run=False)

    assert results[0]["deleted"] is False
    assert "artifact_not_under_workspace" in results[0]["guard_errors"]
    assert results[1]["deleted"] is True
    assert not artifact.exists()
