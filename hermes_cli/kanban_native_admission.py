"""Kanban-native work creation/admission helpers.

This module owns the first Linear-free admission frontier for the Hermes Work
Ledger. It writes native Kanban tasks with a separate public id (``PP-NNN``)
while preserving fail-closed admission rules and dry-run/no-dispatch safety.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb

DEFAULT_NATIVE_NAMESPACE = "BO"
NATIVE_IDEMPOTENCY_PREFIX = "kanban:"
_NAMESPACE_RE = re.compile(r"^[A-Z]{2}$")
_PUBLIC_ID_RE = re.compile(r"^[A-Z]{2}-\d{3,}$")


@dataclass(frozen=True)
class NativeAdmissionRequest:
    title: str
    tenant: str
    repo_full_name: str
    profile: str
    closeout_policy: str
    executor: str
    namespace: str = DEFAULT_NATIVE_NAMESPACE
    body: Optional[str] = None
    created_by: str = "kanban-native-admission"
    workspace_kind: str = "worktree"
    workspace_path: Optional[str] = None
    priority: int = 0
    parents: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    base_branch: Optional[str] = None
    worktree_branch: Optional[str] = None
    approval_boundary: str = "human_approval_required"
    public_id: Optional[str] = None
    idempotency_key: Optional[str] = None


def normalize_namespace(namespace: str) -> str:
    ns = (namespace or "").strip().upper()
    if not _NAMESPACE_RE.match(ns):
        raise ValueError("namespace must be exactly two uppercase letters")
    if ns == "CH":
        raise ValueError("CH is reserved for Linear legacy references and cannot be used for native work")
    if ns == "HL":
        raise ValueError("HL is not a Kanban-native namespace or legacy alias; use BO for Brain OS work")
    return ns


def normalize_public_id(public_id: str) -> str:
    pid = (public_id or "").strip().upper()
    if not _PUBLIC_ID_RE.match(pid):
        raise ValueError("public_id must use PP-NNN format")
    normalize_namespace(pid.split("-", 1)[0])
    return pid


def _missing_required(req: NativeAdmissionRequest) -> list[str]:
    checks = {
        "title": req.title,
        "tenant": req.tenant,
        "repo_full_name": req.repo_full_name,
        "profile": req.profile,
        "closeout_policy": req.closeout_policy,
        "executor": req.executor,
    }
    return [name for name, value in checks.items() if not str(value or "").strip()]


def native_idempotency_key(public_id: str) -> str:
    return f"{NATIVE_IDEMPOTENCY_PREFIX}{normalize_public_id(public_id)}"


def _clean_optional(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _clean_required(value: Any) -> str:
    return str(value or "").strip()


def namespace_admission_blocker(conn: Any, namespace: str) -> Optional[dict[str, str]]:
    """Return a fail-closed blocker when a native namespace is not approved.

    Namespace shape validation is necessary but not sufficient: CH-426 policy
    requires a live registry entry with ``status='active'`` before native work
    can allocate IDs or enter candidate selection. Reserved legacy, retired,
    unknown, or duplicate registry state blocks admission instead of falling
    back to a default domain.
    """
    ns = normalize_namespace(namespace)
    rows = [item for item in kb.list_namespaces(conn) if item.prefix == ns]
    if not rows:
        return {
            "field": "namespace",
            "reason": "native_namespace_unregistered",
            "message": f"namespace {ns} is not registered for Kanban-native work",
        }
    statuses = {row.status for row in rows}
    if len(rows) != 1 or len(statuses) != 1:
        return {
            "field": "namespace",
            "reason": "native_namespace_ambiguous",
            "message": f"namespace {ns} has ambiguous registry state",
        }
    status = rows[0].status
    if status != "active":
        return {
            "field": "namespace",
            "reason": f"native_namespace_{status}",
            "message": f"namespace {ns} is {status}, not active",
        }
    return None


def next_public_id(conn: Any, namespace: str = DEFAULT_NATIVE_NAMESPACE) -> str:
    ns = normalize_namespace(namespace)
    rows = conn.execute(
        "SELECT public_id FROM tasks WHERE public_id LIKE ?",
        (f"{ns}-%",),
    ).fetchall()
    max_seen = 0
    for row in rows:
        value = str(row["public_id"] or "")
        if not value.startswith(f"{ns}-"):
            continue
        suffix = value.split("-", 1)[1]
        if suffix.isdigit():
            max_seen = max(max_seen, int(suffix))
    return f"{ns}-{max_seen + 1:03d}"


def existing_native_task_id(conn: Any, public_id: str) -> Optional[str]:
    pid = normalize_public_id(public_id)
    row = conn.execute(
        "SELECT id FROM tasks WHERE public_id = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1",
        (pid,),
    ).fetchone()
    return row["id"] if row else None


def _as_bool_text(value: Any) -> str:
    return "true" if value is True else "false" if value is False else str(value)


def _get_nested(payload: dict[str, Any], path: tuple[str, ...], default: Any = None) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _format_metadata_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return _as_bool_text(value)
    if value is None:
        return "null"
    return str(value)


def _iter_suggested_breakdown(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return child/parent breakdown suggestions from common seed payload shapes.

    Suggestions are display-only during admission.  This renderer deliberately
    does not translate them into ready tasks or dependency gates.
    """
    candidates = (
        payload.get("suggested_breakdown"),
        payload.get("suggested_children"),
        payload.get("children"),
        _get_nested(payload, ("seed_contract", "suggested_breakdown")),
        _get_nested(payload, ("seed_contract", "suggested_children")),
        _get_nested(payload, ("seed_contract", "children")),
    )
    suggestions: list[dict[str, Any]] = []
    for candidate in candidates:
        if not candidate:
            continue
        if isinstance(candidate, dict):
            iterable = candidate.get("items") or candidate.get("suggestions") or candidate.get("children") or []
        else:
            iterable = candidate
        if not isinstance(iterable, list):
            continue
        for item in iterable:
            if isinstance(item, dict):
                suggestions.append(item)
            else:
                suggestions.append({"title": str(item)})
    return suggestions


