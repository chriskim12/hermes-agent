"""Linear → Kanban shadow bridge helpers.

This module deliberately implements only the CH-409 shadow frontier:
read a Linear issue, map it to a Kanban task, and rely on the Kanban
``idempotency_key`` to avoid duplicate shadow rows.  It does not dispatch
workers, change Linear, or treat Kanban completion as Linear closeout.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from hermes_cli import kanban_db as kb

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LINEAR_SHADOW_IDEMPOTENCY_PREFIX = "linear:"
DEFAULT_LINEAR_SHADOW_TENANT = "brain-os"

_PROJECT_TENANT_MAP = {
    "brain os": "brain-os",
    "dailychingu": "dailychingu",
    "why starve": "whystarve",
    "whystarve": "whystarve",
    "personal": "personal",
    "hermes": "hermes",
}

_LINEAR_IDENTIFIER_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


@dataclass(frozen=True)
class LinearIssueSnapshot:
    """Bounded Linear issue fields required for Kanban shadow creation."""

    identifier: str
    title: str
    url: str
    issue_id: Optional[str] = None
    state: Optional[str] = None
    project: Optional[str] = None
    team: Optional[str] = None
    assignee: Optional[str] = None
    parent_identifier: Optional[str] = None
    updated_at: Optional[str] = None
    description: Optional[str] = None

    @classmethod
    def from_graphql(cls, raw: dict[str, Any]) -> "LinearIssueSnapshot":
        identifier = str(raw.get("identifier") or "").strip()
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not identifier or not title or not url:
            raise ValueError("Linear issue response is missing identifier, title, or url")
        return cls(
            identifier=identifier,
            title=title,
            url=url,
            issue_id=raw.get("id"),
            state=_nested_name(raw.get("state")),
            project=_nested_name(raw.get("project")),
            team=_nested_name(raw.get("team")),
            assignee=_nested_name(raw.get("assignee")),
            parent_identifier=_nested_identifier(raw.get("parent")),
            updated_at=raw.get("updatedAt"),
            description=raw.get("description"),
        )


def _nested_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name else None
    return None


def _nested_identifier(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        identifier = value.get("identifier")
        return str(identifier) if identifier else None
    return None


def tenant_for_linear_project(project_name: Optional[str]) -> str:
    """Map a Linear project name to the CH-408 Kanban tenant namespace.

    CH-408 deliberately forbids guessing a domain from the issue title. If
    Linear does not provide a recognized project, callers must pass an explicit
    tenant override rather than silently writing to the wrong ledger namespace.
    """

    if not project_name:
        raise ValueError("Linear project is required for tenant routing")
    tenant = _PROJECT_TENANT_MAP.get(project_name.strip().lower())
    if not tenant:
        raise ValueError(f"unmapped Linear project for tenant routing: {project_name!r}")
    return tenant


def linear_idempotency_key(identifier: str) -> str:
    ident = identifier.strip().upper()
    if not _LINEAR_IDENTIFIER_RE.match(ident):
        raise ValueError(f"invalid Linear identifier: {identifier!r}")
    return f"{LINEAR_SHADOW_IDEMPOTENCY_PREFIX}{ident}"


def build_shadow_title(issue: LinearIssueSnapshot) -> str:
    return f"{issue.identifier} — {issue.title}"


def build_shadow_body(issue: LinearIssueSnapshot, *, tenant: Optional[str] = None) -> str:
    """Encode source provenance in the task body, not nonexistent task metadata."""

    resolved_tenant = tenant or tenant_for_linear_project(issue.project)
    payload = {
        "source": "linear",
        "identifier": issue.identifier,
        "issue_id": issue.issue_id,
        "url": issue.url,
        "state": issue.state,
        "project": issue.project,
        "team": issue.team,
        "assignee": issue.assignee,
        "parent_identifier": issue.parent_identifier,
        "updated_at": issue.updated_at,
        "shadow_policy": {
            "public_id": issue.identifier,
            "idempotency_key": linear_idempotency_key(issue.identifier),
            "tenant": resolved_tenant,
            "execution": "shadow_only_no_dispatch",
        },
    }
    parts = [
        f"Linear shadow for `{issue.identifier}`.",
        "",
        f"Source: {issue.url}",
        f"State at sync: {issue.state or '-'}",
        f"Project: {issue.project or '-'}",
        f"Parent: {issue.parent_identifier or '-'}",
        "",
        "```json source_payload",
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        "```",
    ]
    if issue.description:
        parts.extend(["", "## Linear description snapshot", issue.description.strip()])
    return "\n".join(parts).strip() + "\n"


def existing_shadow_task_id(conn: Any, identifier: str) -> Optional[str]:
    key = linear_idempotency_key(identifier)
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    return row["id"] if row else None


def shadow_issue_to_kanban(
    conn: Any,
    issue: LinearIssueSnapshot,
    *,
    tenant: Optional[str] = None,
    created_by: str = "linear-shadow-bridge",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create or return an idempotent Kanban shadow task for a Linear issue."""

    key = linear_idempotency_key(issue.identifier)
    resolved_tenant = tenant or tenant_for_linear_project(issue.project)
    existing_id = existing_shadow_task_id(conn, issue.identifier)
    title = build_shadow_title(issue)
    body = build_shadow_body(issue, tenant=resolved_tenant)
    if dry_run:
        return {
            "created": False,
            "dry_run": True,
            "task_id": existing_id,
            "title": title,
            "tenant": resolved_tenant,
            "idempotency_key": key,
            "status": "triage",
        }
    if existing_id:
        task_id = existing_id
        created = False
    else:
        task_id = kb.create_task(
            conn,
            title=title,
            body=body,
            created_by=created_by,
            workspace_kind="scratch",
            tenant=resolved_tenant,
            triage=True,
            idempotency_key=key,
        )
        created = True
    task = kb.get_task(conn, task_id)
    return {
        "created": created,
        "dry_run": False,
        "task_id": task_id,
        "title": task.title if task else title,
        "tenant": task.tenant if task else resolved_tenant,
        "idempotency_key": key,
        "status": task.status if task else "triage",
    }


