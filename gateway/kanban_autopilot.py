"""Kanban-first /autopilot command surface.

Phase 1 adds a durable controller-state skeleton without granting execution
authority.  The controller can remember desired mode and focus, but it must not
call the dispatcher, claim tasks, spawn workers, or mutate Kanban task state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

AUTOPILOT_STATE_FILE = "gateway_autopilot_state.json"
AUTOPILOT_STATE_VERSION = 2
AUTOPILOT_USAGE = "/autopilot [status|dry-run|queue|on|pause [reason]|pause-lane <tenant> [reason]|resume-lane <tenant>|hard-stop <reason>|recover <ack>|off|stop|focus <BO-123>]"
_READ_ONLY_ACTIONS = {"status", "dry-run", "dry_run", "queue"}
_CONTROL_ACTIONS = {"on", "off", "stop", "pause", "pause-lane", "resume-lane", "hard-stop", "recover", "focus", "once"}
_MUTATIONS_ATTEMPTED: list[str] = []
_READY_GATE_REQUIREMENTS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("missing_goal", ("goal:", "goal -", "objective:"), "goal"),
    ("missing_end_state", ("end-state", "end state", "output:", "deliverable"), "end-state/output"),
    ("missing_scope_non_goals", ("scope/non-goals", "non-goals", "non goals", "out of scope"), "scope/non-goals"),
    ("missing_acceptance_criteria", ("acceptance criteria", "acceptance:"), "acceptance criteria"),
    ("missing_verification_requirements", ("verification requirements", "verification:", "tests_run", "test:"), "verification requirements"),
    ("missing_authority_boundary", ("authority boundary", "authority:", "kanban"), "authority boundary"),
    ("missing_repo_lane_truth", ("repo/lane truth", "repo_full_name", "repository", "branch"), "repo/lane truth"),
    ("missing_risk_flags", ("risk flags", "risk:", "env", "secret", "prod", "customer-visible", "restart"), "risk flags"),
    ("missing_dependencies_blockers", ("dependencies/blockers", "dependencies:", "blockers:", "none"), "dependencies/blockers"),
    ("missing_review_package_expectation", ("review package", "review_ready", "changed files", "commit"), "review package expectation"),
)
_PR_URL_RE = re.compile(r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/\d+/?$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_RELEASE_ONLY_BASES = {"prod", "production"}
_CLOSED_LOOP_ALLOWED_STATES = [
    "disabled",
    "dry_run",
    "single_flight",
    "bounded_multi_tick",
    "parent_scoped",
    "lane_scoped",
    "paused",
    "hard_stopped",
    "needs_human",
]
_DEFAULT_CLOSED_LOOP_CAPS = {
    "max_active_flights": 1,
    "max_dispatches_per_tick": 1,
    "max_tasks_per_run_single_flight": 1,
    "max_tasks_per_run_early_bounded_multi_tick": 2,
    "max_new_prs_per_run": 1,
    "max_open_autopilot_prs": 2,
    "max_consecutive_failures": 1,
    "max_no_progress_ticks": 1,
    "max_same_card_retries": 1,
    "max_runtime_minutes": 60,
    "max_daily_autopilot_tasks": 3,
    "require_clean_closeout_per_task": True,
    "require_review_ready_contract_before_next_task": True,
}


def get_closed_loop_operating_contract() -> dict[str, Any]:
    """Return the BO-091 closed-loop Autopilot ADR/operating contract.

    The contract is intentionally data-shaped so later slices can validate
    policy/config files against the same invariants instead of relying on prose.
    It grants no runtime authority: dispatcher handoff and worker completion
    remain separate later slices and explicit approval gates.
    """

    return {
        "adr": "bounded_controller_not_executor",
        "authority_ceiling": "review_ready_pr",
        "state_machine": {
            "allowed_states": list(_CLOSED_LOOP_ALLOWED_STATES),
            "promotion_ladder": [
                ["dry_run", "single_flight"],
                ["single_flight", "bounded_multi_tick"],
                ["bounded_multi_tick", "parent_scoped"],
                ["bounded_multi_tick", "lane_scoped"],
            ],
            "terminal_or_human_states": ["paused", "hard_stopped", "needs_human"],
        },
        "default_caps": dict(_DEFAULT_CLOSED_LOOP_CAPS),
        "dispatcher_boundary": {
            "execution_owner": "existing_kanban_dispatcher",
            "autopilot_role": "controller_policy_evidence_layer",
            "autopilot_may_directly_claim_or_spawn": False,
            "second_dispatcher_allowed": False,
            "handoff_success_is_worker_completion": False,
            "worker_done_truth_source": "kanban_dispatcher_worker_done_evidence",
        },
        "scope_model": {
            "selectors": ["parent_public_id", "lane_tenant", "repo_project", "labels", "assignee_or_profile"],
            "scope_can_silently_widen": False,
            "scope_escape_result": "needs_human_or_activation_rejected",
        },
        "forbidden_without_current_approval": [
            "gateway_restart_reload",
            "config_env_secret_provider_billing_pricing_mutation",
            "worker_dispatch_claim_spawn_live_side_effect_before_activation_slice",
            "fork_push_or_pr_before_child_range_worker_done",
            "upstream_pr",
            "merge_release_deploy_prod_customer_visible_action",
            "canonical_main_sync_or_materialization",
        ],
        "stop_conditions": [
            "forbidden_action_requested",
            "scope_ambiguity",
            "stale_kanban_state",
            "dependency_or_blocker_detected",
            "verification_failure_threshold_exceeded",
            "worker_crash_or_timeout_repeated",
            "dispatcher_unavailable",
            "kanban_read_unavailable",
            "policy_file_invalid_or_stale",
            "worker_completion_evidence_missing",
            "budget_or_cap_exceeded",
            "pr_backlog_cap_exceeded",
            "disk_ci_runtime_safety_threshold_exceeded",
            "current_turn_approval_required_or_expired",
        ],
        "future_ralplan_required_for": [
            "merge_release_deploy_prod_customer_visible_authority",
            "gateway_restart_reload_automation",
            "config_env_secret_provider_billing_pricing_mutation",
            "replace_or_bypass_existing_dispatcher",
            "new_dispatcher_worker_lifecycle_ownership",
            "global_queue_draining_autonomy",
            "cross_repo_lane_parent_scope_expansion",
            "customer_facing_automation",
            "destructive_cleanup_authority",
            "security_auth_payment_billing_policy_mutation",
        ],
        "docs": "docs/closed-loop-kanban-autopilot.md",
    }


def validate_closed_loop_policy_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate that a proposed closed-loop policy preserves BO-091 invariants."""

    reason_codes: list[str] = []
    if contract.get("adr") != "bounded_controller_not_executor":
        reason_codes.append("adr_must_be_bounded_controller_not_executor")
    if contract.get("authority_ceiling") != "review_ready_pr":
        reason_codes.append("authority_ceiling_must_be_review_ready_pr")
    states = (contract.get("state_machine") or {}).get("allowed_states") or []
    for state in _CLOSED_LOOP_ALLOWED_STATES:
        if state not in states:
            reason_codes.append(f"missing_state_{state}")
    caps = contract.get("default_caps") or {}
    if caps.get("max_dispatches_per_tick") != 1:
        reason_codes.append("max_dispatches_per_tick_must_be_one")
    if caps.get("max_active_flights") != 1:
        reason_codes.append("max_active_flights_must_be_one")
    boundary = contract.get("dispatcher_boundary") or {}
    if boundary.get("execution_owner") != "existing_kanban_dispatcher":
        reason_codes.append("execution_owner_must_be_existing_dispatcher")
    if boundary.get("autopilot_may_directly_claim_or_spawn") is not False:
        reason_codes.append("direct_claim_or_spawn_not_allowed")
    if boundary.get("second_dispatcher_allowed") is not False:
        reason_codes.append("second_dispatcher_not_allowed")
    if boundary.get("handoff_success_is_worker_completion") is not False:
        reason_codes.append("handoff_success_must_not_equal_worker_completion")
    scope = contract.get("scope_model") or {}
    if scope.get("scope_can_silently_widen") is not False:
        reason_codes.append("scope_must_not_silently_widen")
    forbidden = set(contract.get("forbidden_without_current_approval") or [])
    for required in {"gateway_restart_reload", "config_env_secret_provider_billing_pricing_mutation"}:
        if required not in forbidden:
            reason_codes.append(f"missing_forbidden_{required}")
    future = set(contract.get("future_ralplan_required_for") or [])
    if "merge_release_deploy_prod_customer_visible_authority" not in future:
        reason_codes.append("future_ralplan_gate_missing_merge_release_prod")
    return {"ok": not reason_codes, "reason_codes": reason_codes}


