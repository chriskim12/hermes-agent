"""Kanban-first /autopilot command surface.

Phase 1 adds a durable controller-state skeleton without granting execution
authority.  The controller can remember desired mode and focus, but it must not
call the dispatcher, claim tasks, spawn workers, or mutate Kanban task state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

AUTOPILOT_STATE_FILE = "gateway_autopilot_state.json"
AUTOPILOT_STATE_VERSION = 2
AUTOPILOT_USAGE = "/autopilot [status|dry-run|on|pause [reason]|off|stop|focus <BO-123>]"
_READ_ONLY_ACTIONS = {"status", "dry-run", "dry_run"}
_CONTROL_ACTIONS = {"on", "off", "stop", "pause", "focus", "once"}
_MUTATIONS_ATTEMPTED: list[str] = []


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
    if action in {"status", "dry-run", "on", "off", "stop", "pause", "focus", "once"}:
        return AutopilotCommand(action=action, raw_args=raw, value=value)
    raise ValueError(f"unsupported /autopilot command: {raw_args!r}; usage: {AUTOPILOT_USAGE}")


def _effective_mode(desired_mode: str, state: dict[str, Any]) -> str:
    desired = str(desired_mode or "disabled").lower()
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
        "updated_at": _iso_now(),
        "updated_by": actor or "unknown",
    }
    if command.action == "on":
        state.update({"desired_mode": "on", "enabled": True, "pause_reason": None})
    elif command.action in {"off", "stop"}:
        state.update({"desired_mode": "stopped", "enabled": False, "pause_reason": None})
    elif command.action == "pause":
        state.update({"desired_mode": "paused", "enabled": False, "pause_reason": command.value or "manual_pause"})
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
        "state_file": state,
        "state_file_enabled_is_execution_proof": False,
        "read_only": False,
        "mutations_attempted": list(_MUTATIONS_ATTEMPTED),
        "dispatch_once_called": False,
        "claim_task_called": False,
        "worker_spawn_called": False,
        "kanban_mutation_called": False,
    }


def handle_autopilot_command(raw_args: str = "", *, actor: Optional[str] = None) -> AutopilotResult:
    """Handle /autopilot without execution authority in Phase 1."""

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
