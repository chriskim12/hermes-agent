"""Repo-aware close policy helpers for Linear Done transitions."""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from tools.repo_workflow_profile import (
    PUSH_AUTHORITY,
    RELEASE_AUTHORITY,
    REVIEW_VERDICT_ONLY,
    resolve_repo_workflow_profile,
    resolve_workflow_handoff_authority,
)

_CHRIS_DONE_STATE_IDS = {
    "11441b27-828e-4dd5-a66f-9236a98d82c9",  # Chris team Done
}
_CHRIS_IN_REVIEW_STATE_IDS = {
    "bd49fae3-66b0-4fae-bc61-89501e03e0ba",  # Chris team In Review
}

_ALLOWED_HANDOFF_DECISIONS = {
    REVIEW_VERDICT_ONLY,
    PUSH_AUTHORITY,
    RELEASE_AUTHORITY,
}
_HANDOFF_FIELD_LABELS = {
    "handoff_changed": "HANDOFF_CHANGED",
    "handoff_verified": "HANDOFF_VERIFIED",
    "handoff_risks": "HANDOFF_RISKS",
    "handoff_decision": "HANDOFF_DECISION",
}
_REQUIRED_HANDOFF_FIELDS = tuple(_HANDOFF_FIELD_LABELS.keys())
_HANDOFF_QUERY = """query($id:String!){ issue(id:$id){ description comments(first:10){nodes{body}} } }"""


class LinearHandoffLookupError(RuntimeError):
    """Raised when live Linear handoff lookup cannot be completed safely."""



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


def _extract_linear_description(command: str) -> str:
    normalized = command.replace("\\n", "\n")
    prefixes = ('description: \\"', 'description: "')
    suffixes = ('\\" })', '" })', '\\" }) {', '" }) {')

    for prefix in prefixes:
        if prefix not in normalized:
            continue
        tail = normalized.split(prefix, 1)[1]
        for suffix in suffixes:
            if suffix in tail:
                return tail.split(suffix, 1)[0].strip()
        return tail.strip()
    return ""


def _extract_handoff_fields_from_text(text: str) -> dict[str, str]:
    if not text:
        return {}

    fields: dict[str, str] = {}
    for key, label in _HANDOFF_FIELD_LABELS.items():
        match = re.search(rf"{label}:\s*(.+)", text, re.IGNORECASE)
        if match:
            fields[key] = match.group(1).strip()
    return fields


def _extract_handoff_fields(command: str) -> dict[str, str]:
    return _extract_handoff_fields_from_text(_extract_linear_description(command))