@dataclass(frozen=True)
class AutopilotCommand:
    """Parsed /autopilot command shape."""

    action: str
    raw_args: str = ""
    value: str = ""

    @property
    def read_only(self) -> bool:
        return self.action in _READ_ONLY_ACTIONS


@dataclass(frozen=True)
class AutopilotResult:
    """Structured decision plus gateway/CLI friendly message."""

    ok: bool
    command: Optional[AutopilotCommand]
    message: str
    decision: dict[str, Any]
    fail_closed: bool = False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path() -> Path:
    return get_hermes_home() / AUTOPILOT_STATE_FILE


def _read_state(path: Optional[Path] = None) -> dict[str, Any]:
    state_path = path or _state_path()
    try:
        raw = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"exists": False, "enabled": False, "desired_mode": "disabled", "path": str(state_path)}
    except OSError as exc:
        return {
            "exists": False,
            "enabled": False,
            "desired_mode": "disabled",
            "path": str(state_path),
            "read_error": str(exc),
        }
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "exists": True,
            "enabled": False,
            "desired_mode": "disabled",
            "path": str(state_path),
            "read_error": f"invalid_json:{exc.msg}",
        }
    if not isinstance(data, dict):
        return {
            "exists": True,
            "enabled": False,
            "desired_mode": "disabled",
            "path": str(state_path),
            "read_error": "state_not_object",
        }
    enabled = bool(data.get("enabled"))
    desired = str(data.get("desired_mode") or ("enabled" if enabled else "disabled")).strip().lower()
    return {**data, "exists": True, "enabled": enabled, "desired_mode": desired, "path": str(state_path)}


