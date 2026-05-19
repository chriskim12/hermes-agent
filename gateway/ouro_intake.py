"""Ouroboros-style /ouro-intake admission controller.

The command intentionally stops at Seed Contract -> Kanban admission. It does
not start workers, mutate repos, open PRs, restart the gateway, or grant any
execution authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass
from typing import Any

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_native_admission import next_public_id, normalize_namespace

_ALLOWED_PROJECTS = {
    "bo": "BO",
    "brain": "BO",
    "brain-os": "BO",
    "dc": "DC",
    "dailychingu": "DC",
    "ws": "WS",
    "whystarve": "WS",
    "rs": "RS",
    "risu": "RS",
}

_KEY_ALIASES = {
    "goal": "goal",
    "project": "project",
    "namespace": "project",
    "tenant": "tenant",
    "context": "context",
    "non-goals": "non_goals",
    "non_goals": "non_goals",
    "acceptance": "acceptance_criteria",
    "acceptance-criteria": "acceptance_criteria",
    "acceptance_criteria": "acceptance_criteria",
    "verify": "verification_requirements",
    "verification": "verification_requirements",
    "risks": "risks",
    "questions": "open_questions",
    "routing": "initial_routing",
    "scope": "scope",
}

_VALID_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:")
_SENSITIVE_RE = re.compile(
    r"\b(prod|production|billing|paddle|payment|secret|env|customer|email|refund|deploy|restart)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OuroIntakeResult:
    action: str
    message: str
    mutated: bool = False
    dispatched: bool = False
    task_id: str | None = None
    public_id: str | None = None
    error: str | None = None


def _help_message() -> str:
    return (
        "/ouro-intake creates an Ouroboros-style Seed Contract and admits it to Kanban.\n\n"
        "Usage:\n"
        "  /ouro-intake goal:<text> project:<bo|dc|ws|rs> [tenant:<name>] [context:<text>]\n"
        "  /ouro-intake <plain goal text>\n\n"
        "Boundary: admission only. executor_dispatch=forbidden_during_admission; "
        "no worker, repo mutation, PR, gateway restart, secret/env change, or live rollout is approved."
    )


def _split_tokens(raw_args: str) -> list[str]:
    try:
        return shlex.split(raw_args)
    except ValueError:
        # Fall back to whitespace splitting so malformed quotes fail closed as
        # ordinary text rather than crashing the gateway handler.
        return raw_args.split()


def _parse_args(raw_args: str) -> dict[str, Any]:
    values: dict[str, Any] = {"project": "bo", "tenant": "intake"}
    free_goal: list[str] = []
    for token in _split_tokens(raw_args):
        if _VALID_KEY_RE.match(token):
            key, value = token.split(":", 1)
            canonical = _KEY_ALIASES.get(key.strip().lower())
            if canonical:
                values[canonical] = value.strip()
                continue
        free_goal.append(token)
    if not str(values.get("goal") or "").strip() and free_goal:
        values["goal"] = " ".join(free_goal).strip()
    return values


def _namespace_for(project: str) -> str:
    key = (project or "bo").strip().lower()
    ns = _ALLOWED_PROJECTS.get(key, project.strip().upper())
    return normalize_namespace(ns)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _as_list(value: Any, *, default: list[str] | None = None) -> list[str]:
    if value is None or str(value).strip() == "":
        return list(default or [])
    text = str(value).strip()
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    return parts or [text]


def _ambiguity_flags(values: dict[str, Any]) -> list[str]:
    """Return Seed-review blockers that should keep intake decision-gated."""

    goal = str(values.get("goal") or "").strip()
    flags: list[str] = []
    if len(goal.split()) < 4:
        flags.append("goal_too_short_for_execution")
    if not str(values.get("context") or "").strip():
        flags.append("missing_context")
    if not str(values.get("acceptance_criteria") or "").strip():
        flags.append("missing_acceptance_criteria")
    if _SENSITIVE_RE.search(goal):
        flags.append("sensitive_side_effect_domain")
    return flags


def _seed_review(values: dict[str, Any]) -> dict[str, Any]:
    flags = _ambiguity_flags(values)
    questions: list[str] = []
    if "goal_too_short_for_execution" in flags:
        questions.append("What concrete outcome should the work produce?")
    if "missing_context" in flags:
        questions.append("What existing repo/runtime/product context should be considered before implementation?")
    if "missing_acceptance_criteria" in flags:
        questions.append("What observable proof should make this accepted as Done?")
    if "sensitive_side_effect_domain" in flags:
        questions.append("Which side effects are explicitly allowed, and which must remain approval-gated?")
    mode = "decision_gate_only" if flags else "seed_ready_for_admission"
    return {
        "mode": mode,
        "ambiguity_flags": flags,
        "blocking_questions": questions,
        "dispatch_allowed": False,
    }


def _build_seed_contract(values: dict[str, Any], *, public_id: str, actor: str | None) -> dict[str, Any]:
    goal = str(values.get("goal") or "").strip()
    project = str(values.get("project") or "bo").strip().lower()
    namespace = _namespace_for(project)
    tenant = str(values.get("tenant") or "intake").strip() or "intake"
    context = str(values.get("context") or "").strip()
    non_goals = _as_list(values.get("non_goals"), default=[
        "executor dispatch",
        "repo mutation",
        "PR creation/merge",
        "gateway restart/reload",
        "secret/env mutation",
        "production or customer-visible change",
    ])
    acceptance = _as_list(values.get("acceptance_criteria"), default=[
        "Seed Contract is admitted to Kanban with public_id and readback evidence",
        "open questions are visible before implementation begins",
        "Chris explicitly approves any execution/routing transition after admission",
    ])
    verification = _as_list(values.get("verification_requirements"), default=[
        "Kanban readback confirms authority/admission metadata",
        "task remains unassigned and non-dispatchable",
        "task_runs is empty for the admission card",
    ])
    risks = _as_list(values.get("risks"), default=[
        "ambiguous goal may require a follow-up Socratic interview before execution",
        "auto-decompose/dispatcher must not treat intake as runnable work",
    ])
    open_questions = _as_list(values.get("open_questions"), default=[
        "What exact scope should be included/excluded?",
        "What proof would make this Done?",
        "Which repo/runtime, if any, becomes relevant after approval?",
    ])
    review = _seed_review(values)
    if review["blocking_questions"]:
        open_questions = list(dict.fromkeys([*review["blocking_questions"], *open_questions]))
    suggested_breakdown = [
        {
            "title": "Clarify Seed Contract until ambiguity flags are resolved",
            "link_type": "hierarchy",
            "dispatch_allowed": False,
        }
    ] if review["mode"] == "decision_gate_only" else []
    seed = {
        "source": "ouro_intake",
        "public_id": public_id,
        "created_by": actor or "unknown",
        "created_at": int(time.time()),
        "goal": goal,
        "non_goals": non_goals,
        "context": context,
        "seed_review": review,
        "authority": {
            "after_admission": f"Kanban {public_id}",
            "seed_contract_is_source_material_only": True,
        },
        "scope": str(values.get("scope") or "decision-gate/admission").strip(),
        "side_effect_boundary": {
            "executor_dispatch": "forbidden_during_admission",
            "repo_mutation": False,
            "pull_request": False,
            "gateway_restart_or_reload": False,
            "secret_or_env_mutation": False,
            "prod_or_customer_visible_change": False,
        },
        "acceptance_criteria": acceptance,
        "verification_requirements": verification,
        "risks": risks,
        "open_questions": open_questions,
        "suggested_breakdown": suggested_breakdown,
        "initial_routing": {
            "status": "proposed_only",
            "verdict": str(values.get("initial_routing") or "blocked").strip() or "blocked",
            "reason": "Ouroboros-style intake records routing intent only; execution requires Chris approval plus Kanban promotion.",
        },
        "approval_required_for": [
            "worker dispatch",
            "repo/worktree mutation",
            "PR creation/update/merge",
            "gateway restart/reload",
            "secret/env/config mutation",
            "live rollout or customer-visible change",
        ],
        "kanban": {
            "namespace": namespace,
            "tenant": tenant,
            "idempotency_key": f"ouro-intake:{hashlib.sha256(goal.encode('utf-8')).hexdigest()[:16]}",
        },
    }
    seed["seed_contract_sha256"] = hashlib.sha256(_canonical_json(seed).encode("utf-8")).hexdigest()
    return seed


def _body(seed: dict[str, Any]) -> str:
    return (
        f"Ouroboros-style Seed Contract admission `{seed['public_id']}`.\n\n"
        "This card is the Kanban authority after admission. The Seed Contract below is source material only; "
        "it is not execution approval.\n\n"
        "Admission boundary: `executor_dispatch=forbidden_during_admission`. No worker, repo mutation, PR, "
        "gateway restart/reload, secret/env change, prod/customer-visible change, or live rollout is approved by this intake.\n\n"
        "```json seed_contract\n"
        + json.dumps(seed, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n```\n"
    )


def _readback(conn: Any, task_id: str) -> dict[str, Any]:
    task = kb.get_task(conn, task_id)
    if task is None:
        raise RuntimeError(f"created task did not read back: {task_id}")
    runs = conn.execute("SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ?", (task_id,)).fetchone()["n"]
    return {
        "task_id": task.id,
        "public_id": task.public_id,
        "status": task.status,
        "assignee": task.assignee,
        "claim_lock": task.claim_lock,
        "worker_pid": task.worker_pid,
        "task_runs": int(runs or 0),
        "admission_executor_dispatch": (task.admission_snapshot or {}).get("executor_dispatch"),
        "routing_status": (task.routing_verdict or {}).get("status"),
    }


def handle_ouro_intake_command(raw_args: str = "", *, actor: str | None = None) -> OuroIntakeResult:
    """Create an admission-only Kanban Seed Contract from explicit command text."""

    raw_args = (raw_args or "").strip()
    if not raw_args or raw_args in {"help", "--help", "-h"}:
        return OuroIntakeResult(action="help", message=_help_message())

    values = _parse_args(raw_args)
    if not str(values.get("goal") or "").strip():
        return OuroIntakeResult(
            action="error",
            error="missing_goal",
            message="Missing goal. Use `/ouro-intake goal:<text> project:<bo|dc|ws|rs>`; no action was taken.",
        )

    try:
        namespace = _namespace_for(str(values.get("project") or "bo"))
    except ValueError as exc:
        return OuroIntakeResult(
            action="error",
            error="invalid_project",
            message=f"Invalid project/namespace: {exc}. No action was taken.",
        )

    with kb.connect() as conn:
        public_id = next_public_id(conn, namespace)
        seed = _build_seed_contract(values, public_id=public_id, actor=actor)
        title = f"{public_id} — Ouro intake: {seed['goal'][:80]}"
        task_id = kb.create_task(
            conn,
            title=title,
            body=_body(seed),
            assignee=None,
            created_by="ouro-intake",
            workspace_kind="scratch",
            tenant=seed["kanban"]["tenant"],
            priority=0,
            parents=(),
            triage=False,
            idempotency_key=seed["kanban"]["idempotency_key"],
            public_id=public_id,
            skills=None,
            review_phase=None,
            routing_verdict={
                "verdict": "blocked",
                "status": "proposed_only",
                "reason": "Ouro intake admission records a Seed Contract only; execution requires Chris approval and Kanban promotion.",
            },
            admission_snapshot={
                "source": "ouro_intake",
                "public_id": public_id,
                "tenant": seed["kanban"]["tenant"],
                "executor_dispatch": "forbidden_during_admission",
                "approval_boundary": "human_approval_required",
                "seed_review_mode": seed["seed_review"]["mode"],
                "ambiguity_flags": seed["seed_review"]["ambiguity_flags"],
                "seed_contract_sha256": seed["seed_contract_sha256"],
            },
            closeout_evidence={
                "policy": "admission_only_no_execution",
                "evidence_status": "not_started",
            },
        )
        kb.block_task(conn, task_id, reason=None)
        kb.add_comment(
            conn,
            task_id,
            "ouro-intake",
            "Admission-only block: Seed Contract is source material, not executable authority. "
            "executor_dispatch=forbidden_during_admission; Chris approval plus Kanban promotion is required before implementation.",
        )
        rb = _readback(conn, task_id)

    message = (
        f"Created Ouro intake admission card {public_id} ({task_id}).\n"
        f"Status: {rb['status']} | assignee: {rb['assignee'] or 'none'} | task_runs: {rb['task_runs']}\n"
        "Boundary: no worker dispatched; execution remains blocked until Chris explicitly approves a Kanban transition."
    )
    return OuroIntakeResult(
        action="created",
        mutated=True,
        dispatched=False,
        task_id=task_id,
        public_id=public_id,
        message=message,
    )
