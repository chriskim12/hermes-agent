"""Hermes /autopilot command controller surface.

CH-385 deliberately stops at the command/plugin entrypoint and deterministic
classification layer.  It may record controller intent (ON/OFF) and read Linear /
work_state truth, but it must not spawn executors, bypass admission, or mark
Linear cards Done.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from hermes_constants import get_hermes_home


AUTOPILOT_USAGE = (
    "/autopilot [status|dry-run|ON|OFF|CH-123|status CH-123|dry-run CH-123]"
)
AUTOPILOT_STATE_VERSION = 1
AUTOPILOT_STATE_FILE = "gateway_autopilot_state.json"
_AUTOPILOT_TARGET_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

ACTION_STATUS = "status"
ACTION_DRY_RUN = "dry_run"
ACTION_ENABLE = "enable"
ACTION_DISABLE = "disable"
ACTION_ONE_SHOT = "one_shot"

READ_ONLY_ACTIONS = {ACTION_STATUS, ACTION_DRY_RUN}


class AutopilotParseError(ValueError):
    """Invalid /autopilot shape.  The caller must fail closed."""


class LinearIssueClient(Protocol):
    def fetch_issue(self, identifier: str) -> Mapping[str, Any]:
        """Return a Linear issue payload for *identifier* without mutating Linear."""


@dataclass(frozen=True)
class AutopilotCommand:
    """Deterministically parsed /autopilot command."""

    action: str
    target_id: Optional[str] = None
    raw_args: str = ""

    @property
    def read_only(self) -> bool:
        return self.action in READ_ONLY_ACTIONS


@dataclass(frozen=True)
class AutopilotResult:
    """Structured command result plus a gateway/CLI friendly message."""

    ok: bool
    command: Optional[AutopilotCommand]
    message: str
    decision: dict[str, Any]
    fail_closed: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _normalize_target_id(value: str) -> str:
    return str(value or "").strip().upper()


def _is_target_id(value: str) -> bool:
    return bool(_AUTOPILOT_TARGET_RE.fullmatch(_normalize_target_id(value)))


def parse_autopilot_args(raw_args: str) -> AutopilotCommand:
    """Parse accepted /autopilot shapes and reject everything else.

    Accepted shapes are intentionally narrow:
      - status
      - dry-run
      - ON
      - OFF
      - CH-123
      - status CH-123
      - dry-run CH-123

    A bare /autopilot is status for safe discoverability.  Unknown words,
    extra tokens, and ON/OFF with targets fail closed.
    """

    raw = str(raw_args or "").strip()
    tokens = raw.split()
    if not tokens:
        return AutopilotCommand(action=ACTION_STATUS, raw_args=raw)

    first = tokens[0]
    first_lower = first.lower()
    first_upper = first.upper()

    if first_lower == "status":
        if len(tokens) == 1:
            return AutopilotCommand(action=ACTION_STATUS, raw_args=raw)
        if len(tokens) == 2 and _is_target_id(tokens[1]):
            return AutopilotCommand(
                action=ACTION_STATUS,
                target_id=_normalize_target_id(tokens[1]),
                raw_args=raw,
            )
        raise AutopilotParseError(f"Invalid /autopilot status shape. Usage: {AUTOPILOT_USAGE}")

    if first_lower == "dry-run":
        if len(tokens) == 1:
            return AutopilotCommand(action=ACTION_DRY_RUN, raw_args=raw)
        if len(tokens) == 2 and _is_target_id(tokens[1]):
            return AutopilotCommand(
                action=ACTION_DRY_RUN,
                target_id=_normalize_target_id(tokens[1]),
                raw_args=raw,
            )
        raise AutopilotParseError(f"Invalid /autopilot dry-run shape. Usage: {AUTOPILOT_USAGE}")

    if first_upper == "ON":
        if len(tokens) == 1:
            return AutopilotCommand(action=ACTION_ENABLE, raw_args=raw)
        raise AutopilotParseError("/autopilot ON does not accept a target; admission stays separate.")

    if first_upper == "OFF":
        if len(tokens) == 1:
            return AutopilotCommand(action=ACTION_DISABLE, raw_args=raw)
        raise AutopilotParseError("/autopilot OFF does not accept a target.")

    if len(tokens) == 1 and _is_target_id(first):
        return AutopilotCommand(
            action=ACTION_ONE_SHOT,
            target_id=_normalize_target_id(first),
            raw_args=raw,
        )

    raise AutopilotParseError(f"Invalid /autopilot command. Usage: {AUTOPILOT_USAGE}")


class AutopilotStateStore:
    """Small Hermes-owned runtime-intent store for the controller surface."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else get_hermes_home() / AUTOPILOT_STATE_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "schema_version": AUTOPILOT_STATE_VERSION,
            "enabled": False,
            "updated_at": None,
            "updated_by": None,
            "source": "default_disabled_fail_closed",
        }

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default_state()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            state = self._default_state()
            state["state_error"] = f"invalid_state_file:{type(exc).__name__}"
            return state
        if not isinstance(payload, dict):
            state = self._default_state()
            state["state_error"] = "invalid_state_payload"
            return state
        state = self._default_state()
        state.update(payload)
        enabled = payload.get("enabled", state["enabled"])
        if not isinstance(enabled, bool):
            state["enabled"] = False
            state["state_error"] = "invalid_enabled_type"
        else:
            state["enabled"] = enabled
        state["schema_version"] = AUTOPILOT_STATE_VERSION
        return state

    def status(self) -> dict[str, Any]:
        return self.load()

    def set_enabled(
        self,
        enabled: bool,
        *,
        actor: Optional[str] = None,
        source: str = "autopilot_command",
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        state = self.load()
        state.update(
            {
                "schema_version": AUTOPILOT_STATE_VERSION,
                "enabled": bool(enabled),
                "updated_at": (now or _utcnow()).isoformat(),
                "updated_by": actor or "unknown",
                "source": source,
            }
        )
        self._write_atomic(state)
        return state

    def _write_atomic(self, payload: Mapping[str, Any]) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dict(payload), fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            Path(tmp_name).replace(self.path)
        except Exception:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            finally:
                raise


class EnvLinearIssueClient:
    """Read-only Linear GraphQL client using LINEAR_API_KEY from the environment."""

    _QUERY = """
    query AutopilotIssue($id: String!) {
      issue(id: $id) {
        identifier
        title
        state { name type }
        parent { identifier title state { name type } }
        children(first: 50) { nodes { identifier title state { name type } } }
      }
    }
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.getenv("LINEAR_API_KEY")

    def fetch_issue(self, identifier: str) -> Mapping[str, Any]:
        if not self.api_key:
            return {
                "status": "unavailable",
                "reason": "LINEAR_API_KEY_missing",
                "identifier": identifier,
            }
        body = json.dumps(
            {"query": self._QUERY, "variables": {"id": identifier}}
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.linear.app/graphql",
            data=body,
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {
                "status": "unavailable",
                "reason": f"linear_http_{exc.code}",
                "identifier": identifier,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "reason": f"linear_query_{type(exc).__name__}",
                "identifier": identifier,
            }

        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            return {
                "status": "unavailable",
                "reason": "linear_graphql_error",
                "identifier": identifier,
                "errors": errors,
            }
        issue = ((payload.get("data") or {}).get("issue") if isinstance(payload, dict) else None)
        if not issue:
            return {
                "status": "missing",
                "reason": "linear_issue_not_found",
                "identifier": identifier,
            }
        issue["status"] = "ok"
        return issue


def classify_linear_target(issue: Mapping[str, Any], requested_id: str) -> dict[str, Any]:
    """Classify one Linear issue as parent/child/standalone without mutation."""

    status = str(issue.get("status") or "ok")
    if status != "ok":
        return {
            "status": status,
            "reason": issue.get("reason") or status,
            "identifier": issue.get("identifier") or requested_id,
            "shape": "unknown",
            "execution_ready": False,
        }

    children = issue.get("children") or {}
    child_nodes = children.get("nodes") if isinstance(children, Mapping) else []
    child_nodes = child_nodes if isinstance(child_nodes, list) else []
    parent = issue.get("parent") if isinstance(issue.get("parent"), Mapping) else None
    if child_nodes:
        shape = "parent"
    elif parent:
        shape = "child"
    else:
        shape = "standalone"

    state = issue.get("state") if isinstance(issue.get("state"), Mapping) else {}
    state_name = str(state.get("name") or "").strip()
    state_type = str(state.get("type") or "").strip()
    return {
        "status": "ok",
        "identifier": issue.get("identifier") or requested_id,
        "title": issue.get("title") or "",
        "shape": shape,
        "state_name": state_name,
        "state_type": state_type,
        "execution_ready": state_name.lower() == "execution ready",
        "parent": {
            "identifier": parent.get("identifier"),
            "state_name": ((parent.get("state") or {}).get("name") if parent else None),
        }
        if parent
        else None,
        "children": [
            {
                "identifier": child.get("identifier"),
                "state_name": ((child.get("state") or {}).get("name") if isinstance(child, Mapping) else None),
            }
            for child in child_nodes
            if isinstance(child, Mapping)
        ],
    }


def summarize_work_state(work_state_store: Any = None) -> dict[str, Any]:
    """Return read-only work_state summary for /autopilot status/dry-run."""

    try:
        from gateway.work_state import LIVE_STATES

        store = work_state_store
        if store is None:
            from gateway.work_state import WorkStateStore

            store = WorkStateStore()
        records = store.list_records()
        counts_by_state: dict[str, int] = {}
        delegated_omx_live = 0
        hermes_owned_live = 0
        for record in records:
            state = str(getattr(record, "state", "") or "")
            counts_by_state[state] = counts_by_state.get(state, 0) + 1
            if state in LIVE_STATES and getattr(record, "owner", None) == "hermes":
                hermes_owned_live += 1
                if (
                    getattr(record, "executor", None) == "omx"
                    and getattr(record, "mode", None) == "delegated"
                ):
                    delegated_omx_live += 1
        return {
            "available": True,
            "records_total": len(records),
            "counts_by_state": counts_by_state,
            "hermes_owned_live": hermes_owned_live,
            "delegated_omx_live": delegated_omx_live,
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": f"work_state_unavailable:{type(exc).__name__}",
        }


def classify_work_state_target(target_id: str, work_state_store: Any = None) -> dict[str, Any]:
    """Resolve one target against work_state and classify without side effects."""

    try:
        from gateway.work_state import classify_delegated_omx_supervisor_action

        store = work_state_store
        if store is None:
            from gateway.work_state import WorkStateStore

            store = WorkStateStore()
        resolution = store.resolve_delegated_signal_candidate(
            work_id=target_id,
            live_only=True,
        )
        result: dict[str, Any] = {
            "available": True,
            "resolution_status": resolution.get("status"),
            "resolution_reason": resolution.get("reason"),
            "matches_count": len(resolution.get("matches") or []),
        }
        record = resolution.get("record")
        if resolution.get("status") == "single_match" and record is not None:
            decision = classify_delegated_omx_supervisor_action(record).to_dict()
            result["supervisor_decision"] = decision
        return result
    except Exception as exc:
        return {
            "available": False,
            "reason": f"work_state_target_unavailable:{type(exc).__name__}",
        }


def _admission_decision(
    *,
    command: AutopilotCommand,
    state: Mapping[str, Any],
    linear: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    if command.action in READ_ONLY_ACTIONS:
        return {
            "status": "read_only",
            "reason": f"{command.action}_never_starts_executor",
            "admission_bypassed": False,
        }
    if command.action == ACTION_ENABLE:
        return {
            "status": "controller_intent_enabled",
            "reason": "future_automatic_starts_still_require_admission",
            "admission_bypassed": False,
        }
    if command.action == ACTION_DISABLE:
        return {
            "status": "controller_intent_disabled",
            "reason": "new_automatic_starts_prevented",
            "admission_bypassed": False,
        }
    if command.action == ACTION_ONE_SHOT:
        if not linear or linear.get("status") != "ok":
            return {
                "status": "blocked",
                "reason": "linear_target_unavailable_fail_closed",
                "admission_bypassed": False,
            }
        if linear.get("shape") == "parent":
            return {
                "status": "requires_child_selection",
                "reason": "parent_target_requires_execution_ready_child_admission",
                "admission_bypassed": False,
            }
        if not linear.get("execution_ready"):
            return {
                "status": "blocked",
                "reason": "linear_target_not_execution_ready",
                "admission_bypassed": False,
            }
        return {
            "status": "eligible_for_admission",
            "reason": "one_shot_target_requires_admission_before_executor_start",
            "admission_bypassed": False,
        }
    return {
        "status": "blocked",
        "reason": "unknown_autopilot_action_fail_closed",
        "admission_bypassed": False,
    }


def _format_bool(value: Any) -> str:
    return "ON" if bool(value) else "OFF"


def _format_result_message(decision: Mapping[str, Any]) -> str:
    command = decision.get("command") or {}
    action = command.get("action") or "unknown"
    target_id = command.get("target_id")
    state = decision.get("state") or {}
    admission = decision.get("admission") or {}
    lines = [f"/autopilot {action.replace('_', '-')}" + (f" {target_id}" if target_id else "")]
    lines.append(f"Controller intent: {_format_bool(state.get('enabled'))}")
    if action in {ACTION_STATUS, ACTION_DRY_RUN}:
        lines.append("Mode: read-only — no executor spawn, no Linear mutation.")
    if action == ACTION_ENABLE:
        lines.append("ON recorded: automatic controller intent is enabled; admission is still mandatory.")
    elif action == ACTION_DISABLE:
        lines.append("OFF recorded: new automatic starts are prevented.")
    elif action == ACTION_ONE_SHOT:
        lines.append("One-shot target classified; executor spawn is not performed by this entrypoint.")
    if target_id:
        linear = decision.get("linear") or {}
        shape = linear.get("shape") or "unknown"
        linear_status = linear.get("status") or "unknown"
        state_name = linear.get("state_name") or linear.get("reason") or "unknown"
        lines.append(f"Linear target: {target_id} ({shape}, {linear_status}, {state_name})")
    work_state = decision.get("work_state") or {}
    if target_id:
        lines.append(
            "work_state: "
            f"{work_state.get('resolution_status', 'unavailable')}"
            f"/{work_state.get('resolution_reason', work_state.get('reason', 'unknown'))}"
        )
    else:
        if work_state.get("available"):
            lines.append(
                "work_state: "
                f"{work_state.get('hermes_owned_live', 0)} Hermes live, "
                f"{work_state.get('delegated_omx_live', 0)} delegated OMX live."
            )
        else:
            lines.append(f"work_state: {work_state.get('reason', 'unavailable')}")
    lines.append(
        "Decision: "
        f"{admission.get('status', 'unknown')} "
        f"({admission.get('reason', 'no_reason')})."
    )
    lines.append("Side effects: executor_spawned=false, linear_done_mutated=false.")
    return "\n".join(lines)


def handle_autopilot_command(
    raw_args: str,
    *,
    actor: Optional[str] = None,
    state_store: Optional[AutopilotStateStore] = None,
    work_state_store: Any = None,
    linear_client: Optional[LinearIssueClient] = None,
    executor_spawner: Optional[Callable[..., Any]] = None,
    now: Optional[datetime] = None,
) -> AutopilotResult:
    """Handle /autopilot without spawning executors or mutating Linear.

    ``executor_spawner`` is accepted only as a test seam/documented guard: this
    entrypoint must never call it in CH-385.
    """

    del executor_spawner  # CH-385 guard: parsing/classification only.
    store = state_store or AutopilotStateStore()
    try:
        command = parse_autopilot_args(raw_args)
    except AutopilotParseError as exc:
        return AutopilotResult(
            ok=False,
            command=None,
            fail_closed=True,
            message=f"Fail-closed: {exc}\nUsage: {AUTOPILOT_USAGE}",
            decision={
                "ok": False,
                "fail_closed": True,
                "reason": str(exc),
                "usage": AUTOPILOT_USAGE,
                "side_effects": {
                    "state_written": False,
                    "executor_spawned": False,
                    "linear_done_mutated": False,
                },
            },
        )

    state_written = False
    if command.action == ACTION_ENABLE:
        state = store.set_enabled(True, actor=actor, now=now)
        state_written = True
    elif command.action == ACTION_DISABLE:
        state = store.set_enabled(False, actor=actor, now=now)
        state_written = True
    else:
        state = store.status()

    linear: Optional[dict[str, Any]] = None
    if command.target_id:
        client = linear_client or EnvLinearIssueClient()
        linear = dict(classify_linear_target(client.fetch_issue(command.target_id), command.target_id))
        work_state = classify_work_state_target(command.target_id, work_state_store)
    else:
        work_state = summarize_work_state(work_state_store)

    admission = _admission_decision(command=command, state=state, linear=linear)
    decision: dict[str, Any] = {
        "ok": True,
        "fail_closed": False,
        "command": {
            "action": command.action,
            "target_id": command.target_id,
            "read_only": command.read_only,
        },
        "state": state,
        "linear": linear,
        "work_state": work_state,
        "admission": admission,
        "side_effects": {
            "state_written": state_written,
            "executor_spawned": False,
            "linear_done_mutated": False,
        },
        "generated_at": (now or _utcnow()).isoformat(),
    }
    return AutopilotResult(
        ok=True,
        command=command,
        message=_format_result_message(decision),
        decision=decision,
    )


def plugin_command_entrypoint(raw_args: str) -> str:
    """Plugin-compatible fail-closed slash-command entrypoint."""

    return handle_autopilot_command(raw_args).message