def _write_state(state: dict[str, Any], path: Optional[Path] = None) -> dict[str, Any]:
    state_path = path or _state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return _read_state(state_path)


def parse_autopilot_args(raw_args: str) -> AutopilotCommand:
    """Parse the deliberately narrow Phase-1 command surface."""

    raw = str(raw_args or "").strip()
    if not raw:
        return AutopilotCommand(action="status", raw_args="")
    parts = raw.split(maxsplit=1)
    action = parts[0].strip().lower().replace("_", "-")
    value = parts[1].strip() if len(parts) > 1 else ""
    if action == "dry_run":
        action = "dry-run"
    if action in {"status", "dry-run", "queue", "on", "off", "stop", "pause", "pause-lane", "resume-lane", "hard-stop", "recover", "focus", "once"}:
        return AutopilotCommand(action=action, raw_args=raw, value=value)
    raise ValueError(f"unsupported /autopilot command: {raw_args!r}; usage: {AUTOPILOT_USAGE}")


def _effective_mode(desired_mode: str, state: dict[str, Any]) -> str:
    desired = str(desired_mode or "disabled").lower()
    if desired == "hard_stopped":
        return "hard_stop"
    if desired in {"stopped", "off", "disabled"}:
        return "stopped" if desired == "stopped" else "degraded"
    if desired == "paused":
        return "paused"
    # Phase 1 records controller intent only.  Even desired=on remains blocked
    # until later BO-079/BO-080 gates prove safe execution eligibility.
    if desired in {"on", "enabled"}:
        return "blocked"
    if state.get("read_error"):
        return "degraded"
    return "degraded"