def _extract_linear_issue_id(command: str) -> str:
    if not command:
        return ""

    normalized = command.replace('\\"', '"').replace("\\'", "'")
    patterns = (
        r"issueUpdate\s*\(\s*id\s*:\s*[\"']([^\"']+)[\"']",
        r"[\"']id[\"']\s*:\s*[\"'](CH-\d+|[0-9a-fA-F-]{20,})[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            return match.group(1).strip()
    return ""


def _fetch_linear_handoff_texts(issue_id: str) -> list[str]:
    token = os.environ.get("LINEAR_API_KEY")
    if not token:
        raise LinearHandoffLookupError("LINEAR_API_KEY is not available for live Linear handoff lookup.")

    payload = json.dumps({"query": _HANDOFF_QUERY, "variables": {"id": issue_id}}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise LinearHandoffLookupError("live Linear handoff lookup failed.") from exc

    if data.get("errors"):
        raise LinearHandoffLookupError("live Linear handoff lookup returned GraphQL errors.")

    issue = data.get("data", {}).get("issue")
    if not issue:
        raise LinearHandoffLookupError("target Linear issue was not found during handoff lookup.")

    texts = []
    description = issue.get("description")
    if isinstance(description, str) and description.strip():
        texts.append(description)
    comments = issue.get("comments", {}).get("nodes", [])
    for comment in comments:
        body = comment.get("body") if isinstance(comment, dict) else None
        if isinstance(body, str) and body.strip():
            texts.append(body)
    return texts


def _find_valid_handoff_fields(
    repo_path: str | Path,
    candidates: list[dict[str, str]],
) -> tuple[dict[str, str] | None, list[str], str | None]:
    last_blockers: list[str] = list(_REQUIRED_HANDOFF_FIELDS)
    last_detail: str | None = None
    for fields in candidates:
        blockers = [key for key in _REQUIRED_HANDOFF_FIELDS if not fields.get(key, "").strip()]
        if blockers:
            last_blockers = blockers
            last_detail = None
            continue

        decision = fields["handoff_decision"].strip().lower()
        if decision not in _ALLOWED_HANDOFF_DECISIONS:
            last_blockers = ["handoff_decision"]
            last_detail = (
                "Pending human decision must be one of: "
                "review verdict only, push authority, release authority."
            )
            continue

        authority_resolution = resolve_workflow_handoff_authority(repo_path, decision)
        if authority_resolution.supported:
            return fields, [], None
        last_blockers = ["handoff_decision"]
        last_detail = authority_resolution.reason

    return None, last_blockers, last_detail


def _linear_in_review_handoff_blockers(
    repo_path: str | Path,
    command: str,
    fetch_handoff_texts: Callable[[str], list[str]] | None = None,
) -> tuple[list[str], str | None]:
    if fetch_handoff_texts is None:
        fetch_handoff_texts = _fetch_linear_handoff_texts
    command_fields = _extract_handoff_fields(command)
    command_candidates = [command_fields] if command_fields else []

    issue_id = _extract_linear_issue_id(command)
    lookup_error: str | None = None
    live_candidates: list[dict[str, str]] = []
    if issue_id:
        try:
            live_candidates = [
                fields
                for fields in (_extract_handoff_fields_from_text(text) for text in fetch_handoff_texts(issue_id))
                if fields
            ]
        except LinearHandoffLookupError as exc:
            lookup_error = str(exc)
    else:
        lookup_error = "target Linear issue id could not be extracted for live handoff lookup."

    # Linear issue content is the SSOT whenever it yields a handoff candidate. The
    # command-string handoff remains a backwards-compatible fallback for older
    # scripts/tests that have not recorded a live handoff yet.
    candidates = live_candidates if live_candidates else command_candidates
    _, blockers, detail = _find_valid_handoff_fields(repo_path, candidates)
    if not blockers:
        return [], None

    if lookup_error and not command_candidates:
        return list(_REQUIRED_HANDOFF_FIELDS), lookup_error
    if lookup_error and command_candidates:
        detail = f"{detail + ' ' if detail else ''}{lookup_error}"
    return blockers, detail


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

    profile = resolve_repo_workflow_profile(base_repo_root)
    allowed_done_branches = set(profile.done_allowed_branches) if profile else set()
    blockers: list[str] = []
    if current_checkout_root != base_repo_root:
        blockers.append("task_worktree_still_open")
    else:
        branch_result = _run_git(current_checkout_root, "branch", "--show-current")
        current_branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
        if current_branch not in allowed_done_branches:
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


def linear_in_review_transition_requested(command: str) -> bool:
    if not command or "api.linear.app/graphql" not in command or "issueUpdate" not in command:
        return False
    if "stateId" not in command:
        return False
    return any(in_review_state in command for in_review_state in _CHRIS_IN_REVIEW_STATE_IDS)


def linear_done_close_blockers(repo_path: str | Path) -> list[str]:
    repo_root = _resolve_base_repo_root(repo_path)
    if repo_root is None:
        return []

    profile = resolve_repo_workflow_profile(repo_root)
    if profile and _repo_name(repo_root) == profile.name:
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


def build_linear_in_review_block_error(repo_path: str | Path, command: str) -> Optional[str]:
    if not linear_in_review_transition_requested(command):
        return None

    blockers, detail = _linear_in_review_handoff_blockers(repo_path, command)
    if not blockers:
        return None

    error = (
        "Linear In Review handoff is blocked until a valid HANDOFF_* block is present "
        "in the Linear issue description or recent comments. "
        f"Missing/invalid fields: {', '.join(blockers)}."
    )
    if detail:
        error = f"{error} {detail}"
    return error
