"""Gateway-scoped Hermes work-state tracking for targeted owner ingress.

This module keeps a small persistent ledger of live Hermes work records so
owner-ingress packets can be resolved against explicit work state rather than
broad chat/session heuristics.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from hermes_constants import get_hermes_home


LIVE_STATES = frozenset({
    "created",
    "running",
    "blocked",
    "stale",
    "retry_needed",
    "handoff_needed",
    "failed",
})

WORK_SESSION_LIFECYCLE_STATES = frozenset({
    "active",
    "waiting_user",
    "stale",
    "nudged",
    "resolved",
    "stopped",
    "orphaned",
    "failed",
})
WORK_SESSION_WATCH_STATUSES = frozenset({"unwatched", "active", "inactive", "failed"})
WORK_SESSION_AUTO_WATCH_SOURCE = "hermes-work-session-registry"
WORK_SESSION_AUTO_WATCH_CLEANUP_CONDITION = "stop_or_resolved_or_owner_close"
DEFAULT_WORK_SESSION_STALE_MINUTES = 10
TRUSTED_WORK_SESSION_AUTO_WATCH_MARKERS = frozenset({
    "trusted_auto_watch",
    "hermes_auto_watch",
    "auto_watch_trusted",
})
WORK_SESSION_SEMANTIC_STATES = frozenset({
    "stale",
    "blocked_on_user",
    "permission_prompt",
    "tool_running",
    "completed_idle",
    "orphaned",
    "unknown",
})
WORK_SESSION_ACTIONABLE_SEMANTIC_STATES = frozenset({
    "stale",
    "blocked_on_user",
    "permission_prompt",
})

WORK_SESSION_DELIVER_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "refused"})
WORK_SESSION_DELIVER_PROMPT_MAX_CHARS = 1200

WAKE_STATES = frozenset({
    "blocked",
    "stale",
    "retry_needed",
    "handoff_needed",
    "failed",
})

USABLE_OUTCOMES = frozenset({
    "no_progress_theater",
    "red_only_partial_handoff",
    "blocked",
    "stale",
    "retry_needed",
    "handoff_needed",
    "runtime_contamination",
})

CLOSE_DISPOSITIONS = frozenset({
    "update",
    "close",
})

OMX_LANES = frozenset({"omx_exec", "plan", "ralplan", "ralph", "team"})
PLANNING_GATES = frozenset({"open", "closed"})
NEXT_EXECUTION_BRANCHES = frozenset({"none", "pending", "ralph", "team"})
CLOSE_AUTHORITIES = frozenset({"hermes", "human", "omx"})
OMX_GLOBAL_BOOLEAN_FLAGS = frozenset({
    "--madmax",
    "--high",
    "--xhigh",
    "--spark",
    "--madmax-spark",
    "--notify-temp",
    "--tmux",
    "--discord",
    "--slack",
    "--telegram",
    "--force",
    "--dry-run",
    "--keep-config",
    "--purge",
    "--verbose",
})
OMX_GLOBAL_FLAGS_WITH_VALUES = frozenset({"--custom", "--scope", "--skill-target"})
OMX_LANE_SUBCOMMANDS = {
    "exec": "omx_exec",
    "plan": "plan",
    "ralplan": "ralplan",
    "ralph": "ralph",
    "team": "team",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_path_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(Path(text).expanduser().resolve())
    except Exception:
        try:
            return str(Path(text).expanduser())
        except Exception:
            return text


def _parse_event_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
    return _utcnow()


def _compact_native_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {str(key): value for key, value in payload.items() if isinstance(key, str)}


def _normalize_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _normalize_positive_int(value: Any, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _is_truthy_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "trusted", "hermes"}


def _is_falsy_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    text = str(value or "").strip().lower()
    return text in {"0", "false", "no", "n", "off", "dead", "missing", "none"}


def _normalize_work_session_event_name(event: str) -> str:
    return str(event or "").strip().lower().replace("_", "-")


def _is_work_session_stop_event(normalized_event: str) -> bool:
    return normalized_event in {"stop", "sessionstop", "session-stopped", "session.stopped"}


def _is_work_session_resolved_event(normalized_event: str, payload: Dict[str, Any]) -> bool:
    lifecycle_state = (
        str(payload.get("lifecycle_state") or "").strip().lower().replace("_", "-")
    )
    close_disposition = (
        str(payload.get("close_disposition") or "").strip().lower().replace("_", "-")
    )
    return (
        normalized_event
        in {
            "resolved",
            "sessionresolved",
            "session-resolved",
            "session.resolved",
            "ownerclose",
            "owner-close",
            "owner.closed",
            "owner-closed",
        }
        or lifecycle_state == "resolved"
        or close_disposition == "close"
    )


def _is_work_session_action_required_event(
    normalized_event: str,
    payload: Dict[str, Any],
) -> bool:
    event_text = " ".join(
        str(payload.get(key) or "")
        for key in (
            "event",
            "native_event",
            "event_name",
            "hook_event_name",
            "alert_type",
            "notification_type",
            "reason",
        )
    ).strip().lower()
    return (
        normalized_event
        in {
            "actionrequired",
            "action-required",
            "action.required",
            "pane-stale",
            "pane.stale",
            "tmux-stale",
            "tmux.stale",
            "watch-stale",
            "watch.stale",
            "stale",
        }
        or _is_truthy_marker(payload.get("action_required"))
        or _is_truthy_marker(payload.get("requires_action"))
        or "action required" in event_text
        or "action-required" in event_text
        or "stale" in event_text
    )


def _default_clawhip_tmux_watch_registrar(watch_record: Dict[str, Any]) -> Dict[str, Any]:
    """Register an exact tmux-session watch with the clawhip daemon.

    This intentionally posts the same record shape that ``clawhip tmux list``
    renders instead of launching ``clawhip tmux watch`` as a long-lived wrapper
    process. That keeps Hermes from adding broad prefix monitors or orphaning a
    wrapper-owned monitor.
    """

    base_url = (
        os.environ.get("CLAWHIP_DAEMON_URL")
        or os.environ.get("CLAWHIP_BASE_URL")
        or "http://127.0.0.1:25294"
    ).rstrip("/")
    req = urlrequest.Request(
        f"{base_url}/api/tmux/register",
        data=json.dumps(watch_record).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=2.0) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if 200 <= resp.status < 300:
                return {"ok": True, "status": resp.status, "body": body}
            return {"ok": False, "status": resp.status, "body": body}
    except (OSError, urlerror.URLError, urlerror.HTTPError) as exc:
        return {"ok": False, "error": str(exc)}


def _default_clawhip_tmux_watch_cleanup(cleanup_record: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort deactivation boundary for Hermes-owned clawhip watches.

    clawhip may not expose a stable unregister API in every deployment. Treat
    cleanup as a daemon deactivation boundary: try a targeted DELETE first, then
    a clear/deactivate POST shape, and persist any failure for audit.
    """

    base_url = (
        os.environ.get("CLAWHIP_DAEMON_URL")
        or os.environ.get("CLAWHIP_BASE_URL")
        or "http://127.0.0.1:25294"
    ).rstrip("/")
    attempts = [
        ("DELETE", f"{base_url}/api/tmux/register"),
        ("POST", f"{base_url}/api/tmux/clear"),
    ]
    errors: List[str] = []
    body = json.dumps(cleanup_record).encode("utf-8")
    for method, url in attempts:
        req = urlrequest.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlrequest.urlopen(req, timeout=2.0) as resp:
                response_body = resp.read().decode("utf-8", errors="replace")
                if 200 <= resp.status < 300:
                    return {
                        "ok": True,
                        "status": resp.status,
                        "method": method,
                        "body": response_body,
                    }
                errors.append(f"{method} {resp.status}: {response_body}")
        except (OSError, urlerror.URLError, urlerror.HTTPError) as exc:
            errors.append(f"{method}: {exc}")
    return {"ok": False, "error": "; ".join(errors) or "clawhip_tmux_watch_cleanup_failed"}


