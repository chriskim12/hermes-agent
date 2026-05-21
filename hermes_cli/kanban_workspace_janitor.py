"""Read-only Kanban workspace janitor classifier.

This module intentionally performs no deletion. It classifies Kanban task
workspaces and reproducible artifacts so cleanup can stay report-first and
approval-gated.
"""

from __future__ import annotations

import os
import shutil
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


@dataclass(slots=True)
class CleanupAction:
    """A planned exact-path artifact cleanup action.

    The action is inert until passed to ``apply_artifact_cleanup_actions`` with
    ``dry_run=False``. This keeps BO-127's apply path explicit and testable.
    """

    task_id: str
    workspace_path: str
    artifact_path: str
    kind: str
    size_bytes: int
    candidate_state: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workspace_path": self.workspace_path,
            "artifact_path": self.artifact_path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "candidate_state": self.candidate_state,
            "reason": self.reason,
        }


def plan_artifact_cleanup_actions(
    reports: Iterable[WorkspaceReport],
) -> list[CleanupAction]:
    """Plan exact allowlisted artifact cleanup actions from classifier output."""
    actions: list[CleanupAction] = []
    for report in reports:
        if report.state != "safe-artifact-candidate":
            continue
        workspace = Path(report.workspace_path)
        for artifact in report.artifacts:
            kind = str(artifact.get("kind") or "")
            artifact_path = Path(str(artifact.get("path") or ""))
            if kind not in ARTIFACT_NAMES:
                continue
            if artifact_path.name != kind:
                continue
            if not _is_relative_to(artifact_path, workspace):
                continue
            actions.append(
                CleanupAction(
                    task_id=report.task_id,
                    workspace_path=str(workspace),
                    artifact_path=str(artifact_path),
                    kind=kind,
                    size_bytes=int(artifact.get("size_bytes") or 0),
                    candidate_state=report.state,
                    reason=report.reason,
                )
            )
    actions.sort(key=lambda action: action.size_bytes, reverse=True)
    return actions


def apply_artifact_cleanup_actions(
    actions: Iterable[CleanupAction],
    *,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """Apply or preview exact artifact cleanup actions.

    ``dry_run=True`` is the default and never deletes. ``dry_run=False`` only
    removes an artifact if all final exact-path guards still pass:

    - artifact path exists and is a directory;
    - artifact is not a symlink;
    - artifact path remains under the recorded workspace;
    - basename still matches the allowlisted artifact kind.
    """
    results: list[dict[str, Any]] = []
    for action in actions:
        artifact = Path(action.artifact_path)
        workspace = Path(action.workspace_path)
        guard_errors: list[str] = []
        if action.kind not in ARTIFACT_NAMES:
            guard_errors.append("kind_not_allowlisted")
        if artifact.name != action.kind:
            guard_errors.append("artifact_basename_mismatch")
        if not _is_relative_to(artifact, workspace):
            guard_errors.append("artifact_not_under_workspace")
        if artifact.is_symlink():
            guard_errors.append("artifact_is_symlink")
        if not artifact.exists():
            guard_errors.append("artifact_missing")
        if artifact.exists() and not artifact.is_dir():
            guard_errors.append("artifact_not_directory")

        result = action.to_dict()
        result.update({"dry_run": dry_run, "deleted": False, "guard_errors": guard_errors})
        if guard_errors or dry_run:
            results.append(result)
            continue
        before = path_size(artifact)
        shutil.rmtree(artifact)
        result.update({
            "deleted": not artifact.exists(),
            "reclaimed_bytes": before,
        })
        results.append(result)
    return results


@dataclass(slots=True)
class WorkspaceCleanupAction:
    """A planned exact-path full-workspace cleanup action."""

    task_id: str
    workspace_path: str
    size_bytes: int
    candidate_state: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workspace_path": self.workspace_path,
            "size_bytes": self.size_bytes,
            "candidate_state": self.candidate_state,
            "reason": self.reason,
        }


def plan_workspace_cleanup_actions(
    reports: Iterable[WorkspaceReport],
) -> list[WorkspaceCleanupAction]:
    """Plan full-workspace cleanup actions from strict classifier output."""
    actions: list[WorkspaceCleanupAction] = []
    for report in reports:
        if report.state != "future-workspace-cleanup-candidate":
            continue
        git = report.gates.get("git") or {}
        if git.get("is_git_worktree") is not True or git.get("dirty") is not False:
            continue
        if report.gates.get("active_refs") or report.gates.get("active_worker"):
            continue
        if not report.gates.get("has_evidence"):
            continue
        actions.append(
            WorkspaceCleanupAction(
                task_id=report.task_id,
                workspace_path=report.workspace_path,
                size_bytes=report.size_bytes,
                candidate_state=report.state,
                reason=report.reason,
            )
        )
    actions.sort(key=lambda action: action.size_bytes, reverse=True)
    return actions


def apply_workspace_cleanup_actions(
    actions: Iterable[WorkspaceCleanupAction],
    *,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """Apply or preview exact full-workspace cleanup actions.

    Full workspace cleanup is deliberately stricter than artifact cleanup. The
    final guard rejects symlinks, non-directories, non-workspace-looking paths,
    and any git worktree with a dirty status at apply time.
    """
    results: list[dict[str, Any]] = []
    for action in actions:
        workspace = Path(action.workspace_path)
        guard_errors: list[str] = []
        if workspace.is_symlink():
            guard_errors.append("workspace_is_symlink")
        if not workspace.exists():
            guard_errors.append("workspace_missing")
        if workspace.exists() and not workspace.is_dir():
            guard_errors.append("workspace_not_directory")
        if workspace.name != action.task_id:
            guard_errors.append("workspace_basename_not_task_id")
        gstate = git_state(workspace) if workspace.exists() else {"dirty": None}
        if gstate.get("is_git_worktree") is not True:
            guard_errors.append("workspace_not_git_worktree")
        if gstate.get("dirty") is not False:
            guard_errors.append("workspace_git_not_clean")

        result = action.to_dict()
        result.update({"dry_run": dry_run, "deleted": False, "guard_errors": guard_errors})
        if guard_errors or dry_run:
            results.append(result)
            continue
        before = path_size(workspace)
        shutil.rmtree(workspace)
        result.update({
            "deleted": not workspace.exists(),
            "reclaimed_bytes": before,
        })
        results.append(result)
    return results
