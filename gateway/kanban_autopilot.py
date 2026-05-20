"""Kanban-first /autopilot command surface.

Phase 0 is intentionally honest and non-executing: it restores the command
import/status path without presenting an enabled state file as runtime proof or
claiming any worker-dispatch authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

AUTOPILOT_STATE_FILE = "gateway_autopilot_state.json"
AUTOPILOT_USAGE = "/autopilot [status|dry-run|once|on|off]"
_READ_ONLY_ACTIONS = {"status", "dry-run", "dry_run"}
_CONTROL_ACTIONS = {"on", "off", "once"}


@dataclass(frozen=True)
class AutopilotCommand:
    """Parsed /autopilot command shape."""

    action: str
    raw_args: str = ""

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


def _state_path() -> Path:
    return get_hermes_home() / AUTOPILOT_STATE_FILE


def _read_state(path: Optional[Path] = None) -> dict[str, Any]:
    state_path = path or _state_path()
    try:
        raw = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"exists": False, "enabled": False, "path": str(state_path)}
    except OSError as exc:
        return {"exists": False, "enabled": False, "path": str(state_path), "read_error": str(exc)}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            "exists": True,
            "enabled": False,
            "path": str(state_path),
            "read_error": f"invalid_json:{exc.msg}",
        }
    if not isinstance(data, dict):
        return {"exists": True, "enabled": False, "path": str(state_path), "read_error": "state_not_object"}
    return {**data, "exists": True, "enabled": bool(data.get("enabled")), "path": str(state_path)}


def parse_autopilot_args(raw_args: str) -> AutopilotCommand:
    """Parse the deliberately narrow Phase-0 command surface."""

    normalized = str(raw_args or "").strip().lower().replace("_", "-")
    if not normalized:
        normalized = "status"
    if normalized in {"status", "dry-run", "on", "off", "once"}:
        return AutopilotCommand(action=normalized, raw_args=str(raw_args or ""))
    raise ValueError(f"unsupported /autopilot command: {raw_args!r}; usage: {AUTOPILOT_USAGE}")


def _status_decision(command: AutopilotCommand, *, actor: Optional[str] = None) -> dict[str, Any]:
    state = _read_state()
    desired_mode = "enabled" if state.get("enabled") else "disabled"
    reasons = ["phase0_status_only_no_execution_authority"]
    if state.get("read_error"):
        reasons.append(str(state["read_error"]))
    if state.get("enabled"):
        reasons.append("state_file_enabled_true_is_not_runtime_proof")
    return {
        "action": command.action,
        "actor": actor,
        "desired_mode": desired_mode,
        "effective_mode": "blocked" if state.get("enabled") else "degraded",
        "status": "BLOCKED" if state.get("enabled") else "DEGRADED",
        "reason": ";".join(reasons),
        "state_file": state,
        "state_file_enabled_is_execution_proof": False,
        "read_only": True,
        "mutations_attempted": [],
        "dispatch_once_called": False,
        "claim_task_called": False,
        "worker_spawn_called": False,
        "kanban_mutation_called": False,
    }


def _format_status_message(decision: dict[str, Any]) -> str:
    state = decision.get("state_file") or {}
    return "\n".join(
        [
            "Autopilot status: " + str(decision["status"]),
            f"desired_mode={decision['desired_mode']}",
            f"effective_mode={decision['effective_mode']}",
            "reason=" + str(decision["reason"]),
            "State file enabled=true is not execution proof.",
            "No dispatch, claim, worker spawn, or Kanban mutation was attempted.",
            f"state_file={state.get('path')}",
        ]
    )


def _blocked_control_decision(command: AutopilotCommand, *, actor: Optional[str] = None) -> dict[str, Any]:
    state = _read_state()
    return {
        "action": command.action,
        "actor": actor,
        "desired_mode": "enabled" if state.get("enabled") else "disabled",
        "effective_mode": "blocked",
        "status": "BLOCKED",
        "reason": "phase0_control_actions_are_non_mutating_until_later_approved_child",
        "state_file": state,
        "state_file_enabled_is_execution_proof": False,
        "read_only": False,
        "mutations_attempted": [],
    }


def handle_autopilot_command(raw_args: str = "", *, actor: Optional[str] = None) -> AutopilotResult:
    """Handle /autopilot without execution authority in Phase 0.

    Status/dry-run are read-only.  Control verbs fail closed rather than writing
    state or touching the dispatcher; later BO-078+ children can add explicit
    controller state and eligibility logic under their own approval boundaries.
    """

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

    if command.action in _READ_ONLY_ACTIONS:
        decision = _status_decision(command, actor=actor)
        return AutopilotResult(
            ok=True,
            command=command,
            message=_format_status_message(decision),
            decision=decision,
            fail_closed=False,
        )

    if command.action in _CONTROL_ACTIONS:
        decision = _blocked_control_decision(command, actor=actor)
        return AutopilotResult(
            ok=False,
            command=command,
            message=(
                "Autopilot control action blocked in Phase 0: "
                f"{command.action}. No state, dispatcher, worker, or Kanban mutation was attempted."
            ),
            decision=decision,
            fail_closed=True,
        )

    decision = {"status": "BLOCKED", "reason": "unsupported_action", "mutations_attempted": []}
    return AutopilotResult(False, command, f"Unsupported /autopilot action. {AUTOPILOT_USAGE}", decision, True)
