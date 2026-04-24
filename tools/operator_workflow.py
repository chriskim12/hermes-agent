"""Executable owner-authority workflows for repo-specific task close paths.

This module intentionally starts narrow: DailyChingu is the only repo with a
materialized push-authority workflow.  Repos without a workflow profile fail
closed instead of inheriting DailyChingu semantics.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from tools.repo_workflow_profile import (
    PUSH_AUTHORITY,
    RELEASE_AUTHORITY,
    REVIEW_VERDICT_ONLY,
    RepoWorkflowProfile,
    resolve_repo_workflow_profile,
)

INSPECTION_ONLY = "inspection only"
HOLD_OR_FIX = "hold/fix"
UNKNOWN_AUTHORITY = "unknown"

AuthorityDecision = Literal[
    "review verdict only",
    "push authority",
    "release authority",
    "inspection only",
    "hold/fix",
    "unknown",
]


@dataclass(frozen=True)
class AuthorityPhraseResolution:
    phrase: str
    decision: AuthorityDecision
    executable: bool
    reason: str = ""


@dataclass(frozen=True)
class PushWorkflowRequest:
    repo_path: str | Path
    task_worktree: str | Path
    task_branch: str
    linear_issue_id: str | None
    owner_phrase: str
    evidence_callback: Callable[[str], None] | None = None


@dataclass
class PushWorkflowResult:
    success: bool
    blockers: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    release_executed: bool = False
    integration_commit: str | None = None

    @property
    def status(self) -> str:
        return "completed" if self.success else "blocked"


_PUSH_AUTHORITY_PHRASES = {
    "push 승인",
    "push도 진행해",
    "push까지 진행해",
    "develop에 반영해",
    "develop으로 올려",
    "task close까지 진행해",
    "반영하고 cleanup까지 해",
    "반영하고 정리해",
}
_REVIEW_ONLY_PHRASES = {"승인"}
_INSPECTION_PHRASES = {"확인해봐", "push 가능한지 봐"}
_AMBIGUOUS_PHRASES = {"좋아", "진행해", "마무리해", "올려"}
_RELEASE_AUTHORITY_PHRASES = {"release 승인", "release까지 진행해", "main으로 올려"}


def normalize_owner_authority_phrase(phrase: str) -> AuthorityPhraseResolution:
    """Normalize owner text into a strict internal authority class.

    The resolver is deliberately conservative: only high-confidence DailyChingu
    task-close language maps to ``PUSH_AUTHORITY``.  Vague approval/progress
    words stay non-executable so they cannot silently degrade into ``git push``.
    """
    normalized = " ".join((phrase or "").strip().lower().split())
    if not normalized:
        return AuthorityPhraseResolution(phrase=phrase, decision=UNKNOWN_AUTHORITY, executable=False, reason="empty phrase")
    if normalized in _PUSH_AUTHORITY_PHRASES:
        return AuthorityPhraseResolution(phrase=phrase, decision=PUSH_AUTHORITY, executable=True)
    if normalized in _RELEASE_AUTHORITY_PHRASES:
        return AuthorityPhraseResolution(phrase=phrase, decision=RELEASE_AUTHORITY, executable=False, reason="release authority is handled by a separate workflow")
    if normalized in _REVIEW_ONLY_PHRASES:
        return AuthorityPhraseResolution(phrase=phrase, decision=REVIEW_VERDICT_ONLY, executable=False, reason="review verdict only")
    if normalized in _INSPECTION_PHRASES:
        return AuthorityPhraseResolution(phrase=phrase, decision=INSPECTION_ONLY, executable=False, reason="inspection only")
    if normalized in _AMBIGUOUS_PHRASES:
        return AuthorityPhraseResolution(phrase=phrase, decision=UNKNOWN_AUTHORITY, executable=False, reason="ambiguous owner phrase")
    if "release" in normalized or "main" in normalized:
        return AuthorityPhraseResolution(phrase=phrase, decision=RELEASE_AUTHORITY, executable=False, reason="release authority is separate")
    return AuthorityPhraseResolution(phrase=phrase, decision=UNKNOWN_AUTHORITY, executable=False, reason="no strict authority match")


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _resolve_base_repo_root(path_value: str | Path) -> Optional[Path]:
    candidate = Path(path_value).expanduser().resolve(strict=False)
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    common_path = Path(result.stdout.strip()).resolve(strict=False)
    if common_path.name == ".git":
        return common_path.parent
    return None


def _git_output(repo: Path, *args: str) -> str:
    result = _run_git(repo, *args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _status_clean(repo: Path) -> bool:
    status = _run_git(repo, "status", "--short").stdout
    for line in status.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(".worktrees/") or ".worktrees/" in stripped:
            continue
        return False
    return True


def _evidence(result: PushWorkflowResult, request: PushWorkflowRequest, line: str) -> None:
    result.evidence.append(line)
    if request.evidence_callback:
        request.evidence_callback(line)


def _block(result: PushWorkflowResult, *blockers: str) -> PushWorkflowResult:
    result.success = False
    result.blockers.extend(blocker for blocker in blockers if blocker)
    return result


def _ensure_profile(profile: RepoWorkflowProfile | None) -> bool:
    return bool(profile and profile.name == "dailychingu" and profile.integration_branch and profile.remote_name)


def execute_push_authority_workflow(request: PushWorkflowRequest) -> PushWorkflowResult:
    """Execute DailyChingu PUSH_AUTHORITY through develop integration and cleanup.

    Positive path:
    1. resolve live card / phrase / repo profile,
    2. verify task lane is clean and mergeable,
    3. integrate task branch into ``develop``,
    4. verify develop contains the task result,
    5. remove task-owned worktree and local branch,
    6. record close evidence,
    7. stop before release.
    """
    result = PushWorkflowResult(success=False)
    phrase = normalize_owner_authority_phrase(request.owner_phrase)
    if phrase.decision != PUSH_AUTHORITY or not phrase.executable:
        return _block(result, f"authority_not_push:{phrase.decision}")
    if not request.linear_issue_id:
        return _block(result, "no_live_card")

    base_repo = _resolve_base_repo_root(request.repo_path)
    task_worktree = Path(request.task_worktree).expanduser().resolve(strict=False)
    if base_repo is None:
        return _block(result, "repo_not_git")
    profile = resolve_repo_workflow_profile(base_repo)
    if not _ensure_profile(profile):
        return _block(result, "repo_profile_missing_or_not_dailychingu")
    assert profile is not None

    if _resolve_base_repo_root(task_worktree) != base_repo:
        return _block(result, "task_worktree_not_in_repo")
    if not _status_clean(task_worktree):
        return _block(result, "dirty_task_worktree")

    current_task_branch = _git_output(task_worktree, "branch", "--show-current")
    if current_task_branch != request.task_branch:
        return _block(result, "task_branch_mismatch")

    integration_branch = profile.integration_branch or "develop"
    task_commit = _git_output(task_worktree, "rev-parse", "--verify", request.task_branch)
    _evidence(result, request, f"review accepted / push authority: {request.linear_issue_id}")
    _evidence(result, request, f"task branch: {request.task_branch} @ {task_commit[:12]}")

    # Guard before checkout: a dirty base checkout should block without changing
    # the operator's current branch or attempting integration.
    if not _status_clean(base_repo):
        return _block(result, "dirty_integration_checkout")
    checkout = _run_git(base_repo, "checkout", integration_branch)
    if checkout.returncode != 0:
        return _block(result, "integration_branch_checkout_failed")
    if not _status_clean(base_repo):
        return _block(result, "dirty_integration_checkout")

    merge = _run_git(base_repo, "merge", "--ff-only", request.task_branch)
    if merge.returncode != 0:
        return _block(result, "unintegratable_task_branch")

    contains = _run_git(base_repo, "merge-base", "--is-ancestor", task_commit, integration_branch)
    if contains.returncode != 0:
        return _block(result, "develop_does_not_contain_task_result")
    result.integration_commit = _git_output(base_repo, "rev-parse", integration_branch)
    _evidence(result, request, f"develop integration truth: {result.integration_commit[:12]}")

    remove = _run_git(base_repo, "worktree", "remove", str(task_worktree))
    if remove.returncode != 0:
        return _block(result, "cleanup_worktree_remove_failed")
    delete = _run_git(base_repo, "branch", "-d", request.task_branch)
    if delete.returncode != 0:
        return _block(result, "cleanup_branch_delete_failed")
    _evidence(result, request, "cleanup: task worktree removed and local task branch deleted")
    _evidence(result, request, "release: not executed; release authority remains separate")

    result.success = True
    result.release_executed = False
    return result