def fetch_linear_issue(identifier: str, *, api_key: Optional[str] = None) -> LinearIssueSnapshot:
    """Fetch a Linear issue by identifier using LINEAR_API_KEY."""

    ident = identifier.strip().upper()
    if not _LINEAR_IDENTIFIER_RE.match(ident):
        raise ValueError(f"invalid Linear identifier: {identifier!r}")
    token = api_key or os.environ.get("LINEAR_API_KEY")
    if not token:
        raise RuntimeError("LINEAR_API_KEY is required to fetch Linear issues")
    query = """
    query($id:String!){
      issue(id:$id){
        id identifier title url description updatedAt
        state { name }
        project { name }
        team { name }
        assignee { name }
        parent { identifier }
      }
    }
    """
    data = json.dumps({"query": query, "variables": {"id": ident}}).encode()
    req = urllib.request.Request(
        LINEAR_GRAPHQL_URL,
        data=data,
        headers={"Authorization": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            payload = json.loads(res.read().decode())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Linear request failed: {exc}") from exc
    if payload.get("errors"):
        raise RuntimeError(f"Linear returned errors: {payload['errors']}")
    issue = ((payload.get("data") or {}).get("issue"))
    if not issue:
        raise RuntimeError(f"Linear issue not found: {ident}")
    return LinearIssueSnapshot.from_graphql(issue)


def shadow_linear_identifier(
    identifier: str,
    *,
    tenant: Optional[str] = None,
    created_by: str = "linear-shadow-bridge",
    dry_run: bool = False,
) -> dict[str, Any]:
    issue = fetch_linear_issue(identifier)
    with kb.connect() as conn:
        return shadow_issue_to_kanban(
            conn,
            issue,
            tenant=tenant,
            created_by=created_by,
            dry_run=dry_run,
        )


__all__ = [
    "LinearIssueSnapshot",
    "build_shadow_body",
    "build_shadow_title",
    "existing_shadow_task_id",
    "fetch_linear_issue",
    "linear_idempotency_key",
    "shadow_issue_to_kanban",
    "shadow_linear_identifier",
    "tenant_for_linear_project",
]