def evaluate_autopilot_ready_gate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a dry-run Ready-gate verdict for one Kanban candidate.

    This deliberately treats raw Kanban ``status=ready`` as insufficient.  The
    candidate must carry an executable contract before a later autopilot phase
    may even consider selection.  The function is pure/read-only: no dispatcher,
    claim, spawn, or Kanban mutation calls are made here.
    """

    body = str(candidate.get("body") or "")
    title = str(candidate.get("title") or "")
    haystack = "\n".join([title, body, json.dumps(candidate, ensure_ascii=False, sort_keys=True)]).lower()
    reason_codes: list[str] = []
    missing_labels: list[str] = []
    if str(candidate.get("status") or "").lower() != "ready":
        reason_codes.append("kanban_status_not_ready")
        missing_labels.append("Kanban status=ready")
    for code, markers, label in _READY_GATE_REQUIREMENTS:
        if not any(marker in haystack for marker in markers):
            reason_codes.append(code)
            missing_labels.append(label)
    routing = candidate.get("routing_verdict") or {}
    if isinstance(routing, str):
        try:
            routing = json.loads(routing)
        except json.JSONDecodeError:
            routing = {"raw": routing}
    verdict = str((routing or {}).get("verdict") or "").strip().lower()
    if not verdict:
        reason_codes.append("missing_routing_verdict")
        missing_labels.append("routing verdict")
    accepted = not reason_codes
    return {
        "task_id": candidate.get("id"),
        "public_id": candidate.get("public_id"),
        "autopilot_ready": accepted,
        "status": "accepted" if accepted else "rejected",
        "reason_codes": reason_codes,
        "human_reason": "ready for autopilot dry-run selection" if accepted else "Missing executable contract fields: " + ", ".join(missing_labels),
        "dry_run_side_effects": {"claimed": 0, "spawned": 0, "mutated": 0},
    }


def evaluate_review_ready_contract(
    evidence: dict[str, Any],
    *,
    expected_repo_full_name: str = "chriskim12/hermes-agent",
) -> dict[str, Any]:
    """Validate the review-ready PR package without allowing merge/release."""

    reason_codes: list[str] = []
    required = {
        "repo_full_name": evidence.get("repo_full_name"),
        "commit": evidence.get("commit") or evidence.get("head_sha"),
        "task_branch": evidence.get("task_branch") or evidence.get("branch"),
        "pr_url": evidence.get("pr_url"),
        "pr_base": evidence.get("pr_base"),
        "pr_head": evidence.get("pr_head"),
    }
    for field, value in required.items():
        if not str(value or "").strip():
            reason_codes.append(f"missing_{field}")
    bool_required = {
        "checks_passed": evidence.get("checks_passed"),
        "worktree_clean": evidence.get("worktree_clean"),
        "kanban_worker_done": evidence.get("kanban_worker_done"),
        "boundaries_confirmed": evidence.get("boundaries_confirmed"),
    }
    for field, value in bool_required.items():
        if value is not True:
            reason_codes.append(f"missing_{field}")
    commit = str(required["commit"] or "").strip()
    if commit and not _SHA_RE.fullmatch(commit):
        reason_codes.append("commit_not_sha_like")
    pr_url = str(required["pr_url"] or "").strip()
    pr_match = _PR_URL_RE.fullmatch(pr_url) if pr_url else None
    if pr_url and not pr_match:
        reason_codes.append("pr_url_not_github_pull_url")
    if pr_match:
        pr_repo = f"{pr_match.group('owner')}/{pr_match.group('repo')}".lower()
        if pr_repo != expected_repo_full_name.lower():
            reason_codes.append("pr_url_repo_mismatch")
    repo = str(required["repo_full_name"] or "").strip().lower()
    if repo and repo != expected_repo_full_name.lower():
        reason_codes.append("repo_full_name_mismatch")
    task_branch = str(required["task_branch"] or "").strip()
    pr_head = str(required["pr_head"] or "").strip()
    if task_branch and pr_head and task_branch != pr_head:
        reason_codes.append("pr_head_task_branch_mismatch")
    pr_base = str(required["pr_base"] or "").strip().lower()
    if pr_base in _RELEASE_ONLY_BASES:
        reason_codes.append("release_base_requires_separate_approval")
    return {
        "review_ready": not reason_codes,
        "status": "review_ready" if not reason_codes else "blocked",
        "reason_codes": reason_codes,
        "merge_allowed": False,
        "release_allowed": False,
        "prod_customer_visible_allowed": False,
        "gateway_restart_reload_allowed": False,
        "human_reason": "review-ready PR contract satisfied; merge/release still forbidden" if not reason_codes else "Review-ready PR contract missing or unsafe: " + ", ".join(reason_codes),
    }


def evaluate_dispatcher_eligibility(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Dry-run which Ready-gate-passing tasks could later reach dispatcher.

    This is a bridge to the existing Kanban dispatcher, not a second dispatcher.
    It returns handoff-shaped facts but performs no claim/spawn/DB transition.
    """

    state = _read_state()
    effective = _effective_mode(str(state.get("desired_mode") or "disabled"), state)
    paused_lanes = {str(lane).lower() for lane in (state.get("paused_lanes") or [])}
    eligible: list[dict[str, Any]] = []
    ineligible: list[dict[str, Any]] = []
    for candidate in candidates:
        gate = evaluate_autopilot_ready_gate(candidate)
        tenant = str(candidate.get("tenant") or "").lower()
        if effective == "hard_stop":
            gate = {
                **gate,
                "autopilot_ready": False,
                "status": "rejected",
                "reason_codes": ["autopilot_hard_stop_active"],
                "human_reason": "Autopilot hard-stop is active; explicit operator recovery is required.",
            }
        elif tenant and tenant in paused_lanes:
            gate = {
                **gate,
                "autopilot_ready": False,
                "status": "rejected",
                "reason_codes": ["autopilot_lane_paused"],
                "human_reason": f"Autopilot lane is paused: {tenant}",
            }
        elif effective != "blocked":
            gate = {
                **gate,
                "autopilot_ready": False,
                "status": "rejected",
                "reason_codes": ["autopilot_effective_mode_not_blocked_pending_dispatch_gate"],
                "human_reason": "Autopilot controller is not in desired=on/effective=blocked pending-dispatch-gate mode.",
            }
        if gate["autopilot_ready"]:
            eligible.append(gate)
        else:
            ineligible.append(gate)
    return {
        "controller_effective_mode": effective,
        "paused_lanes": sorted(paused_lanes),
        "handoff_target": "existing_kanban_dispatcher",
        "second_dispatcher_created": False,
        "eligible": eligible,
        "ineligible": ineligible,
        "dry_run_side_effects": {"claimed": 0, "spawned": 0, "mutated": 0},
    }


