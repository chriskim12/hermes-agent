"""Read-only Kanban workspace janitor classifier.

This module intentionally performs no deletion. It classifies Kanban task
workspaces and reproducible artifacts so cleanup can stay report-first and
approval-gated.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ARTIFACT_NAMES = {
    "node_modules",
    ".next",
    ".turbo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
}
TERMINAL_STATUSES = {"done", "archived", "cancelled", "superseded"}


@dataclass(slots=True)
class WorkspaceReport:
    task_id: str
    workspace_path: str
    state: str
    reason: str
    size_bytes: int
    task: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    gates: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workspace_path": self.workspace_path,
            "state": self.state,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "task": self.task,
            "artifacts": self.artifacts,
            "gates": self.gates,
        }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            p = Path(root) / name
            try:
                if not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    return total


def discover_artifacts(workspace: Path, *, min_bytes: int = 0) -> list[dict[str, Any]]:
    """Return allowlisted reproducible artifacts under a workspace."""
    artifacts: list[dict[str, Any]] = []
    if not workspace.exists():
        return artifacts
    for root, dirs, _files in os.walk(workspace, followlinks=False):
        # Mutate dirs so os.walk does not descend into already-counted artifacts.
        kept: list[str] = []
        for dirname in dirs:
            child = Path(root) / dirname
            if dirname in ARTIFACT_NAMES:
                size = path_size(child)
                if size >= min_bytes:
                    artifacts.append({
                        "kind": dirname,
                        "path": str(child),
                        "size_bytes": size,
                    })
            else:
                kept.append(dirname)
        dirs[:] = kept
    artifacts.sort(key=lambda item: int(item.get("size_bytes") or 0), reverse=True)
    return artifacts


def process_cwds() -> list[str]:
    cwds: list[str] = []
    proc = Path("/proc")
    if not proc.exists():
        return cwds
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        cwds.append(str(cwd))
    return cwds


def tmux_cwds() -> list[str]:
    try:
        cp = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_current_path}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def git_state(workspace: Path) -> dict[str, Any]:
    if not (workspace / ".git").exists():
        return {"is_git_worktree": False, "dirty": None, "status_short": None}
    cp = subprocess.run(
        ["git", "-C", str(workspace), "status", "--short"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    status = cp.stdout.strip()
    return {
        "is_git_worktree": True,
        "dirty": bool(status) if cp.returncode == 0 else None,
        "status_short": status,
        "error": cp.stderr.strip() if cp.returncode != 0 else None,
    }


def _load_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id, public_id, title, status, review_phase, completed_at, result,
               closeout_evidence, current_run_id, worker_pid, assignee, workspace_kind,
               workspace_path
        FROM tasks WHERE id = ?
        """,
        (task_id,),
    ).fetchone()
    return dict(row) if row else {"id": task_id, "status": "unknown"}


def classify_workspace(
    workspace: Path,
    task: Mapping[str, Any],
    *,
    now: int | None = None,
    artifact_ttl_seconds: int = 48 * 3600,
    workspace_ttl_seconds: int = 7 * 24 * 3600,
    proc_cwds: Sequence[str] | None = None,
    pane_cwds: Sequence[str] | None = None,
    min_artifact_bytes: int = 0,
) -> WorkspaceReport:
    """Classify one workspace without mutating it."""
    now = int(now if now is not None else __import__("time").time())
    task_id = str(task.get("id") or workspace.name)
    proc_cwds = list(process_cwds() if proc_cwds is None else proc_cwds)
    pane_cwds = list(tmux_cwds() if pane_cwds is None else pane_cwds)
    artifacts = discover_artifacts(workspace, min_bytes=min_artifact_bytes)
    gstate = git_state(workspace)
    size = path_size(workspace)

    active_refs = [p for p in [*proc_cwds, *pane_cwds] if _is_relative_to(Path(p), workspace)]
    active_worker = bool(task.get("current_run_id") or task.get("worker_pid"))
    status = str(task.get("status") or "unknown")
    completed_at = task.get("completed_at") or 0
    age = max(0, now - int(completed_at or 0)) if completed_at else None
    has_evidence = bool(task.get("result") or task.get("closeout_evidence"))

    gates = {
        "terminal_status": status in TERMINAL_STATUSES,
        "age_seconds": age,
        "artifact_ttl_met": age is not None and age >= artifact_ttl_seconds,
        "workspace_ttl_met": age is not None and age >= workspace_ttl_seconds,
        "active_refs": active_refs,
        "active_worker": active_worker,
        "git": gstate,
        "has_evidence": has_evidence,
        "artifact_count": len(artifacts),
    }

    if active_refs or active_worker or status not in TERMINAL_STATUSES:
        return WorkspaceReport(task_id, str(workspace), "blocked-active", "task is active/non-terminal or has active references", size, dict(task), artifacts, gates)
    if not has_evidence:
        return WorkspaceReport(task_id, str(workspace), "approval-required", "terminal task lacks preserved result/closeout evidence", size, dict(task), artifacts, gates)
    if gstate.get("dirty") is True:
        return WorkspaceReport(task_id, str(workspace), "approval-required", "git worktree has dirty/untracked state", size, dict(task), artifacts, gates)
    if artifacts and gates["artifact_ttl_met"]:
        return WorkspaceReport(task_id, str(workspace), "safe-artifact-candidate", "allowlisted reproducible artifacts meet terminal-state TTL", size, dict(task), artifacts, gates)
    if gates["workspace_ttl_met"] and gstate.get("dirty") is False:
        return WorkspaceReport(task_id, str(workspace), "future-workspace-cleanup-candidate", "clean terminal workspace meets full-workspace TTL", size, dict(task), artifacts, gates)
    return WorkspaceReport(task_id, str(workspace), "approval-required", "terminal workspace is too recent or lacks an allowlisted cleanup path", size, dict(task), artifacts, gates)


def classify_workspaces(
    db_path: Path,
    workspaces_root: Path,
    *,
    now: int | None = None,
    min_workspace_bytes: int = 0,
    min_artifact_bytes: int = 0,
) -> list[WorkspaceReport]:
    """Classify every task workspace directory under *workspaces_root*."""
    conn = sqlite3.connect(str(db_path))
    reports: list[WorkspaceReport] = []
    if not workspaces_root.exists():
        return reports
    proc = process_cwds()
    panes = tmux_cwds()
    for workspace in sorted(p for p in workspaces_root.iterdir() if p.is_dir()):
        size = path_size(workspace)
        if size < min_workspace_bytes:
            continue
        task = _load_task(conn, workspace.name)
        report = classify_workspace(
            workspace,
            task,
            now=now,
            proc_cwds=proc,
            pane_cwds=panes,
            min_artifact_bytes=min_artifact_bytes,
        )
        reports.append(report)
    reports.sort(key=lambda report: report.size_bytes, reverse=True)
    return reports
