"""Repo-aware close policy helpers for Linear Done transitions."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

_CHRIS_DONE_STATE_IDS = {
    "11441b27-828e-4dd5-a66f-9236a98d82c9",  # Chris team Done
}

_DAILYCHINGU_REPO_NAME = "dailychingu"
_DAILYCHINGU_ALLOWED_DONE_BRANCHES = {"develop", "main"}


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_base_repo_root(path_value: str | Path) -> Optional[Path]:
    candidate = Path(path_value).expanduser().resolve(strict=False)
    if candidate.is_file():
        candidate = candidate.parent

    common_dir = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if common_dir.returncode != 0:
        return None

    common_path = Path(common_dir.stdout.strip()).resolve(strict=False)
    if common_path.name == ".git":
        return common_path.parent
    return None


def _resolve_current_checkout_root(path_value: str | Path) -> Optional[Path]:
    candidate = Path(path_value).expanduser().resolve(strict=False)
    if candidate.is_file():
        candidate = candidate.parent
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve(strict=False)


def _repo_name(path_value: Path) -> str:
    return path_value.name


def _status_has_relevant_changes(status_output: str) -> bool:
    for line in status_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith('.worktrees/') or '.worktrees/' in stripped:
            continue
        return True
    return False


def _dailychingu_task_done_close_blockers(repo_path: str | Path) -> list[str]:
    base_repo_root = _resolve_base_repo_root(repo_path)
    current_checkout_root = _resolve_current_checkout_root(repo_path)
    if base_repo_root is None or current_checkout_root is None:
        return []

    blockers: list[str] = []
    if current_checkout_root != base_repo_root:
        blockers.append("task_worktree_still_open")
    else:
        branch_result = _run_git(current_checkout_root, "branch", "--show-current")
        current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
        if current_branch not in _DAILYCHINGU_ALLOWED_DONE_BRANCHES:
            blockers.append("task_branch_not_integrated")

    status = _run_git(base_repo_root, "status", "--short")
    if status.returncode == 0 and _status_has_relevant_changes(status.stdout):
        blockers.append("base_checkout_dirty")

    return blockers


def linear_done_transition_requested(command: str) -> bool:
    if not command or "api.linear.app/graphql" not in command or "issueUpdate" not in command:
        return False
    if "stateId" not in command:
        return False
    return any(done_state in command for done_state in _CHRIS_DONE_STATE_IDS)


def linear_done_close_blockers(repo_path: str | Path) -> list[str]:
    repo_root = _resolve_base_repo_root(repo_path)
    if repo_root is None:
        return []

    if _repo_name(repo_root) == _DAILYCHINGU_REPO_NAME:
        return _dailychingu_task_done_close_blockers(repo_path)

    blockers: list[str] = []

    status = _run_git(repo_root, "status", "--short")
    if status.returncode == 0 and status.stdout.strip():
        blockers.append("base_checkout_dirty")

    worktrees = _run_git(repo_root, "worktree", "list", "--porcelain")
    if worktrees.returncode == 0:
        worktree_blocks = [block for block in worktrees.stdout.strip().split("\n\n") if block.strip()]
        if len(worktree_blocks) > 1:
            blockers.append("worktree_residue")

    branches = _run_git(repo_root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    if branches.returncode == 0:
        branch_names = [line.strip() for line in branches.stdout.splitlines() if line.strip()]
        if len(branch_names) > 1:
            blockers.append("branch_residue")

    return blockers


def build_linear_done_block_error(repo_path: str | Path, command: str) -> Optional[str]:
    if not linear_done_transition_requested(command):
        return None

    blockers = linear_done_close_blockers(repo_path)
    if not blockers:
        return None

    return (
        "Linear Done transition is blocked until repo hygiene closes cleanly. "
        f"Blocking residue: {', '.join(blockers)}. "
        "Clean the current task-owned surface and retry the Done transition."
    )