def render_seed_admission_card_body(payload: dict[str, Any], *, card_kind: str = "admission") -> str:
    """Render a Seed Contract/admission payload as a Kanban-native card body.

    The output is intentionally verbose about authority boundaries because this
    body may be read by humans and later agents.  Routing/executor fields are
    preserved as admission metadata only; the rendered text must not imply that
    workers may be claimed or dispatched before Chris approves execution.
    """
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    kind = (card_kind or "admission").strip().lower().replace("_", "-")
    if kind not in {"admission", "decision-gate"}:
        raise ValueError("card_kind must be 'admission' or 'decision-gate'")

    public_id = str(payload.get("public_id") or "UNKNOWN").strip()
    idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
    tenant = str(payload.get("tenant") or "").strip() or None
    source = str(payload.get("source") or "kanban_native").strip()
    admission = dict(payload.get("admission") or {})
    routing = dict(payload.get("routing") or {})
    repo_intent = dict(payload.get("repo_intent") or {})
    closeout = dict(payload.get("closeout") or {})
    execution_hints = dict(routing.get("execution_hints") or payload.get("execution_hints") or {})
    parents = payload.get("parents") if isinstance(payload.get("parents"), list) else []
    link_intent = payload.get("link_intent") if isinstance(payload.get("link_intent"), list) else []
    suggestions = _iter_suggested_breakdown(payload)

    normalized_payload = dict(payload)
    normalized_payload["source"] = source
    normalized_payload["admission"] = {
        **admission,
        "approval_boundary": "human_approval_required",
        "executor_dispatch": "forbidden_during_admission",
        "linear_required": False,
    }
    normalized_payload["routing"] = {
        **routing,
        "status": "proposed_only",
        "approval_boundary": "human_approval_required",
    }
    normalized_payload["closeout"] = {
        **closeout,
        "policy": "admission_only_no_execution",
        "worker_done_review_ready_closed_are_distinct": True,
    }

    lines = [
        f"# {public_id} Kanban-native {kind} card",
        "",
        "STATUS: BLOCKED / ADMISSION-ONLY. Chris must approve execution before any executor, profile, worker, or routing hint may dispatch.",
        "",
        "## Authority boundary (hard requirements)",
        "- source=kanban_native",
        "- approval_boundary=human_approval_required",
        "- executor_dispatch=forbidden_during_admission",
        "- linear_required=false",
        "- closeout.policy=admission_only_no_execution",
        "- closeout.admission_only_no_execution=true",
        "",
        "## Visible evidence / comment text",
        "This card is an admission or decision-gate record only. Any executor, profile, skills, repo, parent, child, or routing metadata below is admission metadata and future intent; it does not authorize dispatch, worker claims, run rows, ready tasks, repo mutation, PR activity, deployment, gateway restart, secret changes, billing/customer-visible actions, or Linear work.",
        "",
        "## Identity",
    ]
    for label, value in (
        ("public_id", public_id),
        ("idempotency_key", idempotency_key),
        ("tenant", tenant),
        ("source", "kanban_native"),
    ):
        lines.append(f"- {label}={_format_metadata_value(value)}")

    lines.extend(["", "## Proposed routing (not executable)"])
    for label, value in (
        ("routing.status", "proposed_only"),
        ("routing.verdict", routing.get("verdict")),
        ("routing.reason", routing.get("reason")),
        ("routing.approval_boundary", "human_approval_required"),
        ("execution_hints", execution_hints),
    ):
        lines.append(f"- {label}={_format_metadata_value(value)}")
    lines.append("- dispatch_authority=none_during_admission")

    lines.extend(["", "## Repository intent (descriptive only)"])
    if repo_intent:
        for key in sorted(repo_intent):
            lines.append(f"- repo_intent.{key}={_format_metadata_value(repo_intent.get(key))}")
    else:
        lines.append("- repo_intent=null")
    lines.append("- repo intent may inform future approved worktree setup, but this admission card does not create or mutate any repository state.")

    lines.extend(["", "## Parent/link intent (suggestions and hierarchy only unless explicitly approved)"])
    if parents:
        for index, parent in enumerate(parents, 1):
            if isinstance(parent, dict):
                relation = parent.get("relation_type") or "hierarchy"
                executable_gate = parent.get("executable_gate") if "executable_gate" in parent else False
                rendered = ", ".join(f"{key}={_format_metadata_value(parent.get(key))}" for key in sorted(parent))
                lines.append(f"- parent suggestion {index}: {rendered}, relation_type={relation}, executable_gate={_as_bool_text(bool(executable_gate))}")
            else:
                lines.append(f"- parent suggestion {index}: {_format_metadata_value(parent)}, relation_type=hierarchy, executable_gate=false")
    else:
        lines.append("- no parent suggestions supplied")
    if link_intent:
        for index, link in enumerate(link_intent, 1):
            lines.append(f"- link suggestion {index}: {_format_metadata_value(link)}; suggestion_only=true")
    lines.append("- Parent/child breakdowns shown here are suggestions only; they are not ready executable tasks and do not gate readiness unless Chris later approves a dependency edge.")

    lines.extend(["", "## Suggested child breakdown (not ready tasks)"])
    if suggestions:
        for index, suggestion in enumerate(suggestions, 1):
            title = suggestion.get("title") or suggestion.get("public_id") or f"suggestion {index}"
            rendered = ", ".join(f"{key}={_format_metadata_value(suggestion.get(key))}" for key in sorted(suggestion))
            lines.append(f"- suggestion {index}: {title} — {rendered}; executable_ready=false; suggestion_only=true")
    else:
        lines.append("- no child breakdown suggestions supplied")

    lines.extend([
        "",
        "## Done / Review Ready / Closed semantics",
        "- Done: admission renderer/card body exists and preserves authority evidence; this does not mean implementation execution is complete.",
        "- Review Ready: Chris has enough admission evidence to approve, reject, or revise execution; no executor dispatch is implied.",
        "- Closed: the admission or decision gate is resolved or superseded; closing admission is distinct from executable work being finished.",
        "- Executable Ready: forbidden from this card body until a separate human approval creates or promotes executable work.",
        "",
        "```json source_payload",
        json.dumps(normalized_payload, indent=2, sort_keys=True, ensure_ascii=False),
        "```",
    ])
    return "\n".join(lines).strip() + "\n"


