"""Linear card → /goal execution contract generator.

This module is intentionally deterministic and read-only.  It converts a live
Linear issue payload into a bounded `/goal` prompt, but it does not register a
new goal, start executors, write work_state locks, or mutate Linear.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol


SUPPORTED_GOAL_MODES = {
    "single-card",
    "parent-auto-pilot",
    "shaping",
    "cleanup",
    "verification",
}

_MODE_POLICIES = {
    "single-card": "Execute exactly the target Linear issue; do not start siblings or parent continuation work.",
    "parent-auto-pilot": "Use the parent as queue scope; execute children one at a time and stop on blocker, approval gate, or parent completion.",
    "shaping": "Discovery/product shaping only; ask one narrowing question at a time before implementation mutation.",
    "cleanup": "Close repo/runtime residue for the named issue only; avoid broad refactors and new product scope.",
    "verification": "Verify existing implementation/evidence; do not patch unless verification exposes a bounded defect.",
}

_SECRET_PATTERNS = [
    re.compile(r"(?i)bearer\s+[^\s`'\"]+"),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*=\s*[^\s`'\"]+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


class LinearGoalClient(Protocol):
    def fetch_goal_issue(self, identifier: str) -> Mapping[str, Any] | None:
        """Return a live Linear issue payload without mutating Linear."""


@dataclass(frozen=True)
class GoalContractError(ValueError):
    reason: str
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


class EnvLinearGoalClient:
    """Read-only Linear GraphQL client for goal contract generation."""

    _ISSUE_FIELDS = """
        identifier
        title
        url
        description
        priority
        state { name type }
        parent { identifier title state { name type } }
        children(first: 50) { nodes { identifier title url state { name type } } }
        comments(first: 10) { nodes { body createdAt } }
        team { key name }
    """
    _QUERY = f"""
    query GoalContractIssue($id: String!) {{
      issue(id: $id) {{
        {_ISSUE_FIELDS}
      }}
    }}
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.getenv("LINEAR_API_KEY")

    def fetch_goal_issue(self, identifier: str) -> Mapping[str, Any] | None:
        if not self.api_key:
            return {"status": "unavailable", "reason": "LINEAR_API_KEY_missing", "identifier": identifier}
        body = json.dumps({"query": self._QUERY, "variables": {"id": identifier}}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"status": "unavailable", "reason": f"linear_http_{exc.code}", "identifier": identifier}
        except Exception as exc:
            return {"status": "unavailable", "reason": f"linear_query_{type(exc).__name__}", "identifier": identifier}
        if payload.get("errors"):
            return {"status": "unavailable", "reason": "linear_graphql_error", "errors": payload.get("errors"), "identifier": identifier}
        issue = (payload.get("data") or {}).get("issue")
        if not issue:
            return {"status": "missing", "reason": "linear_issue_not_found", "identifier": identifier}
        issue["status"] = "ok"
        return issue


def _redact(text: Any) -> str:
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def _state_label(issue: Mapping[str, Any]) -> str:
    state = issue.get("state") if isinstance(issue.get("state"), Mapping) else {}
    return f"{state.get('name', 'unknown')}/{state.get('type', 'unknown')}"