def _queue_decision(command: AutopilotCommand, *, actor: Optional[str], candidates: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    candidate_list = candidates or []
    results = [evaluate_autopilot_ready_gate(candidate) for candidate in candidate_list]
    dispatcher_eligibility = evaluate_dispatcher_eligibility(candidate_list)
    return {
        "action": command.action,
        "actor": actor,
        "read_only": True,
        "status": "DRY_RUN",
        "reason": "phase3_dispatcher_eligibility_bridge_no_execution_authority",
        "candidates": results,
        "dispatcher_eligibility": dispatcher_eligibility,
        "accepted_count": sum(1 for result in results if result["autopilot_ready"]),
        "rejected_count": sum(1 for result in results if not result["autopilot_ready"]),
        "dry_run_side_effects": {"claimed": 0, "spawned": 0, "mutated": 0},
        "mutations_attempted": list(_MUTATIONS_ATTEMPTED),
        "state_file_enabled_is_execution_proof": False,
    }


def _format_queue_message(decision: dict[str, Any]) -> str:
    lines = [
        "Autopilot queue dry-run: READY-GATE ONLY",
        f"accepted={decision['accepted_count']}",
        f"rejected={decision['rejected_count']}",
        "No dispatch, claim, worker spawn, or Kanban mutation was attempted.",
    ]
    for candidate in decision.get("candidates") or []:
        lines.append(
            f"- {candidate.get('public_id') or candidate.get('task_id')}: {candidate['status']} "
            f"({', '.join(candidate['reason_codes']) or 'ok'})"
        )
    return "\n".join(lines)


def _status_decision(command: AutopilotCommand, *, actor: Optional[str] = None) -> dict[str, Any]:
    state = _read_state()
    desired_mode = str(state.get("desired_mode") or "disabled")
    effective = _effective_mode(desired_mode, state)
    reasons = ["phase1_controller_state_only_no_execution_authority"]
    if state.get("read_error"):
        reasons.append(str(state["read_error"]))
    if state.get("enabled") or desired_mode in {"on", "enabled"}:
        reasons.append("state_file_enabled_true_is_not_runtime_proof")
    return {
        "action": command.action,
        "actor": actor,
        "desired_mode": desired_mode,
        "effective_mode": effective,
        "status": effective.upper(),
        "reason": ";".join(reasons),
        "focus": state.get("focus"),
        "pause_reason": state.get("pause_reason"),
        "paused_lanes": state.get("paused_lanes") or [],
        "hard_stop_reason": state.get("hard_stop_reason"),
        "dispatch_blocked": effective in {"blocked", "paused", "hard_stop", "stopped", "degraded"},
        "operator_recovery_required": effective == "hard_stop",
        "state_file": state,
        "state_file_enabled_is_execution_proof": False,
        "read_only": command.read_only,
        "mutations_attempted": list(_MUTATIONS_ATTEMPTED),
        "dispatch_once_called": False,
        "claim_task_called": False,
        "worker_spawn_called": False,
        "kanban_mutation_called": False,
    }


def _format_status_message(decision: dict[str, Any]) -> str:
    state = decision.get("state_file") or {}
    lines = [
        "Autopilot status: " + str(decision["status"]),
        f"desired_mode={decision['desired_mode']}",
        f"effective_mode={decision['effective_mode']}",
    ]
    if decision.get("focus"):
        lines.append(f"focus={decision['focus']}")
    if decision.get("pause_reason"):
        lines.append(f"pause_reason={decision['pause_reason']}")
    lines.extend(
        [
            "reason=" + str(decision["reason"]),
            "State file enabled=true is not execution proof.",
            "No dispatch, claim, worker spawn, or Kanban mutation was attempted.",
            f"state_file={state.get('path')}",
        ]
    )
    return "\n".join(lines)


def _controller_state_for(command: AutopilotCommand, *, actor: Optional[str]) -> dict[str, Any]:
    previous = _read_state()
    state: dict[str, Any] = {
        "version": AUTOPILOT_STATE_VERSION,
        "enabled": bool(previous.get("enabled")),
        "desired_mode": str(previous.get("desired_mode") or ("enabled" if previous.get("enabled") else "disabled")),
        "focus": previous.get("focus"),
        "pause_reason": previous.get("pause_reason"),
        "paused_lanes": previous.get("paused_lanes") or [],
        "hard_stop_reason": previous.get("hard_stop_reason"),
        "updated_at": _iso_now(),
        "updated_by": actor or "unknown",
    }
    if command.action == "on":
        state.update({"desired_mode": "on", "enabled": True, "pause_reason": None})
    elif command.action in {"off", "stop"}:
        state.update({"desired_mode": "stopped", "enabled": False, "pause_reason": None})
    elif command.action == "pause":
        state.update({"desired_mode": "paused", "enabled": False, "pause_reason": command.value or "manual_pause"})
    elif command.action == "pause-lane":
        parts = command.value.split(maxsplit=1)
        lane = parts[0].strip().lower() if parts else ""
        reason = parts[1].strip() if len(parts) > 1 else "manual_lane_pause"
        paused = {str(existing).lower() for existing in (state.get("paused_lanes") or [])}
        if lane:
            paused.add(lane)
        state.update({"paused_lanes": sorted(paused), "pause_reason": reason})
    elif command.action == "resume-lane":
        lane = command.value.split(maxsplit=1)[0].strip().lower()
        paused = {str(existing).lower() for existing in (state.get("paused_lanes") or [])}
        if lane:
            paused.discard(lane)
        state.update({"paused_lanes": sorted(paused)})
    elif command.action == "hard-stop":
        state.update({"desired_mode": "hard_stopped", "enabled": False, "hard_stop_reason": command.value or "manual_hard_stop", "pause_reason": command.value or "manual_hard_stop"})
    elif command.action == "recover":
        state.update({"desired_mode": "paused", "enabled": False, "hard_stop_reason": None, "pause_reason": command.value or "manual_recovery_acknowledged"})
    elif command.action == "focus":
        state.update({"focus": command.value.upper() if command.value else None})
    elif command.action == "once":
        state.update({"desired_mode": "paused", "enabled": False, "pause_reason": "once_not_available_until_dispatch_gate"})
    return state


def _control_decision(command: AutopilotCommand, *, actor: Optional[str] = None) -> dict[str, Any]:
    state = _write_state(_controller_state_for(command, actor=actor))
    desired_mode = str(state.get("desired_mode") or "disabled")
    effective = _effective_mode(desired_mode, state)
    return {
        "action": command.action,
        "actor": actor,
        "desired_mode": desired_mode,
        "effective_mode": effective,
        "status": effective.upper(),
        "reason": "phase1_controller_state_persisted_without_execution_authority",
        "focus": state.get("focus"),
        "pause_reason": state.get("pause_reason"),
        "paused_lanes": state.get("paused_lanes") or [],
        "hard_stop_reason": state.get("hard_stop_reason"),
        "dispatch_blocked": effective in {"blocked", "paused", "hard_stop", "stopped", "degraded"},
        "operator_recovery_required": effective == "hard_stop",
        "state_file": state,
        "state_file_enabled_is_execution_proof": False,
        "read_only": False,
        "mutations_attempted": list(_MUTATIONS_ATTEMPTED),
        "dispatch_once_called": False,
        "claim_task_called": False,
        "worker_spawn_called": False,
        "kanban_mutation_called": False,
    }


def handle_autopilot_command(
    raw_args: str = "",
    *,
    actor: Optional[str] = None,
    candidates: Optional[list[dict[str, Any]]] = None,
) -> AutopilotResult:
    """Handle /autopilot without execution authority in Phase 2."""

    try:
        command = parse_autopilot_args(raw_args)
    except ValueError as exc:
        return AutopilotResult(
            ok=False,
            command=None,
            message=str(exc),
            decision={"status": "BLOCKED", "reason": "parse_error", "mutations_attempted": []},
            fail_closed=True,
        )

    if command.action == "queue":
        decision = _queue_decision(command, actor=actor, candidates=candidates)
        return AutopilotResult(True, command, _format_queue_message(decision), decision, False)

    if command.action in _READ_ONLY_ACTIONS:
        decision = _status_decision(command, actor=actor)
        return AutopilotResult(True, command, _format_status_message(decision), decision, False)

    if command.action in _CONTROL_ACTIONS:
        decision = _control_decision(command, actor=actor)
        return AutopilotResult(
            ok=True,
            command=command,
            message=_format_status_message(decision),
            decision=decision,
            fail_closed=False,
        )

    decision = {"status": "BLOCKED", "reason": "unsupported_action", "mutations_attempted": []}
    return AutopilotResult(False, command, f"Unsupported /autopilot action. {AUTOPILOT_USAGE}", decision, True)