def build_native_body(req: NativeAdmissionRequest, public_id: str, idempotency_key: str) -> str:
    payload = {
        "source": "kanban_native",
        "public_id": public_id,
        "idempotency_key": idempotency_key,
        "tenant": _clean_required(req.tenant),
        "legacy_refs": [],
        "admission": {
            "mode": "native_dry_run_or_triage_admission",
            "linear_required": False,
            "executor_dispatch": "forbidden_during_admission",
            "approval_boundary": req.approval_boundary,
        },
        "repo_intent": {
            "repo_full_name": _clean_optional(req.repo_full_name),
            "base_branch": _clean_optional(req.base_branch),
            "worktree_branch": _clean_optional(req.worktree_branch),
            "workspace_kind": req.workspace_kind,
            "workspace_path": req.workspace_path,
        },
        "execution_hints": {
            "executor": _clean_required(req.executor),
            "profile": _clean_required(req.profile),
            "skills": list(req.skills),
        },
        "routing": {
            "verdict": _clean_required(req.executor),
            "reason": "native admission captures requested executor; dispatch remains forbidden until live preflight",
            "approval_boundary": req.approval_boundary,
        },
        "closeout": {
            "policy": _clean_required(req.closeout_policy),
            "worker_done_review_ready_closed_are_distinct": True,
        },
        "parents": list(req.parents),
    }
    parts = [
        f"Kanban-native work item `{public_id}`.",
        "",
        "Linear source issue is not required. This admission record is safe-by-default: "
        "it creates a triage ledger row and does not dispatch an executor.",
        "",
        "```json source_payload",
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        "```",
    ]
    if req.body:
        parts.extend(["", "## Opening context", req.body.strip()])
    return "\n".join(parts).strip() + "\n"


