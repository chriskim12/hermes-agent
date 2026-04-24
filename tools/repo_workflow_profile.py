"""Repo workflow profile helpers.

v1 stays intentionally narrow:
- DailyChingu is the only concrete workflow profile.
- Push/release authority semantics remain fail-closed for repos without a profile.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REVIEW_VERDICT_ONLY = "review verdict only"
PUSH_AUTHORITY = "push authority"
RELEASE_AUTHORITY = "release authority"


@dataclass(frozen=True)
class RepoWorkflowProfile:
    name: str
    display_name: str | None = None
    done_allowed_branches: tuple[str, ...] = ()
    integration_branch: str | None = None
    production_branch: str | None = None
    remote_name: str | None = None
    push_approval_token: str | None = None
    push_workflow_summary: str = ""
    release_approval_token: str | None = None
    release_workflow_summary: str = ""


@dataclass(frozen=True)
class HandoffAuthorityResolution:
    decision: str
    supported: bool
    approval_token: str | None = None
    workflow_summary: str = ""
    reason: str = ""


_DAILYCHINGU_PROFILE = RepoWorkflowProfile(
    name="dailychingu",
    display_name="DailyChingu",
    done_allowed_branches=("develop", "main"),
    integration_branch="develop",
    production_branch="main",
    remote_name="origin",
    push_approval_token="push 승인",
    push_workflow_summary="DailyChingu push authority integrates the task result into develop (merge/ff included), verifies that integration truth, then cleans up task-owned residue.",
    release_approval_token="release 승인",
    release_workflow_summary="DailyChingu release authority follows the develop -> main release path.",
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


def resolve_repo_workflow_profile(path_value: str | Path) -> RepoWorkflowProfile | None:
    repo_root = _resolve_repo_root(path_value)
    if repo_root is None:
        return None
    if repo_root.name == _DAILYCHINGU_PROFILE.name:
        return _DAILYCHINGU_PROFILE
    return None


def resolve_workflow_handoff_authority(
    path_value: str | Path,
    decision: str,
) -> HandoffAuthorityResolution:
    normalized_decision = decision.strip().lower()
    if normalized_decision == REVIEW_VERDICT_ONLY:
        return HandoffAuthorityResolution(
            decision=normalized_decision,
            supported=True,
            approval_token=REVIEW_VERDICT_ONLY,
            workflow_summary="Review may proceed once a human supplies the verdict.",
        )

    profile = resolve_repo_workflow_profile(path_value)
    if normalized_decision == PUSH_AUTHORITY:
        if profile and profile.push_approval_token:
            return HandoffAuthorityResolution(
                decision=normalized_decision,
                supported=True,
                approval_token=profile.push_approval_token,
                workflow_summary=profile.push_workflow_summary,
            )
        return HandoffAuthorityResolution(
            decision=normalized_decision,
            supported=False,
            reason="Fail-closed: this repo has no workflow profile for push authority semantics.",
        )

    if normalized_decision == RELEASE_AUTHORITY:
        if profile and profile.release_approval_token:
            return HandoffAuthorityResolution(
                decision=normalized_decision,
                supported=True,
                approval_token=profile.release_approval_token,
                workflow_summary=profile.release_workflow_summary,
            )
        return HandoffAuthorityResolution(
            decision=normalized_decision,
            supported=False,
            reason="Fail-closed: this repo has no workflow profile for release authority semantics.",
        )

    return HandoffAuthorityResolution(
        decision=normalized_decision,
        supported=False,
        reason=(
            "Pending human decision must be one of: "
            "review verdict only, push authority, release authority."
        ),
    )
