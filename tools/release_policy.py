"""Repo-specific release-close policy helpers.

Start narrow: DailyChingu currently requires `develop -> main` release truth and
post-release local/remote sync on `main`.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Optional

from tools.repo_workflow_profile import resolve_repo_workflow_profile



def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_repo_root(path_value: str | Path) -> Optional[Path]:
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


def _git_ref_exists(repo_root: Path, ref_name: str) -> bool:
    return _run_git(repo_root, "rev-parse", "--verify", "--quiet", ref_name).returncode == 0


def _command_targets_production_push(command: str, production_branch: str) -> bool:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    if len(tokens) < 4 or tokens[0] != "git" or tokens[1] != "push":
        return False

    push_args = tokens[2:]
    for arg in push_args:
        if arg.startswith("-"):
            continue
        if arg in {production_branch, f"refs/heads/{production_branch}"}:
            return True
        if arg.endswith(f":{production_branch}") or arg.endswith(f":refs/heads/{production_branch}"):
            return True
    return False


def release_close_blockers(repo_path: str | Path) -> list[str]:
    repo_root = _resolve_repo_root(repo_path)
    profile = resolve_repo_workflow_profile(repo_root) if repo_root is not None else None
    if (
        repo_root is None
        or profile is None
        or not profile.integration_branch
        or not profile.production_branch
    ):
        return []

    integration_branch = profile.integration_branch
    production_branch = profile.production_branch
    remote_name = profile.remote_name or "origin"

    blockers: list[str] = []
    if _git_ref_exists(repo_root, integration_branch) and _git_ref_exists(repo_root, production_branch):
        merged = _run_git(
            repo_root,
            "merge-base",
            "--is-ancestor",
            integration_branch,
            production_branch,
        )
        if merged.returncode != 0:
            blockers.append("release_path_missing_develop_to_main")

    remote_ref = f"{remote_name}/{production_branch}"
    if _git_ref_exists(repo_root, remote_ref) and _git_ref_exists(repo_root, production_branch):
        sync = _run_git(
            repo_root,
            "rev-list",
            "--left-right",
            "--count",
            f"{remote_ref}...{production_branch}",
        )
        if sync.returncode == 0 and sync.stdout.strip() != "0\t0":
            blockers.append("local_main_not_fast_forward_synced_to_origin_main")

    return blockers


def build_release_push_block_error(repo_path: str | Path, command: str) -> Optional[str]:
    repo_root = _resolve_repo_root(repo_path)
    profile = resolve_repo_workflow_profile(repo_root) if repo_root is not None else None
    if repo_root is None or profile is None or not profile.production_branch:
        return None
    if not _command_targets_production_push(command, profile.production_branch):
        return None

    blockers = release_close_blockers(repo_root)
    if "release_path_missing_develop_to_main" not in blockers:
        return None

    profile_display_name = profile.display_name or profile.name
    return (
        f"{profile_display_name} production push is blocked: release must go through "
        "`develop -> main` first. Merge/fast-forward `develop` into `main` "
        "before pushing `main`."
    )