def build_native_admission_payload(
    conn: Any,
    req: NativeAdmissionRequest,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    missing = _missing_required(req)
    requested_namespace = normalize_namespace(req.namespace)
    public_id = normalize_public_id(req.public_id) if req.public_id else next_public_id(conn, requested_namespace)
    namespace = public_id.split("-", 1)[0]
    namespace_blocker = namespace_admission_blocker(conn, namespace)
    idempotency_key = req.idempotency_key or native_idempotency_key(public_id)
    existing_id = existing_native_task_id(conn, public_id)
    body = build_native_body(req, public_id, idempotency_key)
    blockers = list(missing)
    if namespace_blocker:
        blockers.append(namespace_blocker["field"])
    status = "blocked" if blockers else "would_create"
    reason = "native_admission_contract_ready"
    if missing:
        reason = "native_admission_missing_required_fields"
    elif namespace_blocker:
        reason = namespace_blocker["reason"]
    return {
        "status": status,
        "reason": reason,
        "dry_run": dry_run,
        "missing": blockers,
        "namespace_policy": namespace_blocker or {"status": "active", "namespace": namespace},
        "public_id": public_id,
        "task_id": existing_id,
        "created": False,
        "task": {
            "title": f"{public_id} — {_clean_required(req.title)}",
            "body": body,
            "tenant": _clean_required(req.tenant) or None,
            "public_id": public_id,
            "idempotency_key": idempotency_key,
            "workspace_kind": req.workspace_kind,
            "workspace_path": req.workspace_path,
            "status": "triage",
            "assignee": None,
            "priority": req.priority,
            "parents": list(req.parents),
            "skills": list(req.skills),
        },
        "repo_intent": {
            "repo_full_name": _clean_optional(req.repo_full_name) or None,
            "base_branch": _clean_optional(req.base_branch),
            "worktree_branch": _clean_optional(req.worktree_branch),
        },
        "execution_hints": {
            "executor": _clean_required(req.executor) or None,
            "profile": _clean_required(req.profile) or None,
            "skills": list(req.skills),
        },
        "authority": {
            "review_phase": None,
            "routing_verdict": {
                "verdict": "Hermes direct" if _clean_required(req.executor) == "hermes-direct" else "blocked",
                "reason": "native admission records routing intent only; executor dispatch is forbidden during admission",
                "boundary": req.approval_boundary,
            },
            "admission_snapshot": {
                "source": "kanban_native",
                "linear_required": False,
                "public_id": public_id,
                "idempotency_key": idempotency_key,
                "tenant": _clean_required(req.tenant) or None,
                "repo_full_name": _clean_optional(req.repo_full_name) or None,
                "profile": _clean_required(req.profile) or None,
                "executor": _clean_required(req.executor) or None,
                "closeout_policy": _clean_required(req.closeout_policy) or None,
                "executor_dispatch": "forbidden_during_admission",
            },
            "closeout_evidence": {
                "policy": _clean_required(req.closeout_policy) or None,
                "worker_done_review_ready_closed_are_distinct": True,
                "evidence_status": "not_started",
            },
        },
        "side_effects": {
            "kanban_task_written": False,
            "executor_spawned": False,
            "linear_required": False,
            "linear_mutated": False,
        },
    }


def create_native_work(
    conn: Any,
    req: NativeAdmissionRequest,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    payload = build_native_admission_payload(conn, req, dry_run=dry_run)
    if payload["missing"]:
        return payload
    if dry_run:
        return payload
    if payload["task_id"]:
        payload.update(
            {
                "status": "reused",
                "reason": "native_admission_existing_public_id",
                "dry_run": False,
                "created": False,
            }
        )
        return payload
    task = payload["task"]
    task_id = kb.create_task(
        conn,
        title=task["title"],
        body=task["body"],
        assignee=None,
        created_by=req.created_by,
        workspace_kind=task["workspace_kind"],
        workspace_path=task["workspace_path"],
        tenant=task["tenant"],
        priority=task["priority"],
        parents=tuple(task["parents"]),
        triage=True,
        idempotency_key=task["idempotency_key"],
        skills=task["skills"] or None,
        public_id=task["public_id"],
        review_phase=payload["authority"]["review_phase"],
        routing_verdict=payload["authority"]["routing_verdict"],
        admission_snapshot=payload["authority"]["admission_snapshot"],
        closeout_evidence=payload["authority"]["closeout_evidence"],
    )
    payload.update(
        {
            "status": "created",
            "reason": "native_admission_created_triage_task",
            "dry_run": False,
            "created": True,
            "task_id": task_id,
        }
    )
    payload["side_effects"]["kanban_task_written"] = True
    return payload