def _require_live_issue(issue: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if issue is None:
        raise GoalContractError("linear_lookup_missing")
    if not isinstance(issue, Mapping):
        raise GoalContractError("invalid_payload_type")
    if issue.get("status") not in (None, "ok"):
        raise GoalContractError(str(issue.get("reason") or issue.get("status") or "linear_unavailable"))
    if not issue.get("identifier"):
        raise GoalContractError("missing_identifier")
    if not issue.get("title"):
        raise GoalContractError("missing_title")
    if not isinstance(issue.get("state"), Mapping) or not issue["state"].get("name") or not issue["state"].get("type"):
        raise GoalContractError("missing_state")
    return issue


def _parent_line(issue: Mapping[str, Any]) -> str:
    parent = issue.get("parent")
    if not isinstance(parent, Mapping) or not parent.get("identifier"):
        return "Parent: none"
    state = parent.get("state") if isinstance(parent.get("state"), Mapping) else {}
    return (
        f"Parent: {parent.get('identifier')} — {_redact(parent.get('title'))} "
        f"[{state.get('name', 'unknown')}/{state.get('type', 'unknown')}]"
    )


def _children_lines(issue: Mapping[str, Any]) -> list[str]:
    children = issue.get("children")
    nodes = children.get("nodes") if isinstance(children, Mapping) else []
    if not isinstance(nodes, list) or not nodes:
        return ["- none"]
    lines = []
    for child in nodes[:20]:
        if not isinstance(child, Mapping):
            continue
        lines.append(
            f"- {child.get('identifier', 'unknown')} — {_redact(child.get('title'))} [{_state_label(child)}]"
        )
    return lines or ["- none"]


def _comment_lines(issue: Mapping[str, Any]) -> list[str]:
    comments = issue.get("comments")
    nodes = comments.get("nodes") if isinstance(comments, Mapping) else []
    if not isinstance(nodes, list) or not nodes:
        return ["- none"]
    lines = []
    for comment in nodes[:5]:
        if not isinstance(comment, Mapping):
            continue
        body = _redact(comment.get("body"))[:500].replace("\n", " ")
        lines.append(f"- {comment.get('createdAt', 'unknown')}: {body}")
    return lines or ["- none"]


def generate_goal_contract(issue: Mapping[str, Any], *, mode: str = "single-card") -> dict[str, Any]:
    """Generate a structured `/goal` prompt from one already-live Linear issue payload."""

    if mode not in SUPPORTED_GOAL_MODES:
        raise GoalContractError("unsupported_mode", mode)
    live_issue = _require_live_issue(issue)
    identifier = str(live_issue.get("identifier"))
    title = _redact(live_issue.get("title"))
    description = _redact(live_issue.get("description"))
    if description:
        description = description[:2500]
    else:
        description = "[no description provided — treat this as a blocker unless mode explicitly allows shaping]"

    parent_line = _parent_line(live_issue)
    children_lines = "\n".join(_children_lines(live_issue))
    comment_lines = "\n".join(_comment_lines(live_issue))
    policy = _MODE_POLICIES[mode]
    url = _redact(live_issue.get("url"))

    body = f"""/goal Execute Linear {identifier}: {title}

Linear is the SSOT. Re-query live Linear before any mutation and block if the card, parent, comments, or state contradict this contract.
Do not use stale chat memory or stored /goal_seed as canonical truth; comments are context only.

Target issue: {identifier} — {title}
URL: {url or 'unknown'}
Current state: {_state_label(live_issue)}
{parent_line}
Mode: {mode}
Mode policy: {policy}

Live preflight:
- Read the target Linear issue, parent, children/order, state, description, and latest relevant comments.
- Verify the issue is still actionable for this mode before editing files or starting executors.
- Confirm repo/worktree/branch/ref status and record kickoff evidence in Linear for implementation modes.
- If live lookup, state, parent/child relation, or admission is missing/ambiguous, stop fail-closed and record the blocker.

Scope:
- Use the live Linear card below as the task source of truth.
- Preserve the stated mode boundary and complete only the selected execution unit.
- Produce verification evidence before closeout.

Non-goals:
- Do not execute sibling cards unless mode is parent-auto-pilot and the controller admits the next child explicitly.
- Do not broaden into product shaping, production rollout, provider/billing/customer changes, or gateway lifecycle work unless the live card explicitly requires it and approvals are recorded.
- Do not treat a clean process exit or generated text as Done evidence.

Forbidden side effects:
- No secrets in output; redact tokens, keys, passwords, connection strings, and bearer credentials as [REDACTED].
- No gateway restart/lifecycle action without explicit approval.
- No Linear Done transition without tests, evidence, repo hygiene, and closeout comment.
- No executor spawning outside the selected contract.

Linear card snapshot:
{description}

Children snapshot:
{children_lines}

Recent comments snapshot:
{comment_lines}

Verification:
- Run focused tests/checks relevant to the card.
- Prove negative side effects where the contract requires safety boundaries.
- If implementation changes code, run syntax/diff checks and a static secret scan over the diff.

Closeout rules:
- Post Linear evidence with commit/ref, tests, review result if required, live proof, and explicit non-actions.
- Clean task-owned worktree/branch residue before Done when repo policy requires it.
- Move Linear to Done only after evidence and state verification.

Stop conditions:
- Stop on approval gate, contradictory live Linear state, ambiguous work_state/session correlation, failing safety check, or exhausted turn budget.
- For single-card/shaping/cleanup/verification modes, stop after the selected issue is verified or blocked; do not continue to the next CH-173 child.
"""

    return {
        "status": "ok",
        "mode": mode,
        "target": {
            "identifier": identifier,
            "title": title,
            "url": url,
            "state": dict(live_issue.get("state") or {}),
        },
        "prompt": body.strip(),
    }


def build_goal_contract_from_linear(
    identifier: str,
    *,
    mode: str = "single-card",
    linear_client: LinearGoalClient | None = None,
) -> dict[str, Any]:
    """Re-query live Linear and generate a bounded `/goal` contract."""

    issue_id = str(identifier or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", issue_id):
        raise GoalContractError("invalid_identifier", identifier)
    client = linear_client or EnvLinearGoalClient()
    payload = client.fetch_goal_issue(issue_id)
    live_issue = _require_live_issue(payload)
    if str(live_issue.get("identifier") or "").upper() != issue_id:
        raise GoalContractError("identifier_mismatch", f"requested={issue_id} fetched={live_issue.get('identifier')}")
    return generate_goal_contract(live_issue, mode=mode)