def _default_clawhip_deliver_executor(deliver_request: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one bounded ``clawhip deliver`` request.

    The policy builds a fully bounded request and unit tests inject a fake
    executor. This default is intentionally a thin one-shot CLI adapter, not a
    monitor or retry loop.
    """

    command = [
        "clawhip",
        "deliver",
        "--provider",
        str(deliver_request.get("provider") or ""),
        "--session-id",
        str(deliver_request.get("provider_session_id") or ""),
        "--prompt",
        str(deliver_request.get("prompt") or ""),
        "--json",
    ]
    tmux_pane = _normalize_text(deliver_request.get("tmux_pane"))
    if tmux_pane:
        command.extend(["--tmux-pane", tmux_pane])
    tmux_session = _normalize_text(deliver_request.get("tmux_session"))
    if tmux_session:
        command.extend(["--tmux-session", tmux_session])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=(
                deliver_request.get("worktree_path")
                or deliver_request.get("repo_path")
                or None
            ),
        )
    except FileNotFoundError:
        return {"ok": False, "error": "clawhip_unavailable"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "clawhip_deliver_timeout"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    stdout = (completed.stdout or "").strip()
    parsed: Dict[str, Any] = {}
    if stdout:
        try:
            value = json.loads(stdout)
            if isinstance(value, dict):
                parsed = value
        except Exception:
            parsed = {"stdout": stdout}
    parsed.setdefault("ok", completed.returncode == 0)
    parsed.setdefault("returncode", completed.returncode)
    if completed.stderr:
        parsed.setdefault("stderr", completed.stderr.strip())
    return parsed


def normalize_usable_outcome(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text if text in USABLE_OUTCOMES else None


def normalize_close_disposition(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text if text in CLOSE_DISPOSITIONS else None


def map_usable_outcome_to_owner_state(usable_outcome: str) -> Optional[str]:
    if usable_outcome in {"blocked", "stale", "retry_needed", "handoff_needed"}:
        return usable_outcome
    if usable_outcome in {"no_progress_theater", "red_only_partial_handoff"}:
        return "handoff_needed"
    if usable_outcome == "runtime_contamination":
        return "failed"
    return None


def bound_next_action(next_action: Optional[str], *, fallback: str) -> str:
    text = " ".join(str(next_action or "").split())
    if not text:
        text = fallback
    if len(text) <= 200:
        return text
    return text[:197].rstrip() + "..."


def delegated_process_exit_closeout(exit_code: Optional[int]) -> Dict[str, str]:
    proof = f"background_process_exit:{exit_code}"
    if exit_code == 0:
        return {
            "state": "handoff_needed",
            "usable_outcome": "no_progress_theater",
            "close_disposition": "close",
            "next_action": "Inspect the OMX run diff before claiming progress",
            "proof": proof,
        }
    return {
        "state": "failed",
        "usable_outcome": "runtime_contamination",
        "close_disposition": "close",
        "next_action": "Inspect runtime contamination before any retry or handoff",
        "proof": proof,
    }


def record_has_closed_usable_outcome(record: Any) -> bool:
    return bool(
        getattr(record, "usable_outcome", None)
        and getattr(record, "close_disposition", None) == "close"
    )


def _normalize_lane_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.replace("-", "_").replace(" ", "_")
    aliases = {
        "exec": "omx_exec",
        "omxexec": "omx_exec",
        "omx_exec": "omx_exec",
        "plan": "plan",
        "ralplan": "ralplan",
        "ralph": "ralph",
        "team": "team",
    }
    normalized = aliases.get(text)
    if normalized in OMX_LANES:
        return normalized
    return None


def _normalize_planning_gate(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.replace("-", "_").replace(" ", "_")
    return text if text in PLANNING_GATES else None


def _normalize_next_execution_branch(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.replace("-", "_").replace(" ", "_")
    if text in {"none_yet", "not_set"}:
        text = "none"
    return text if text in NEXT_EXECUTION_BRANCHES else None


def _normalize_close_authority(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = text.replace("-", "_").replace(" ", "_")
    return text if text in CLOSE_AUTHORITIES else None


def infer_omx_lane_from_command(command: str) -> Optional[str]:
    normalized = str(command or "").lower()
    if not normalized.strip():
        return None
    if "$ralplan" in normalized:
        return "ralplan"
    if "$ralph" in normalized:
        return "ralph"
    if "$team" in normalized:
        return "team"

    shellish = re.sub(r"[\n\r\t\"'`|&;()\[\]{}<>]", " ", normalized)
    tokens = [token for token in shellish.split() if token]
    if not tokens:
        return None

    for idx, token in enumerate(tokens):
        if token != "omx":
            continue
        cursor = idx + 1
        while cursor < len(tokens):
            current = tokens[cursor]
            lane = OMX_LANE_SUBCOMMANDS.get(current)
            if lane:
                return lane
            if current in OMX_GLOBAL_BOOLEAN_FLAGS:
                cursor += 1
                continue
            if current in OMX_GLOBAL_FLAGS_WITH_VALUES:
                cursor += 2
                continue
            if any(current.startswith(f"{flag}=") for flag in OMX_GLOBAL_FLAGS_WITH_VALUES):
                cursor += 1
                continue
            if current == "-w":
                if cursor + 1 < len(tokens) and not tokens[cursor + 1].startswith("-"):
                    cursor += 2
                else:
                    cursor += 1
                continue
            if current.startswith("-w="):
                cursor += 1
                continue
            if current.startswith("-"):
                cursor += 1
                continue
            break
    return None


def resolve_omx_lane_truth(
    *,
    current_lane: Optional[str] = None,
    planning_gate: Optional[str] = None,
    next_execution_branch: Optional[str] = None,
    close_authority: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    lane = _normalize_lane_value(current_lane)
    gate = _normalize_planning_gate(planning_gate)
    branch = _normalize_next_execution_branch(next_execution_branch)
    authority = _normalize_close_authority(close_authority)

    if current_lane and lane is None:
        raise ValueError("invalid_current_lane")
    if planning_gate and gate is None:
        raise ValueError("invalid_planning_gate")
    if next_execution_branch and branch is None:
        raise ValueError("invalid_next_execution_branch")
    if close_authority and authority is None:
        raise ValueError("invalid_close_authority")

    if lane is None:
        if gate or branch or authority:
            raise ValueError("lane_fields_require_current_lane")
        return {
            "current_lane": None,
            "planning_gate": None,
            "next_execution_branch": None,
            "close_authority": None,
        }

    authority = authority or "hermes"

    if lane == "omx_exec":
        expected_gate = "closed"
        expected_branch = "none"
    elif lane == "plan":
        expected_gate = "open"
        expected_branch = "none"
    elif lane == "ralplan":
        expected_gate = gate or "open"
        if expected_gate == "open":
            expected_branch = "none"
        else:
            expected_branch = branch or "pending"
            if expected_branch not in {"pending", "ralph", "team"}:
                raise ValueError("invalid_ralplan_branch")
    elif lane == "ralph":
        expected_gate = "closed"
        expected_branch = "ralph"
    else:
        expected_gate = "closed"
        expected_branch = "team"

    if gate and gate != expected_gate:
        raise ValueError("invalid_planning_gate_for_lane")
    if branch and branch != expected_branch:
        raise ValueError("invalid_next_execution_branch_for_lane")

    return {
        "current_lane": lane,
        "planning_gate": expected_gate,
        "next_execution_branch": expected_branch,
        "close_authority": authority,
    }


def _apply_omx_lane_truth(record: "WorkRecord") -> None:
    if not (
        record.owner == "hermes"
        and record.executor == "omx"
        and record.mode == "delegated"
    ):
        return
    if not any(
        getattr(record, field, None)
        for field in ("current_lane", "planning_gate", "next_execution_branch", "close_authority")
    ):
        return
    lane_truth = resolve_omx_lane_truth(
        current_lane=getattr(record, "current_lane", None),
        planning_gate=getattr(record, "planning_gate", None),
        next_execution_branch=getattr(record, "next_execution_branch", None),
        close_authority=getattr(record, "close_authority", None),
    )
    record.current_lane = lane_truth["current_lane"]
    record.planning_gate = lane_truth["planning_gate"]
    record.next_execution_branch = lane_truth["next_execution_branch"]
    record.close_authority = lane_truth["close_authority"]


@dataclass(frozen=True)
class WorkSessionSemanticClassification:
    state: str
    reason: str
    required_owner_action: str
    should_alert: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "required_owner_action": self.required_owner_action,
            "should_alert": self.should_alert,
        }


def _iter_snapshot_text_values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        values: List[str] = []
        for item in value:
            values.extend(_iter_snapshot_text_values(item))
        return values
    if isinstance(value, dict):
        values = []
        for key in (
            "text",
            "content",
            "screen",
            "visible_text",
            "visibleText",
            "output",
            "tail",
            "last_output",
            "lastOutput",
            "last_lines",
            "lastLines",
            "line",
            "message",
        ):
            if key in value:
                values.extend(_iter_snapshot_text_values(value.get(key)))
        return values
    return [str(value)]


def _work_session_snapshot_text(payload: Dict[str, Any]) -> str:
    values: List[str] = []
    for key in (
        "pane_snapshot",
        "paneSnapshot",
        "snapshot",
        "tmux_snapshot",
        "tmuxSnapshot",
        "pane",
        "screen",
        "visible_text",
        "visibleText",
        "output",
        "tail",
        "last_output",
        "lastOutput",
        "matched_output",
        "matchedOutput",
        "message",
        "reason",
    ):
        if key in payload:
            values.extend(_iter_snapshot_text_values(payload.get(key)))
    return "\n".join(text for text in values if str(text).strip())


def _payload_float(payload: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        if key not in payload:
            continue
        try:
            return float(payload.get(key))
        except (TypeError, ValueError):
            continue
    return None


def _payload_snapshot_bool(payload: Dict[str, Any], *keys: str) -> Optional[bool]:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if _is_truthy_marker(value):
            return True
        if _is_falsy_marker(value):
            return False
    return None


def _bounded_reason(reason: str) -> str:
    text = re.sub(r"\s+", " ", str(reason or "").strip())
    return text[:180] or "unknown"


def _classification_from_explicit_state(
    payload: Dict[str, Any],
) -> Optional[WorkSessionSemanticClassification]:
    explicit = _normalize_text(
        payload.get("classifier_state")
        or payload.get("semantic_state")
        or payload.get("semanticState")
    )
    if not explicit:
        return None
    state = explicit.strip().lower().replace("-", "_")
    if state not in WORK_SESSION_SEMANTIC_STATES:
        return None
    reason = _bounded_reason(
        payload.get("classifier_reason")
        or payload.get("semantic_reason")
        or payload.get("reason")
        or f"explicit classifier state {state}"
    )
    action = _bounded_reason(
        payload.get("required_owner_action")
        or payload.get("owner_action")
        or _default_required_owner_action(state, reason)
    )
    return WorkSessionSemanticClassification(
        state=state,
        reason=reason,
        required_owner_action=action,
        should_alert=state in WORK_SESSION_ACTIONABLE_SEMANTIC_STATES,
    )


def _default_required_owner_action(state: str, reason: str) -> str:
    if state == "permission_prompt":
        return "Approve, deny, or provide the requested permission in the owning work session"
    if state == "blocked_on_user":
        return "Answer the user-facing prompt or provide the missing decision"
    if state == "stale":
        return "Inspect the work session and decide whether to resume, redirect, or close it"
    if state == "tool_running":
        return "Wait for the active tool or command to finish before waking the owner"
    if state == "completed_idle":
        return "Close the completed idle work session without retrying the pane"
    if state == "orphaned":
        return "Clean up the orphaned work-session watch without waking the owner"
    return f"Inspect the work session manually; classifier reason: {reason}"


def classify_work_session_action_required(
    payload: Dict[str, Any],
    *,
    record: Optional["WorkSessionRecord"] = None,
    normalized_event: Optional[str] = None,
) -> WorkSessionSemanticClassification:
    """Classify a clawhip pane snapshot before deciding whether to wake Hermes.

    The classifier is deliberately generic-input/fixed-output: clawhip can send
    arbitrary pane snapshot metadata, but Hermes must reduce it to bounded
    states and a concrete owner action before any action-required alert is
    emitted.
    """

    if not isinstance(payload, dict):
        return WorkSessionSemanticClassification(
            state="unknown",
            reason="invalid action-required payload",
            required_owner_action=_default_required_owner_action(
                "unknown",
                "invalid action-required payload",
            ),
            should_alert=False,
        )

    explicit = _classification_from_explicit_state(payload)
    if explicit is not None:
        return explicit

    normalized_event = normalized_event or _normalize_work_session_event_name(
        str(
            payload.get("event")
            or payload.get("native_event")
            or payload.get("event_name")
            or payload.get("hook_event_name")
            or ""
        )
    )
    snapshot_text = _work_session_snapshot_text(payload)
    lowered = snapshot_text.lower()

    pane_alive = _payload_snapshot_bool(
        payload,
        "pane_alive",
        "paneAlive",
        "process_alive",
        "processAlive",
        "has_process",
        "hasProcess",
    )
    pane_missing = (
        _is_truthy_marker(payload.get("pane_missing"))
        or _is_truthy_marker(payload.get("paneMissing"))
        or _is_truthy_marker(payload.get("orphaned"))
        or "can't find pane" in lowered
        or "cant find pane" in lowered
        or "no such pane" in lowered
        or "pane not found" in lowered
        or "session not found" in lowered
    )
    if pane_missing or pane_alive is False or (record is not None and not record.tmux_pane):
        reason = _bounded_reason(payload.get("reason") or "tmux pane is missing or no longer alive")
        return WorkSessionSemanticClassification(
            state="orphaned",
            reason=reason,
            required_owner_action=_default_required_owner_action("orphaned", reason),
            should_alert=False,
        )

    if any(
        marker in lowered
        for marker in (
            "requires approval",
            "waiting for approval",
            "approval required",
            "permission required",
            "requires permission",
            "allow this command",
            "approve or deny",
            "/approve",
            "/deny",
            "sudo password",
            "password for",
        )
    ):
        reason = _bounded_reason(
            payload.get("reason")
            or "pane is waiting on a permission or approval prompt"
        )
        return WorkSessionSemanticClassification(
            state="permission_prompt",
            reason=reason,
            required_owner_action=_default_required_owner_action("permission_prompt", reason),
            should_alert=True,
        )

    if any(
        marker in lowered
        for marker in (
            "waiting for user",
            "needs user input",
            "need your input",
            "please respond",
            "please choose",
            "select an option",
            "press enter",
            "confirm to continue",
            "should i proceed",
            "do you want me to proceed",
            "reply with",
        )
    ):
        reason = _bounded_reason(payload.get("reason") or "pane is blocked on a user decision")
        return WorkSessionSemanticClassification(
            state="blocked_on_user",
            reason=reason,
            required_owner_action=_default_required_owner_action("blocked_on_user", reason),
            should_alert=True,
        )

    command_running = _payload_snapshot_bool(
        payload,
        "command_running",
        "commandRunning",
        "tool_running",
        "toolRunning",
        "process_running",
        "processRunning",
        "active_process",
        "activeProcess",
    )
    if command_running is True or any(
        marker in lowered
        for marker in (
            "tool running",
            "command still running",
            "tests are running",
            "running tests",
            "npm run",
            "pytest",
            "building",
            "compiling",
            "installing",
            "downloading",
            "waiting for command to finish",
        )
    ):
        reason = _bounded_reason(payload.get("reason") or "pane has an active tool or command")
        return WorkSessionSemanticClassification(
            state="tool_running",
            reason=reason,
            required_owner_action=_default_required_owner_action("tool_running", reason),
            should_alert=False,
        )

    completed_marker = (
        _is_truthy_marker(payload.get("completed"))
        or _is_truthy_marker(payload.get("completed_idle"))
        or _is_truthy_marker(payload.get("completedIdle"))
        or any(
            marker in lowered
            for marker in (
                "process exited with code 0",
                "exit code 0",
                "exited 0",
                "tests passed",
                "all tests passed",
                "done.",
                "task complete",
                "completed successfully",
                "nothing to do",
                "working tree clean",
            )
        )
    )
    if completed_marker and command_running is not True:
        reason = _bounded_reason(payload.get("reason") or "pane appears completed and idle")
        return WorkSessionSemanticClassification(
            state="completed_idle",
            reason=reason,
            required_owner_action=_default_required_owner_action("completed_idle", reason),
            should_alert=False,
        )

    idle_seconds = _payload_float(payload, "idle_seconds", "idleSeconds", "idle_secs", "idleSecs")
    stale_seconds = _payload_float(payload, "stale_seconds", "staleSeconds")
    stale_minutes = _payload_float(
        payload,
        "stale_minutes",
        "staleMinutes",
        "watch_stale_minutes",
        "watchStaleMinutes",
    )
    threshold_seconds = stale_seconds or ((stale_minutes or DEFAULT_WORK_SESSION_STALE_MINUTES) * 60)
    stale_event = _is_work_session_action_required_event(normalized_event, payload)
    if (
        _is_truthy_marker(payload.get("stale"))
        or (idle_seconds is not None and idle_seconds >= threshold_seconds)
        or ("no output" in lowered and stale_event)
        or (stale_event and not snapshot_text.strip())
    ):
        reason = _bounded_reason(
            payload.get("reason")
            or (
                f"pane idle for {int(idle_seconds)}s past {int(threshold_seconds)}s threshold"
                if idle_seconds is not None
                else "action-required stale event did not include a more specific prompt"
            )
        )
        return WorkSessionSemanticClassification(
            state="stale",
            reason=reason,
            required_owner_action=_default_required_owner_action("stale", reason),
            should_alert=True,
        )

    reason = _bounded_reason(
        payload.get("reason")
        or "pane snapshot did not match a bounded actionable state"
    )
    return WorkSessionSemanticClassification(
        state="unknown",
        reason=reason,
        required_owner_action=_default_required_owner_action("unknown", reason),
        should_alert=False,
    )


@dataclass
class WorkRecord:
    work_id: str
    title: str
    objective: str
    owner: str
    executor: str
    mode: str
    owner_session_id: str
    state: str
    started_at: datetime
    last_progress_at: datetime
    next_action: str
    executor_session_id: Optional[str] = None
    tmux_session: Optional[str] = None
    repo_path: Optional[str] = None
    worktree_path: Optional[str] = None
    escalation_target: Optional[str] = None
    proof: Optional[str] = None
    usable_outcome: Optional[str] = None
    close_disposition: Optional[str] = None
    current_lane: Optional[str] = None
    planning_gate: Optional[str] = None
    next_execution_branch: Optional[str] = None
    close_authority: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["started_at"] = self.started_at.isoformat()
        data["last_progress_at"] = self.last_progress_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkRecord":
        return cls(
            work_id=str(data["work_id"]),
            title=str(data.get("title", "")),
            objective=str(data.get("objective", "")),
            owner=str(data.get("owner", "")),
            executor=str(data.get("executor", "")),
            mode=str(data.get("mode", "")),
            owner_session_id=str(data.get("owner_session_id", "")),
            state=str(data.get("state", "")),
            started_at=datetime.fromisoformat(data["started_at"]),
            last_progress_at=datetime.fromisoformat(data["last_progress_at"]),
            next_action=str(data.get("next_action", "")),
            executor_session_id=data.get("executor_session_id"),
            tmux_session=data.get("tmux_session"),
            repo_path=data.get("repo_path"),
            worktree_path=data.get("worktree_path"),
            escalation_target=data.get("escalation_target"),
            proof=data.get("proof"),
            usable_outcome=data.get("usable_outcome"),
            close_disposition=data.get("close_disposition"),
            current_lane=data.get("current_lane"),
            planning_gate=data.get("planning_gate"),
            next_execution_branch=data.get("next_execution_branch"),
            close_authority=data.get("close_authority"),
        )


@dataclass
class WorkSessionRecord:
    provider: str
    provider_session_id: str
    linear_card_id: str
    lane_id: str
    lifecycle_state: str
    started_at: datetime
    last_event_at: datetime
    last_event: str
    repo_path: Optional[str] = None
    worktree_path: Optional[str] = None
    repo_name: Optional[str] = None
    project: Optional[str] = None
    branch: Optional[str] = None
    directory: Optional[str] = None
    tmux_session: Optional[str] = None
    tmux_pane: Optional[str] = None
    ingress_route: Optional[str] = None
    first_delivery_id: Optional[str] = None
    latest_delivery_id: Optional[str] = None
    watch_status: str = "unwatched"
    watch_registration_source: Optional[str] = None
    watch_owner: Optional[str] = None
    watch_repo_path: Optional[str] = None
    watch_worktree_path: Optional[str] = None
    watch_linear_card_id: Optional[str] = None
    watch_stale_minutes: Optional[int] = None
    watch_cleanup_condition: Optional[str] = None
    watch_registered_at: Optional[datetime] = None
    watch_record: Optional[Dict[str, Any]] = None
    watch_error: Optional[str] = None
    watch_cleanup_error: Optional[str] = None
    stopped_at: Optional[datetime] = None
    semantic_state: Optional[str] = None
    semantic_reason: Optional[str] = None
    semantic_required_owner_action: Optional[str] = None
    semantic_alerted_at: Optional[datetime] = None
    deliver_status: Optional[str] = None
    deliver_attempts: int = 0
    deliver_started_at: Optional[datetime] = None
    deliver_completed_at: Optional[datetime] = None
    deliver_prompt: Optional[str] = None
    deliver_evidence: Optional[Dict[str, Any]] = None
    deliver_error: Optional[str] = None
    native_event_metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "linear_card_id": self.linear_card_id,
            "lane_id": self.lane_id,
            "lifecycle_state": self.lifecycle_state,
            "started_at": self.started_at.isoformat(),
            "last_event_at": self.last_event_at.isoformat(),
            "last_event": self.last_event,
            "repo_path": self.repo_path,
            "worktree_path": self.worktree_path,
            "repo_name": self.repo_name,
            "project": self.project,
            "branch": self.branch,
            "directory": self.directory,
            "tmux_session": self.tmux_session,
            "tmux_pane": self.tmux_pane,
            "ingress_route": self.ingress_route,
            "first_delivery_id": self.first_delivery_id,
            "latest_delivery_id": self.latest_delivery_id,
            "watch_status": self.watch_status,
            "watch_registration_source": self.watch_registration_source,
            "watch_owner": self.watch_owner,
            "watch_repo_path": self.watch_repo_path,
            "watch_worktree_path": self.watch_worktree_path,
            "watch_linear_card_id": self.watch_linear_card_id,
            "watch_stale_minutes": self.watch_stale_minutes,
            "watch_cleanup_condition": self.watch_cleanup_condition,
            "watch_registered_at": self.watch_registered_at.isoformat() if self.watch_registered_at else None,
            "watch_record": self.watch_record or {},
            "watch_error": self.watch_error,
            "watch_cleanup_error": self.watch_cleanup_error,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "semantic_state": self.semantic_state,
            "semantic_reason": self.semantic_reason,
            "semantic_required_owner_action": self.semantic_required_owner_action,
            "semantic_alerted_at": self.semantic_alerted_at.isoformat() if self.semantic_alerted_at else None,
            "deliver_status": self.deliver_status,
            "deliver_attempts": int(self.deliver_attempts or 0),
            "deliver_started_at": self.deliver_started_at.isoformat() if self.deliver_started_at else None,
            "deliver_completed_at": self.deliver_completed_at.isoformat() if self.deliver_completed_at else None,
            "deliver_prompt": self.deliver_prompt,
            "deliver_evidence": self.deliver_evidence or {},
            "deliver_error": self.deliver_error,
            "native_event_metadata": self.native_event_metadata or {},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkSessionRecord":
        stopped_at = data.get("stopped_at")
        watch_registered_at = data.get("watch_registered_at")
        semantic_alerted_at = data.get("semantic_alerted_at")
        deliver_started_at = data.get("deliver_started_at")
        deliver_completed_at = data.get("deliver_completed_at")
        return cls(
            provider=str(data["provider"]),
            provider_session_id=str(data["provider_session_id"]),
            linear_card_id=str(data["linear_card_id"]),
            lane_id=str(data["lane_id"]),
            lifecycle_state=str(data.get("lifecycle_state", "active")),
            started_at=datetime.fromisoformat(data["started_at"]),
            last_event_at=datetime.fromisoformat(data["last_event_at"]),
            last_event=str(data.get("last_event", "")),
            repo_path=data.get("repo_path"),
            worktree_path=data.get("worktree_path"),
            repo_name=data.get("repo_name"),
            project=data.get("project"),
            branch=data.get("branch"),
            directory=data.get("directory"),
            tmux_session=data.get("tmux_session"),
            tmux_pane=data.get("tmux_pane"),
            ingress_route=data.get("ingress_route"),
            first_delivery_id=data.get("first_delivery_id"),
            latest_delivery_id=data.get("latest_delivery_id"),
            watch_status=str(data.get("watch_status") or "unwatched"),
            watch_registration_source=data.get("watch_registration_source"),
            watch_owner=data.get("watch_owner"),
            watch_repo_path=data.get("watch_repo_path"),
            watch_worktree_path=data.get("watch_worktree_path"),
            watch_linear_card_id=data.get("watch_linear_card_id"),
            watch_stale_minutes=(
                _normalize_positive_int(
                    data.get("watch_stale_minutes"),
                    fallback=DEFAULT_WORK_SESSION_STALE_MINUTES,
                )
                if data.get("watch_stale_minutes") is not None
                else None
            ),
            watch_cleanup_condition=data.get("watch_cleanup_condition"),
            watch_registered_at=(
                datetime.fromisoformat(watch_registered_at) if watch_registered_at else None
            ),
            watch_record=data.get("watch_record") or {},
            watch_error=data.get("watch_error"),
            watch_cleanup_error=data.get("watch_cleanup_error"),
            stopped_at=datetime.fromisoformat(stopped_at) if stopped_at else None,
            semantic_state=data.get("semantic_state"),
            semantic_reason=data.get("semantic_reason"),
            semantic_required_owner_action=data.get("semantic_required_owner_action"),
            semantic_alerted_at=(
                datetime.fromisoformat(semantic_alerted_at) if semantic_alerted_at else None
            ),
            deliver_status=data.get("deliver_status"),
            deliver_attempts=(
                _normalize_positive_int(data.get("deliver_attempts"), fallback=0)
                if data.get("deliver_attempts") is not None
                else 0
            ),
            deliver_started_at=(
                datetime.fromisoformat(deliver_started_at) if deliver_started_at else None
            ),
            deliver_completed_at=(
                datetime.fromisoformat(deliver_completed_at) if deliver_completed_at else None
            ),
            deliver_prompt=data.get("deliver_prompt"),
            deliver_evidence=data.get("deliver_evidence") or {},
            deliver_error=data.get("deliver_error"),
            native_event_metadata=data.get("native_event_metadata") or {},
        )


class WorkSessionRegistry:
    """Hermes-owned registry for provider-native clawhip work sessions.

    clawhip stays the generic native-event router. This registry stores Hermes
    work-session linkage and lifecycle state from additive native-event metadata.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        tmux_watch_registrar: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        tmux_watch_cleanup: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        deliver_executor: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        work_state_store: Optional[Any] = None,
    ):
        self.path = Path(path) if path is not None else get_hermes_home() / "gateway_work_sessions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tmux_watch_registrar = tmux_watch_registrar or _default_clawhip_tmux_watch_registrar
        self._tmux_watch_cleanup = tmux_watch_cleanup or _default_clawhip_tmux_watch_cleanup
        self._deliver_executor = deliver_executor or _default_clawhip_deliver_executor
        self._work_state_store = work_state_store
        self._lock = threading.RLock()
        self._records: List[WorkSessionRecord] = []
        self._loaded = False
        self._last_mtime_ns: Optional[int] = None

    def _current_mtime_ns(self) -> Optional[int]:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _ensure_loaded(self) -> None:
        current_mtime_ns = self._current_mtime_ns()
        if self._loaded and current_mtime_ns == self._last_mtime_ns:
            return
        if current_mtime_ns is None:
            self._records = []
            self._loaded = True
            self._last_mtime_ns = None
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            payload = []
        if isinstance(payload, dict):
            payload = payload.get("sessions", [])
        self._records = [
            WorkSessionRecord.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]
        self._loaded = True
        self._last_mtime_ns = current_mtime_ns

    def _save_locked(self) -> None:
        self.path.write_text(
            json.dumps(
                [record.to_dict() for record in self._records],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._last_mtime_ns = self._current_mtime_ns()

    def list_sessions(self) -> List[WorkSessionRecord]:
        with self._lock:
            self._ensure_loaded()
            return [WorkSessionRecord.from_dict(record.to_dict()) for record in self._records]

    def get_session(self, provider: str, provider_session_id: str) -> Optional[WorkSessionRecord]:
        provider = str(provider or "").strip()
        provider_session_id = str(provider_session_id or "").strip()
        with self._lock:
            self._ensure_loaded()
            for record in self._records:
                if record.provider == provider and record.provider_session_id == provider_session_id:
                    return WorkSessionRecord.from_dict(record.to_dict())
        return None

    def _build_watch_record(
        self,
        record: WorkSessionRecord,
        payload: Dict[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        stale_minutes = _normalize_positive_int(
            payload.get("watch_stale_minutes") or payload.get("stale_minutes"),
            fallback=DEFAULT_WORK_SESSION_STALE_MINUTES,
        )
        keywords = [record.linear_card_id, record.lane_id]
        audit_metadata = {
            "source": WORK_SESSION_AUTO_WATCH_SOURCE,
            "owner": "hermes",
            "repo_path": record.repo_path,
            "worktree_path": record.worktree_path,
            "linear_card_id": record.linear_card_id,
            "lane_id": record.lane_id,
            "cleanup_condition": WORK_SESSION_AUTO_WATCH_CLEANUP_CONDITION,
            "tmux_pane": record.tmux_pane,
        }
        return {
            "session": record.tmux_session,
            "channel": _normalize_text(
                payload.get("watch_channel") or payload.get("channel")
            ),
            "mention": _normalize_text(
                payload.get("watch_mention") or payload.get("mention")
            ),
            "routing": dict(audit_metadata),
            "metadata": dict(audit_metadata),
            "keywords": keywords,
            "keyword_window_secs": 120,
            "stale_minutes": stale_minutes,
            "format": payload.get("watch_format") or "inline",
            "registered_at": now.isoformat(),
            "source": WORK_SESSION_AUTO_WATCH_SOURCE,
            "owner": "hermes",
            "registration_source": WORK_SESSION_AUTO_WATCH_SOURCE,
            "parent_process": None,
            "active_wrapper_monitor": False,
        }

    def _payload_requests_trusted_auto_watch(self, payload: Dict[str, Any]) -> bool:
        if _normalize_text(payload.get("owner")) != "hermes":
            return False
        return any(
            _is_truthy_marker(payload.get(marker))
            for marker in TRUSTED_WORK_SESSION_AUTO_WATCH_MARKERS
        )

    def _verified_hermes_work_record_for_watch(
        self,
        record: WorkSessionRecord,
        payload: Dict[str, Any],
    ) -> Optional[WorkRecord]:
        work_state_store = self._work_state_store
        if work_state_store is None:
            return None

        work_id = _normalize_text(payload.get("work_id") or payload.get("workId"))
        owner_session_id = _normalize_text(
            payload.get("owner_session_id") or payload.get("ownerSessionId")
        )
        executor_session_id = _normalize_text(
            payload.get("executor_session_id")
            or payload.get("executorSessionId")
            or payload.get("executor_session")
        )
        try:
            resolution = work_state_store.resolve_delegated_signal_candidate(
                work_id=work_id,
                owner_session_id=owner_session_id,
                executor_session_id=executor_session_id,
                tmux_session=record.tmux_session,
                repo_path=record.repo_path,
                worktree_path=record.worktree_path,
                live_only=True,
            )
        except Exception:
            return None
        if resolution.get("status") != "single_match":
            return None
        matched = resolution.get("record")
        if not isinstance(matched, WorkRecord):
            return None
        if not (
            matched.owner == "hermes"
            and matched.executor == "omx"
            and matched.mode == "delegated"
        ):
            return None
        return matched

    def _maybe_auto_register_tmux_watch_locked(
        self,
        record: WorkSessionRecord,
        payload: Dict[str, Any],
        now: datetime,
    ) -> None:
        if record.watch_status == "active":
            return
        if record.lifecycle_state in {"stopped", "resolved", "orphaned", "failed"}:
            return

        if not self._payload_requests_trusted_auto_watch(payload):
            return
        if not (
            _normalize_text(record.linear_card_id)
            and _normalize_text(record.lane_id)
            and _normalize_text(record.tmux_session)
            and _normalize_text(record.tmux_pane)
            and _normalize_text(record.repo_path)
            and _normalize_text(record.worktree_path)
        ):
            return
        if self._verified_hermes_work_record_for_watch(record, payload) is None:
            return

        watch_record = self._build_watch_record(record, payload, now)
        try:
            result = self._tmux_watch_registrar(watch_record)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        record.watch_status = "active" if ok else "failed"
        record.watch_registration_source = WORK_SESSION_AUTO_WATCH_SOURCE
        record.watch_owner = "hermes"
        record.watch_repo_path = record.repo_path
        record.watch_worktree_path = record.worktree_path
        record.watch_linear_card_id = record.linear_card_id
        record.watch_stale_minutes = int(watch_record["stale_minutes"])
        record.watch_cleanup_condition = WORK_SESSION_AUTO_WATCH_CLEANUP_CONDITION
        record.watch_registered_at = now
        record.watch_record = watch_record
        record.watch_error = None if ok else (
            str(result.get("error") or result.get("body") or result)
            if isinstance(result, dict)
            else "clawhip_tmux_watch_registration_failed"
        )

    def _cleanup_tmux_watch_locked(
        self,
        record: WorkSessionRecord,
        event_at: datetime,
        *,
        lifecycle_state: str,
    ) -> None:
        record.lifecycle_state = lifecycle_state
        record.stopped_at = event_at
        if record.watch_status == "active":
            cleanup_record = {
                "session": record.tmux_session,
                "routing": {
                    "source": WORK_SESSION_AUTO_WATCH_SOURCE,
                    "owner": record.watch_owner or "hermes",
                    "repo_path": record.watch_repo_path or record.repo_path,
                    "worktree_path": record.watch_worktree_path or record.worktree_path,
                    "linear_card_id": record.watch_linear_card_id or record.linear_card_id,
                    "lane_id": record.lane_id,
                    "cleanup_condition": record.watch_cleanup_condition
                    or WORK_SESSION_AUTO_WATCH_CLEANUP_CONDITION,
                    "tmux_pane": record.tmux_pane,
                    "lifecycle_state": lifecycle_state,
                },
                "metadata": {
                    "source": WORK_SESSION_AUTO_WATCH_SOURCE,
                    "owner": record.watch_owner or "hermes",
                    "repo_path": record.watch_repo_path or record.repo_path,
                    "worktree_path": record.watch_worktree_path or record.worktree_path,
                    "linear_card_id": record.watch_linear_card_id or record.linear_card_id,
                    "lane_id": record.lane_id,
                    "cleanup_condition": record.watch_cleanup_condition
                    or WORK_SESSION_AUTO_WATCH_CLEANUP_CONDITION,
                    "tmux_pane": record.tmux_pane,
                    "lifecycle_state": lifecycle_state,
                },
                "registration_source": record.watch_registration_source
                or WORK_SESSION_AUTO_WATCH_SOURCE,
                "cleanup_condition": record.watch_cleanup_condition
                or WORK_SESSION_AUTO_WATCH_CLEANUP_CONDITION,
                "deactivated_at": event_at.isoformat(),
            }
            try:
                result = self._tmux_watch_cleanup(cleanup_record)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            ok = bool(result.get("ok")) if isinstance(result, dict) else False
            record.watch_cleanup_error = None if ok else (
                str(result.get("error") or result.get("body") or result)
                if isinstance(result, dict)
                else "clawhip_tmux_watch_cleanup_failed"
            )
            record.watch_status = "inactive"

    @staticmethod
    def _semantic_reason_slug(reason: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(reason or "").lower()).strip("-")
        return slug[:60] or "unspecified"

    def _build_semantic_alert_payload(
        self,
        record: WorkSessionRecord,
        work_record: WorkRecord,
        classification: WorkSessionSemanticClassification,
    ) -> Dict[str, Any]:
        if classification.state == "stale":
            owner_state = "stale"
            usable_outcome = "stale"
        else:
            owner_state = "blocked"
            usable_outcome = "blocked"

        reason = _bounded_reason(classification.reason)
        owner_action = _bounded_reason(classification.required_owner_action)
        return {
            "work_id": work_record.work_id,
            "owner": "hermes",
            "owner_session_id": work_record.owner_session_id,
            "executor": "omx",
            "executor_session_id": work_record.executor_session_id,
            "tmux_session": record.tmux_session or work_record.tmux_session,
            "repo_path": record.repo_path or work_record.repo_path,
            "worktree_path": record.worktree_path or work_record.worktree_path,
            "state": owner_state,
            "usable_outcome": usable_outcome,
            "close_disposition": "close",
            "next_action": (
                f"Classifier {classification.state}: {owner_action}. "
                f"Reason: {reason}"
            ),
            "proof": (
                f"clawhip:{classification.state}:"
                f"{self._semantic_reason_slug(reason)}"
            ),
            "classifier_state": classification.state,
            "classifier_reason": reason,
            "required_owner_action": owner_action,
            "provider": record.provider,
            "provider_session_id": record.provider_session_id,
            "linear_card_id": record.linear_card_id,
            "lane_id": record.lane_id,
        }

    @staticmethod
    def _bounded_deliver_prompt(prompt: str) -> str:
        text = re.sub(r"[ \t]+", " ", str(prompt or "").strip())
        text = re.sub(r"\n{3,}", "\n\n", text)
        if len(text) <= WORK_SESSION_DELIVER_PROMPT_MAX_CHARS:
            return text
        return text[: WORK_SESSION_DELIVER_PROMPT_MAX_CHARS - 3].rstrip() + "..."

    def _build_deliver_prompt(
        self,
        record: WorkSessionRecord,
        work_record: WorkRecord,
        classification: WorkSessionSemanticClassification,
    ) -> str:
        reason = _bounded_reason(classification.reason)
        owner_action = _bounded_reason(classification.required_owner_action)
        prompt = f"""
[Hermes bounded clawhip deliver]
Repo: {record.repo_name or record.repo_path or work_record.repo_path or 'unknown'}
Worktree: {record.worktree_path or work_record.worktree_path or 'unknown'}
Card: {record.linear_card_id}
Lane: {record.lane_id}
Provider session: {record.provider}:{record.provider_session_id}
Tmux target: {record.tmux_session or work_record.tmux_session or 'unknown'} {record.tmux_pane or ''}
Classifier: {classification.state} — {reason}
Owner action: {owner_action}

Submit exactly one bounded follow-up to this existing hooked pane only. Do not
open, retarget, approve, deny, or mutate any unrelated session. If already
safe to proceed, continue only the current repo/card/lane work; otherwise emit
a concise native event describing the blocker and required owner action.
""".strip()
        return self._bounded_deliver_prompt(prompt)

    def _deliver_refusal_reason(
        self,
        record: WorkSessionRecord,
        work_record: Optional[WorkRecord],
        classification: WorkSessionSemanticClassification,
    ) -> Optional[str]:
        if classification.state not in WORK_SESSION_ACTIONABLE_SEMANTIC_STATES:
            return "classification_not_actionable"
        if record.lifecycle_state != "active":
            return "work_session_not_active"
        if (
            record.deliver_attempts
            or record.deliver_status in WORK_SESSION_DELIVER_TERMINAL_STATUSES
            or record.deliver_status == "nudged"
        ):
            return "deliver_already_attempted"
        if not (record.tmux_session and record.tmux_pane):
            return "missing_hooked_tmux_pane"
        if not (
            record.watch_status == "active"
            and record.watch_owner == "hermes"
            and record.watch_registration_source == WORK_SESSION_AUTO_WATCH_SOURCE
        ):
            return "work_session_not_trusted_hooked"
        if work_record is None:
            return "missing_verified_delegated_work_record"
        if record.linear_card_id != (record.watch_linear_card_id or record.linear_card_id):
            return "card_watch_mismatch"
        return None

    def evaluate_clawhip_deliver_policy(
        self,
        record: WorkSessionRecord,
        work_record: Optional[WorkRecord],
        classification: WorkSessionSemanticClassification,
    ) -> Dict[str, Any]:
        """Pure-ish policy decision for whether Hermes may call clawhip deliver."""

        reason = self._deliver_refusal_reason(record, work_record, classification)
        if reason is not None:
            return {"allowed": False, "reason": reason}
        assert work_record is not None
        prompt = self._build_deliver_prompt(record, work_record, classification)
        return {
            "allowed": True,
            "reason": "eligible",
            "prompt": prompt,
            "provider": record.provider,
            "provider_session_id": record.provider_session_id,
            "linear_card_id": record.linear_card_id,
            "lane_id": record.lane_id,
            "repo_path": record.repo_path or work_record.repo_path,
            "worktree_path": record.worktree_path or work_record.worktree_path,
            "tmux_session": record.tmux_session or work_record.tmux_session,
            "tmux_pane": record.tmux_pane,
            "work_id": work_record.work_id,
            "owner_session_id": work_record.owner_session_id,
            "classification_state": classification.state,
            "classification_reason": _bounded_reason(classification.reason),
        }

    @staticmethod
    def _deliver_result_marker_changed(result: Dict[str, Any]) -> bool:
        if any(
            _is_truthy_marker(result.get(key))
            for key in (
                "prompt_submit_marker_changed",
                "promptSubmitMarkerChanged",
                "marker_changed",
                "markerChanged",
            )
        ):
            return True
        before = _normalize_text(
            result.get("prompt_submit_marker_before")
            or result.get("promptSubmitMarkerBefore")
            or result.get("marker_before")
            or result.get("markerBefore")
        )
        after = _normalize_text(
            result.get("prompt_submit_marker_after")
            or result.get("promptSubmitMarkerAfter")
            or result.get("marker_after")
            or result.get("markerAfter")
        )
        return bool(before and after and before != after)

    @staticmethod
    def _deliver_result_has_followup_evidence(result: Dict[str, Any]) -> bool:
        for key in (
            "pane_evidence",
            "paneEvidence",
            "pane_event",
            "paneEvent",
            "follow_up_native_event",
            "followUpNativeEvent",
            "followup_native_event",
            "native_event",
            "nativeEvent",
            "event_after",
            "eventAfter",
        ):
            value = result.get(key)
            if isinstance(value, (dict, list, tuple)) and value:
                return True
            if _normalize_text(value):
                return True

        event_name = _normalize_text(result.get("event_name") or result.get("eventName"))
        event_delivery = _normalize_text(
            result.get("delivery_id")
            or result.get("deliveryId")
            or result.get("event_id")
            or result.get("eventId")
            or result.get("request_id")
            or result.get("requestId")
        )
        if event_name and event_delivery:
            return True

        return any(
            _is_truthy_marker(result.get(key))
            for key in ("follow_up_event", "followUpEvent", "pane_changed", "paneChanged")
        )

    def _deliver_result_success(self, result: Dict[str, Any]) -> bool:
        status = str(result.get("status") or result.get("verdict") or "").strip().lower()
        ok = bool(result.get("ok")) or status in {"accepted", "succeeded", "success", "ok"}
        return (
            ok
            and self._deliver_result_marker_changed(result)
            and self._deliver_result_has_followup_evidence(result)
        )

    def _compact_deliver_evidence(self, result: Dict[str, Any], *, success: bool) -> Dict[str, Any]:
        keys = (
            "ok",
            "status",
            "verdict",
            "returncode",
            "prompt_submit_marker_changed",
            "prompt_submit_marker_before",
            "prompt_submit_marker_after",
            "marker_before",
            "marker_after",
            "pane_evidence",
            "pane_event",
            "follow_up_native_event",
            "native_event",
            "event_name",
            "resolved",
            "lifecycle_state",
            "error",
        )
        evidence = {key: result.get(key) for key in keys if key in result}
        evidence["success"] = success
        evidence["marker_changed"] = self._deliver_result_marker_changed(result)
        evidence["followup_evidence"] = self._deliver_result_has_followup_evidence(result)
        return evidence

    def _maybe_deliver_locked(
        self,
        record: WorkSessionRecord,
        payload: Dict[str, Any],
        event_at: datetime,
        classification: WorkSessionSemanticClassification,
    ) -> Optional[Dict[str, Any]]:
        work_record = self._verified_hermes_work_record_for_watch(record, payload)
        decision = self.evaluate_clawhip_deliver_policy(record, work_record, classification)
        if not decision.get("allowed"):
            return {"status": "refused", "reason": decision.get("reason")}

        deliver_request = dict(decision)
        deliver_request.pop("allowed", None)
        deliver_request["source"] = "hermes-work-session-deliver-policy"
        deliver_request["attempt"] = int(record.deliver_attempts or 0) + 1
        record.lifecycle_state = "nudged"
        record.deliver_status = "nudged"
        record.deliver_attempts = int(record.deliver_attempts or 0) + 1
        record.deliver_started_at = event_at
        record.deliver_completed_at = None
        record.deliver_prompt = str(deliver_request.get("prompt") or "")
        record.deliver_error = None
        record.deliver_evidence = {
            "request": {k: v for k, v in deliver_request.items() if k != "prompt"},
            "transition_from": "active",
            "transition_to": "nudged",
            "nudged_at": event_at.isoformat(),
        }

        try:
            result = self._deliver_executor(deliver_request)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "error": "invalid_clawhip_deliver_result",
                "raw": str(result),
            }

        success = self._deliver_result_success(result)
        record.deliver_completed_at = _utcnow()
        record.deliver_evidence = self._compact_deliver_evidence(result, success=success)
        if success:
            resolved = (
                _is_truthy_marker(result.get("resolved"))
                or str(result.get("lifecycle_state") or "").strip().lower() == "resolved"
            )
            record.lifecycle_state = "resolved" if resolved else "active"
            record.deliver_status = "succeeded"
            record.deliver_error = None
            record.deliver_evidence["transition"] = f"nudged->{record.lifecycle_state}"
            record.deliver_evidence["completed_at"] = record.deliver_completed_at.isoformat()
            return {
                "status": "succeeded",
                "reason": "prompt_submit_marker_and_followup_evidence",
                "lifecycle_state": record.lifecycle_state,
                "evidence": dict(record.deliver_evidence or {}),
                "prompt": record.deliver_prompt,
            }

        record.lifecycle_state = "failed"
        record.deliver_status = "failed"
        record.deliver_evidence["transition"] = "nudged->failed"
        record.deliver_evidence["completed_at"] = record.deliver_completed_at.isoformat()
        record.deliver_error = _bounded_reason(
            result.get("error")
            or result.get("stderr")
            or "clawhip deliver did not return marker-change plus follow-up evidence"
        )
        return {
            "status": "failed",
            "reason": record.deliver_error,
            "lifecycle_state": record.lifecycle_state,
            "evidence": dict(record.deliver_evidence or {}),
            "prompt": record.deliver_prompt,
        }

    def _apply_semantic_classification_locked(
        self,
        record: WorkSessionRecord,
        payload: Dict[str, Any],
        event_at: datetime,
        classification: WorkSessionSemanticClassification,
    ) -> Optional[Dict[str, Any]]:
        prior_state = record.semantic_state
        prior_reason = record.semantic_reason
        record.semantic_state = classification.state
        record.semantic_reason = _bounded_reason(classification.reason)
        record.semantic_required_owner_action = _bounded_reason(
            classification.required_owner_action
        )

        if classification.state == "completed_idle":
            self._cleanup_tmux_watch_locked(record, event_at, lifecycle_state="resolved")
            return None
        if classification.state == "orphaned":
            self._cleanup_tmux_watch_locked(record, event_at, lifecycle_state="orphaned")
            return None
        if not classification.should_alert:
            return None
        if record.lifecycle_state in {"stopped", "resolved", "orphaned"}:
            return None
        if not record.semantic_reason:
            return None
        if (
            record.semantic_alerted_at
            and prior_state == classification.state
            and prior_reason == record.semantic_reason
        ):
            return None

        work_record = self._verified_hermes_work_record_for_watch(record, payload)
        if work_record is None:
            return None

        record.semantic_alerted_at = event_at
        return self._build_semantic_alert_payload(record, work_record, classification)

    def ingest_clawhip_native_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {"status": "rejected", "reason": "invalid_native_event_payload"}

        provider = str(payload.get("provider") or "").strip()
        provider_session_id = str(
            payload.get("session_id") or payload.get("provider_session_id") or ""
        ).strip()
        event = str(
            payload.get("event")
            or payload.get("native_event")
            or payload.get("event_name")
            or payload.get("hook_event_name")
            or ""
        ).strip()
        if not provider or not provider_session_id or not event:
            return {"status": "rejected", "reason": "missing_provider_session_or_event"}

        event_at = _parse_event_datetime(payload.get("timestamp") or payload.get("event_at"))
        metadata = _compact_native_metadata(payload)
        normalized_event = _normalize_work_session_event_name(event)
        should_classify = _is_work_session_action_required_event(normalized_event, payload)

        with self._lock:
            self._ensure_loaded()
            existing_idx = None
            existing = None
            for idx, candidate in enumerate(self._records):
                if candidate.provider == provider and candidate.provider_session_id == provider_session_id:
                    existing_idx = idx
                    existing = candidate
                    break

            linear_card_id = str(payload.get("linear_card_id") or payload.get("card_id") or "").strip()
            lane_id = str(payload.get("lane_id") or payload.get("current_lane") or "").strip()
            if existing is None and (not linear_card_id or not lane_id):
                return {"status": "rejected", "reason": "missing_card_or_lane_linkage"}

            if existing is None:
                lifecycle_state = "waiting_user" if normalized_event == "userpromptsubmit" else "active"
                record = WorkSessionRecord(
                    provider=provider,
                    provider_session_id=provider_session_id,
                    linear_card_id=linear_card_id,
                    lane_id=lane_id,
                    lifecycle_state=lifecycle_state,
                    started_at=event_at,
                    last_event_at=event_at,
                    last_event=event,
                    repo_path=payload.get("repo_path"),
                    worktree_path=payload.get("worktree_path"),
                    repo_name=payload.get("repo_name"),
                    project=payload.get("project"),
                    branch=payload.get("branch"),
                    directory=payload.get("directory"),
                    tmux_session=payload.get("tmux_session"),
                    tmux_pane=payload.get("tmux_pane"),
                    ingress_route=payload.get("ingress_route"),
                    first_delivery_id=payload.get("delivery_id"),
                    latest_delivery_id=payload.get("delivery_id"),
                    watch_status="unwatched",
                    native_event_metadata=metadata,
                )
                self._records.append(record)
            else:
                if normalized_event in {"userpromptsubmit", "session.prompt-submitted"}:
                    existing.lifecycle_state = "waiting_user"
                elif _is_work_session_stop_event(normalized_event):
                    self._cleanup_tmux_watch_locked(existing, event_at, lifecycle_state="stopped")
                elif _is_work_session_resolved_event(normalized_event, payload):
                    self._cleanup_tmux_watch_locked(existing, event_at, lifecycle_state="resolved")
                elif existing.lifecycle_state in {"stopped", "resolved", "orphaned", "failed"}:
                    pass
                else:
                    existing.lifecycle_state = "active"

                existing.last_event = event
                existing.last_event_at = event_at
                existing.latest_delivery_id = payload.get("delivery_id") or existing.latest_delivery_id
                existing.native_event_metadata = metadata

                # Preserve initial Hermes linkage and route identity. Later upstream events
                # may carry partial/stale fields, so treat them as evidence metadata rather
                # than authority to retarget the session.
                for attr in ("tmux_session", "tmux_pane", "repo_name", "project", "branch", "directory"):
                    if getattr(existing, attr) is None and payload.get(attr):
                        setattr(existing, attr, payload.get(attr))
                record = existing
                if existing_idx is not None:
                    self._records[existing_idx] = existing

            self._maybe_auto_register_tmux_watch_locked(record, payload, event_at)
            classification = None
            semantic_alert = None
            deliver_policy = None
            if should_classify:
                classification = classify_work_session_action_required(
                    payload,
                    record=record,
                    normalized_event=normalized_event,
                )
                deliver_policy = self._maybe_deliver_locked(
                    record,
                    payload,
                    event_at,
                    classification,
                )
                if not (
                    isinstance(deliver_policy, dict)
                    and deliver_policy.get("status") == "succeeded"
                ):
                    semantic_alert = self._apply_semantic_classification_locked(
                        record,
                        payload,
                        event_at,
                        classification,
                    )
            if record.lifecycle_state not in WORK_SESSION_LIFECYCLE_STATES:
                return {"status": "rejected", "reason": "invalid_lifecycle_state"}
            if record.watch_status not in WORK_SESSION_WATCH_STATUSES:
                return {"status": "rejected", "reason": "invalid_watch_status"}
            self._save_locked()
            result = {
                "status": "accepted",
                "reason": "registered",
                "record": WorkSessionRecord.from_dict(record.to_dict()),
            }
            if classification is not None:
                result["classification"] = classification.to_dict()
            if deliver_policy is not None:
                result["deliver_policy"] = deliver_policy
            if semantic_alert is not None:
                result["semantic_alert"] = semantic_alert
            return result


class WorkStateStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else get_hermes_home() / "gateway_work_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records: List[WorkRecord] = []
        self._loaded = False
        self._last_mtime_ns: Optional[int] = None

    def _current_mtime_ns(self) -> Optional[int]:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _ensure_loaded(self) -> None:
        current_mtime_ns = self._current_mtime_ns()
        if self._loaded and current_mtime_ns == self._last_mtime_ns:
            return
        if current_mtime_ns is None:
            self._records = []
            self._loaded = True
            self._last_mtime_ns = None
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            payload = []
        if isinstance(payload, dict):
            payload = payload.get("records", [])
        self._records = [
            WorkRecord.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]
        self._loaded = True
        self._last_mtime_ns = current_mtime_ns

    def _save_locked(self) -> None:
        self.path.write_text(
            json.dumps([record.to_dict() for record in self._records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._last_mtime_ns = self._current_mtime_ns()

    def list_records(self) -> List[WorkRecord]:
        with self._lock:
            self._ensure_loaded()
            return [WorkRecord.from_dict(record.to_dict()) for record in self._records]

    def upsert(self, record: WorkRecord) -> None:
        with self._lock:
            self._ensure_loaded()
            _apply_omx_lane_truth(record)
            for idx, existing in enumerate(self._records):
                if (
                    existing.work_id == record.work_id
                    and existing.owner_session_id == record.owner_session_id
                ):
                    self._records[idx] = record
                    self._save_locked()
                    return
            self._records.append(record)
            self._save_locked()

    def update_record(
        self,
        work_id: str,
        owner_session_id: str,
        **updates: Any,
    ) -> bool:
        with self._lock:
            self._ensure_loaded()
            for record in self._records:
                if record.work_id == work_id and record.owner_session_id == owner_session_id:
                    for key, value in updates.items():
                        if hasattr(record, key):
                            setattr(record, key, value)
                    _apply_omx_lane_truth(record)
                    self._save_locked()
                    return True
        return False

    def find_matching_records(
        self,
        work_id: str,
        *,
        owner_session_id: Optional[str] = None,
        live_only: bool = True,
    ) -> List[WorkRecord]:
        with self._lock:
            self._ensure_loaded()
            matches = [record for record in self._records if record.work_id == work_id]
            if owner_session_id:
                matches = [record for record in matches if record.owner_session_id == owner_session_id]
            if live_only:
                matches = [record for record in matches if record.state in LIVE_STATES]
            return [WorkRecord.from_dict(record.to_dict()) for record in matches]

    def resolve_owner_ingress_candidate(
        self,
        work_id: str,
        *,
        owner_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        matches = self.find_matching_records(
            work_id,
            owner_session_id=owner_session_id,
            live_only=True,
        )
        if not matches:
            return {
                "status": "missing",
                "reason": "missing_or_closed_work_record",
                "matches": [],
            }
        hermes_matches = [record for record in matches if record.owner == "hermes"]
        if not hermes_matches:
            return {
                "status": "missing",
                "reason": "missing_or_closed_work_record",
                "matches": [],
            }
        wake_matches = [record for record in hermes_matches if record.state in WAKE_STATES]
        if not wake_matches:
            return {
                "status": "missing",
                "reason": "missing_or_closed_work_record",
                "matches": [],
            }
        if len(wake_matches) > 1:
            return {
                "status": "ambiguous",
                "reason": "ambiguous_owner_resolution",
                "matches": [record.to_dict() for record in wake_matches],
            }
        return {
            "status": "single_match",
            "reason": "eligible",
            "matches": [wake_matches[0].to_dict()],
            "record": wake_matches[0],
        }

    def resolve_delegated_signal_candidate(
        self,
        *,
        work_id: Optional[str] = None,
        owner_session_id: Optional[str] = None,
        executor_session_id: Optional[str] = None,
        tmux_session: Optional[str] = None,
        repo_path: Optional[str] = None,
        worktree_path: Optional[str] = None,
        live_only: bool = True,
    ) -> Dict[str, Any]:
        normalized_repo_path = _normalize_path_value(repo_path)
        normalized_worktree_path = _normalize_path_value(worktree_path)

        with self._lock:
            self._ensure_loaded()
            matches = [
                record
                for record in self._records
                if record.owner == "hermes"
                and record.executor == "omx"
                and record.mode == "delegated"
            ]
            if live_only:
                matches = [record for record in matches if record.state in LIVE_STATES]
            if work_id:
                matches = [record for record in matches if record.work_id == work_id]
            if owner_session_id:
                matches = [record for record in matches if record.owner_session_id == owner_session_id]
            if executor_session_id:
                matches = [
                    record for record in matches if record.executor_session_id == executor_session_id
                ]
            if tmux_session:
                matches = [record for record in matches if record.tmux_session == tmux_session]
            if normalized_repo_path:
                matches = [
                    record
                    for record in matches
                    if _normalize_path_value(record.repo_path) == normalized_repo_path
                ]
            if normalized_worktree_path:
                matches = [
                    record
                    for record in matches
                    if _normalize_path_value(record.worktree_path) == normalized_worktree_path
                ]
            matches = [WorkRecord.from_dict(record.to_dict()) for record in matches]

        if not matches:
            return {
                "status": "missing",
                "reason": "missing_or_closed_delegated_work_record",
                "matches": [],
            }
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "reason": "ambiguous_delegated_work_resolution",
                "matches": [record.to_dict() for record in matches],
            }
        return {
            "status": "single_match",
            "reason": "eligible",
            "matches": [matches[0].to_dict()],
            "record": matches[0],
        }

    def mark_owner_sessions_blocked(
        self,
        session_keys: List[str],
        *,
        next_action: str = "Resume the interrupted turn",
        proof: str = "gateway_restart_checkpoint",
    ) -> int:
        keys = {key for key in session_keys if key}
        if not keys:
            return 0
        updated = 0
        with self._lock:
            self._ensure_loaded()
            for record in self._records:
                if (
                    record.owner == "hermes"
                    and record.mode == "direct"
                    and record.owner_session_id in keys
                    and record.state in {"created", "running", "stale"}
                ):
                    record.state = "blocked"
                    if next_action:
                        record.next_action = next_action
                    record.last_progress_at = _utcnow()
                    record.proof = proof
                    updated += 1
            if updated:
                self._save_locked()
        return updated

    def derive_direct_signal(
        self,
        work_id: str,
        *,
        now: Optional[datetime] = None,
        stale_after_seconds: int = 0,
    ) -> Optional[Dict[str, str]]:
        now = now or _utcnow()
        matches = self.find_matching_records(work_id, live_only=True)
        if len(matches) != 1:
            return None
        record = matches[0]
        if record.owner != "hermes" or record.mode != "direct":
            return None
        if not record.next_action:
            return None
        if record.state == "blocked":
            return {
                "work_id": record.work_id,
                "owner": record.owner,
                "owner_session_id": record.owner_session_id,
                "state": "blocked",
                "next_action": record.next_action,
                "proof": record.proof or "work_state=blocked",
            }
        if record.state in WAKE_STATES:
            return {
                "work_id": record.work_id,
                "owner": record.owner,
                "owner_session_id": record.owner_session_id,
                "state": record.state,
                "next_action": record.next_action,
                "proof": record.proof or f"work_state={record.state}",
            }
        if (
            record.state == "running"
            and stale_after_seconds > 0
            and (now - record.last_progress_at).total_seconds() >= stale_after_seconds
        ):
            return {
                "work_id": record.work_id,
                "owner": record.owner,
                "owner_session_id": record.owner_session_id,
                "state": "stale",
                "next_action": record.next_action,
                "proof": (
                    f"last_progress_at older than {int(stale_after_seconds)}s "
                    f"({record.last_progress_at.isoformat()})"
                ),
            }
        return None
