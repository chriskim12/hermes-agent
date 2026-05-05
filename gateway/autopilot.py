"""Hermes /autopilot command controller surface.

CH-385 deliberately stopped at the command/plugin entrypoint and deterministic
classification layer.  CH-389 adds the bounded one-task materialization helper:
controller intent + Linear admission can create one work_state lock and invoke a
caller-supplied executor spawner, while still refusing Linear Done mutations and
multi-task continuation.
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
from gateway.goal_contract import GoalContractError, generate_goal_contract


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

    def list_execution_ready_issues(
        self,
        *,
        team_key: str = "CH",
        state_name: str = "Execution Ready",
        limit: int = 100,
    ) -> Mapping[str, Any]:
        """Return live Linear queue candidates without mutating Linear."""


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

    _ISSUE_FIELDS = """
        identifier
        title
        description
        priority
        url
        state { name type }
        parent { identifier title state { name type } }
        project { name }
        labels { nodes { name } }
    """
    _QUERY = f"""
    query AutopilotIssue($id: String!) {{
      issue(id: $id) {{
        {_ISSUE_FIELDS}
        children(first: 50) {{ nodes {{ {_ISSUE_FIELDS} }} }}
      }}
    }}
    """
    _QUEUE_QUERY = f"""
    query AutopilotExecutionReadyQueue($teamKey: String!, $stateName: String!, $first: Int!) {{
      teams(filter: {{ key: {{ eq: $teamKey }} }}, first: 1) {{
        nodes {{
          key
          name
          issues(first: $first, filter: {{ state: {{ name: {{ eq: $stateName }} }} }}) {{
            nodes {{ {_ISSUE_FIELDS} children(first: 50) {{ nodes {{ {_ISSUE_FIELDS} }} }} }}
          }}
        }}
      }}
    }}
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.getenv("LINEAR_API_KEY")

    def _post_graphql(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        identifier: Optional[str] = None,
    ) -> Mapping[str, Any]:
        if not self.api_key:
            payload: dict[str, Any] = {
                "status": "unavailable",
                "reason": "LINEAR_API_KEY_missing",
            }
            if identifier:
                payload["identifier"] = identifier
            return payload
        body = json.dumps({"query": query, "variables": dict(variables)}).encode("utf-8")
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
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = {
                "status": "unavailable",
                "reason": f"linear_http_{exc.code}",
            }
            if identifier:
                payload["identifier"] = identifier
            return payload
        except Exception as exc:
            payload = {
                "status": "unavailable",
                "reason": f"linear_query_{type(exc).__name__}",
            }
            if identifier:
                payload["identifier"] = identifier
            return payload

    def fetch_issue(self, identifier: str) -> Mapping[str, Any]:
        payload = self._post_graphql(self._QUERY, {"id": identifier}, identifier=identifier)
        if payload.get("status") == "unavailable":
            return payload

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

    def list_execution_ready_issues(
        self,
        *,
        team_key: str = "CH",
        state_name: str = "Execution Ready",
        limit: int = 100,
    ) -> Mapping[str, Any]:
        payload = self._post_graphql(
            self._QUEUE_QUERY,
            {"teamKey": team_key, "stateName": state_name, "first": int(limit)},
        )
        if payload.get("status") == "unavailable":
            return payload
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            return {
                "status": "unavailable",
                "reason": "linear_graphql_error",
                "errors": errors,
            }
        teams = (((payload.get("data") or {}).get("teams") or {}).get("nodes") if isinstance(payload, dict) else None)
        if not teams:
            return {
                "status": "missing",
                "reason": "linear_team_not_found",
                "team_key": team_key,
                "issues": [],
            }
        team = teams[0]
        issues = (((team.get("issues") or {}).get("nodes")) if isinstance(team, Mapping) else None)
        return {
            "status": "ok",
            "team_key": team.get("key") or team_key,
            "team_name": team.get("name"),
            "state_name": state_name,
            "issues": issues if isinstance(issues, list) else [],
        }


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


def _empty_dry_run(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "selected_issue": None,
        "would_create_work_state": None,
        "would_goal_contract": None,
        "candidates": [],
        "groups": {"parents": {}, "projects": {}},
    }
    payload.update(extra)
    return payload


def _issue_identifier_sort_key(issue: Mapping[str, Any]) -> tuple[str, int, str]:
    identifier = str(issue.get("identifier") or "")
    match = re.fullmatch(r"([A-Z][A-Z0-9]*)-(\d+)", identifier.upper())
    if not match:
        return (identifier.upper(), 10**12, identifier.upper())
    return (match.group(1), int(match.group(2)), identifier.upper())


def _state_parts(issue: Mapping[str, Any]) -> tuple[str, str]:
    state = issue.get("state") if isinstance(issue.get("state"), Mapping) else {}
    return (str(state.get("name") or "").strip(), str(state.get("type") or "").strip())


def _is_terminal_state(state_name: str, state_type: str) -> bool:
    name = str(state_name or "").strip().lower()
    typ = str(state_type or "").strip().lower()
    return typ in {"completed", "canceled", "cancelled"} or name in {
        "done",
        "canceled",
        "cancelled",
        "duplicate",
        "won't do",
        "wont do",
    }


def _parent_payload(issue: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    parent = issue.get("parent")
    return parent if isinstance(parent, Mapping) else None


def _parent_identifier(issue: Mapping[str, Any], parent_override: Optional[str] = None) -> Optional[str]:
    if parent_override:
        return parent_override
    parent = _parent_payload(issue)
    if not parent:
        return None
    identifier = parent.get("identifier")
    return str(identifier) if identifier else None


def _project_name(issue: Mapping[str, Any]) -> Optional[str]:
    project = issue.get("project")
    if isinstance(project, Mapping) and project.get("name"):
        return str(project.get("name"))
    return None


def _description_text(issue: Mapping[str, Any]) -> str:
    return str(issue.get("description") or "")


def _has_done_when(issue: Mapping[str, Any]) -> bool:
    text = _description_text(issue).lower()
    return "done when" in text or "done_when" in text


def _has_verification(issue: Mapping[str, Any]) -> bool:
    text = _description_text(issue).lower()
    return "verification" in text or "verify" in text


def _work_state_backing_file_error(store: Any) -> Optional[str]:
    path_value = getattr(store, "path", None)
    if not path_value:
        return None
    try:
        path = Path(path_value)
    except TypeError:
        return "work_state_unavailable:invalid_path"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"work_state_unavailable:{type(exc).__name__}"
    if isinstance(payload, list):
        if not all(isinstance(item, dict) for item in payload):
            return "work_state_unavailable:invalid_record_item_type"
        return None
    if not isinstance(payload, dict):
        return "work_state_unavailable:invalid_payload_type"
    if set(payload.keys()) != {"records"}:
        return "work_state_unavailable:invalid_payload_schema"
    records = payload.get("records")
    if not isinstance(records, list):
        return "work_state_unavailable:invalid_records_type"
    if not all(isinstance(item, dict) for item in records):
        return "work_state_unavailable:invalid_record_item_type"
    return None


def _work_state_lock_snapshot(work_state_store: Any = None) -> dict[str, Any]:
    try:
        from gateway.work_state import LIVE_STATES

        store = work_state_store
        if store is None:
            from gateway.work_state import WorkStateStore

            store = WorkStateStore()
        backing_error = _work_state_backing_file_error(store)
        if backing_error:
            return {"available": False, "reason": backing_error, "active_work_ids": set()}
        work_ids: set[str] = set()
        for record in store.list_records():
            if getattr(record, "owner", None) != "hermes":
                continue
            state = str(getattr(record, "state", "") or "")
            if state not in LIVE_STATES:
                continue
            work_id = str(getattr(record, "work_id", "") or "").strip()
            if work_id:
                work_ids.add(work_id)
        return {"available": True, "active_work_ids": work_ids}
    except Exception as exc:
        return {
            "available": False,
            "reason": f"work_state_unavailable:{type(exc).__name__}",
            "active_work_ids": set(),
        }


def _candidate_from_issue(
    issue: Mapping[str, Any],
    *,
    active_work_ids: set[str],
    parent_override: Optional[str] = None,
) -> dict[str, Any]:
    identifier = str(issue.get("identifier") or "").strip()
    classified = classify_linear_target(issue, identifier)
    shape = str(classified.get("shape") or "unknown")
    if parent_override and shape == "standalone":
        shape = "child"
    parent_id = _parent_identifier(issue, parent_override)
    candidate = {
        "identifier": identifier,
        "eligible": False,
        "reason": "unknown",
        "shape": shape,
        "parent_id": parent_id,
        "project": _project_name(issue),
    }
    if classified.get("status") != "ok":
        candidate["reason"] = str(classified.get("reason") or "linear_target_unavailable_fail_closed")
        return candidate

    state_name, state_type = _state_parts(issue)
    parent = _parent_payload(issue)
    parent_state = parent.get("state") if isinstance(parent, Mapping) else None
    parent_state_name = str((parent_state or {}).get("name") or "") if isinstance(parent_state, Mapping) else ""
    parent_state_type = str((parent_state or {}).get("type") or "") if isinstance(parent_state, Mapping) else ""

    if _is_terminal_state(state_name, state_type):
        candidate["reason"] = "terminal_state"
    elif state_name.lower() != "execution ready":
        candidate["reason"] = "state_not_execution_ready"
    elif parent and _is_terminal_state(parent_state_name, parent_state_type):
        candidate["reason"] = "parent_terminal"
    elif shape == "parent":
        candidate["reason"] = "parent_target_requires_child_selection"
    elif identifier in active_work_ids:
        candidate["reason"] = "active_work_state_lock"
    elif not _has_done_when(issue):
        candidate["reason"] = "missing_done_when"
    elif not _has_verification(issue):
        candidate["reason"] = "missing_verification"
    else:
        candidate["eligible"] = True
        candidate["reason"] = "eligible_execution_ready"
    return candidate


def _group_candidates(candidates: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    parents: dict[str, int] = {}
    projects: dict[str, int] = {}
    for candidate in candidates:
        parent_key = str(candidate.get("parent_id") or "standalone")
        project_key = str(candidate.get("project") or "none")
        parents[parent_key] = parents.get(parent_key, 0) + 1
        projects[project_key] = projects.get(project_key, 0) + 1
    return {"parents": parents, "projects": projects}


def _selected_issue_payload(issue: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "identifier": issue.get("identifier"),
        "title": issue.get("title") or "",
        "url": issue.get("url"),
    }


def _would_goal_contract(issue: Mapping[str, Any]) -> dict[str, Any]:
    identifier = str(issue.get("identifier") or "")
    title = str(issue.get("title") or "")
    summary = f"Execute Linear {identifier}: {title}".strip()
    try:
        contract = generate_goal_contract(issue, mode="single-card")
        return {
            "command": contract["prompt"],
            "summary": summary,
            "mode": contract["mode"],
        }
    except GoalContractError as exc:
        return {
            "command": f"/goal {summary}; verify Done when before closeout; preserve dry-run side-effect boundaries.",
            "summary": summary,
            "blocked_reason": exc.reason,
        }


def _would_work_state(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "work_id": candidate.get("identifier"),
        "owner": "hermes",
        "executor": "hermes",
        "mode": "autopilot",
        "state": "created",
        "parent_id": candidate.get("parent_id"),
    }


def _finish_dry_run(
    issues: list[Mapping[str, Any]],
    *,
    active_work_ids: set[str],
    parent_override: Optional[str] = None,
    no_eligible_reason: str = "no_eligible_execution_ready_issue",
) -> dict[str, Any]:
    sorted_issues = sorted(issues, key=_issue_identifier_sort_key)
    candidates = [
        _candidate_from_issue(
            issue,
            active_work_ids=active_work_ids,
            parent_override=parent_override,
        )
        for issue in sorted_issues
    ]
    groups = _group_candidates(candidates)
    for issue, candidate in zip(sorted_issues, candidates):
        if candidate.get("eligible"):
            return {
                "status": "would_run",
                "reason": "dry_run_selected_execution_ready_issue",
                "selected_issue": _selected_issue_payload(issue),
                "would_create_work_state": _would_work_state(candidate),
                "would_goal_contract": _would_goal_contract(issue),
                "candidates": candidates,
                "groups": groups,
            }
    return {
        "status": "blocked",
        "reason": no_eligible_reason,
        "selected_issue": None,
        "would_create_work_state": None,
        "would_goal_contract": None,
        "candidates": candidates,
        "groups": groups,
    }


def evaluate_dry_run_admission(
    *,
    command: AutopilotCommand,
    state: Mapping[str, Any],
    linear_client: LinearIssueClient,
    work_state_store: Any = None,
    target_issue: Optional[Mapping[str, Any]] = None,
    target_classification: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Produce CH-388 read-only admission proof for /autopilot dry-run."""

    if command.action != ACTION_DRY_RUN:
        return _empty_dry_run("blocked", "not_a_dry_run")

    lock_snapshot = _work_state_lock_snapshot(work_state_store)
    if not lock_snapshot.get("available"):
        return _empty_dry_run(
            "blocked",
            "work_state_unavailable_fail_closed",
            work_state_reason=lock_snapshot.get("reason"),
        )
    active_ids = lock_snapshot.get("active_work_ids")
    active_ids = active_ids if isinstance(active_ids, set) else set()
    if not command.target_id:
        if not bool(state.get("enabled")):
            return _empty_dry_run("paused", "controller_disabled_noop")
        queue = linear_client.list_execution_ready_issues(
            team_key="CH",
            state_name="Execution Ready",
            limit=100,
        )
        if queue.get("status") != "ok":
            return _empty_dry_run(
                "blocked",
                "linear_queue_unavailable_fail_closed",
                linear_reason=queue.get("reason") or queue.get("status"),
            )
        issues = queue.get("issues") if isinstance(queue.get("issues"), list) else []
        return _finish_dry_run(issues, active_work_ids=active_ids)

    target_payload = target_issue or linear_client.fetch_issue(command.target_id)
    if target_payload.get("status") != "ok":
        return _empty_dry_run(
            "blocked",
            "linear_target_unavailable_fail_closed",
            linear_reason=target_payload.get("reason") or target_payload.get("status"),
        )
    classified = target_classification or classify_linear_target(target_payload, command.target_id)
    if classified.get("shape") == "parent":
        state_name, state_type = _state_parts(target_payload)
        if _is_terminal_state(state_name, state_type):
            return _empty_dry_run("blocked", "parent_terminal")
        children = target_payload.get("children") or {}
        child_nodes = children.get("nodes") if isinstance(children, Mapping) else []
        child_issues = [child for child in child_nodes if isinstance(child, Mapping)]
        if not child_issues:
            return _empty_dry_run("blocked", "no_eligible_child")
        return _finish_dry_run(
            child_issues,
            active_work_ids=active_ids,
            parent_override=str(classified.get("identifier") or command.target_id),
            no_eligible_reason="no_eligible_child",
        )
    return _finish_dry_run([target_payload], active_work_ids=active_ids)


def _admission_decision(
    *,
    command: AutopilotCommand,
    state: Mapping[str, Any],
    linear: Optional[Mapping[str, Any]],
    dry_run: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    if command.action == ACTION_DRY_RUN and dry_run:
        return {
            "status": str(dry_run.get("status") or "blocked"),
            "reason": str(dry_run.get("reason") or "dry_run_admission_unavailable"),
            "admission_bypassed": False,
        }
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
    dry_run = decision.get("dry_run") or {}
    if dry_run:
        lines.append(
            "Dry-run admission: "
            f"{dry_run.get('status', 'unknown')} "
            f"({dry_run.get('reason', 'no_reason')})."
        )
        selected = dry_run.get("selected_issue") or {}
        if selected:
            lines.append(
                "Selected issue: "
                f"{selected.get('identifier')} — {selected.get('title', '')}".rstrip()
            )
        else:
            lines.append("Selected issue: none")
        would_lock = dry_run.get("would_create_work_state") or {}
        if would_lock:
            lines.append(f"Would-lock id: {would_lock.get('work_id')}")
        else:
            lines.append("Would-lock id: none")
        would_goal = dry_run.get("would_goal_contract") or {}
        if would_goal:
            lines.append(f"Would-contract: {would_goal.get('summary')}")
        else:
            lines.append("Would-contract: none")
        if dry_run.get("status") == "would_run":
            lines.append("Controller would: run after admission materializes the lock/goal contract.")
        elif dry_run.get("status") == "paused":
            lines.append("Controller would: pause; OFF intent prevents queue admission.")
        else:
            lines.append("Controller would: refuse/no-op until the blocker is resolved.")
        lines.append("Dry-run side effects: work_state_written=false, executor_spawned=false, linear_mutated=false.")
    lines.append(
        "Decision: "
        f"{admission.get('status', 'unknown')} "
        f"({admission.get('reason', 'no_reason')})."
    )
    lines.append("Side effects: executor_spawned=false, linear_done_mutated=false.")
    return "\n".join(lines)


def _format_runtime_result_message(command: AutopilotCommand, materialization: Mapping[str, Any]) -> str:
    action = command.action.replace("_", "-")
    target = f" {command.target_id}" if command.target_id else ""
    lines = [f"/autopilot {action}{target}"]
    lines.append("Mode: execution — live admission must materialize lock/goal before executor kickoff.")
    lines.append(
        "Decision: "
        f"{materialization.get('status', 'unknown')} "
        f"({materialization.get('reason', 'no_reason')})."
    )
    selected = materialization.get("selected_issue") or {}
    if selected:
        lines.append(
            "Selected issue: "
            f"{selected.get('identifier')} — {selected.get('title', '')}".rstrip()
        )
    else:
        lines.append("Selected issue: none")
    work_id = materialization.get("work_id")
    owner_session_id = materialization.get("owner_session_id")
    if work_id:
        lines.append(f"work_state lock: {work_id} owner_session={owner_session_id}")
    else:
        lines.append("work_state lock: none")
    executor_session_id = materialization.get("executor_session_id")
    if executor_session_id:
        lines.append(f"Executor session: {executor_session_id}")
    else:
        lines.append("Executor session: none")
    lines.append(
        "Side effects: "
        f"work_state_written={str(bool(materialization.get('work_state_written'))).lower()}, "
        f"executor_spawned={str(bool(materialization.get('executor_spawned'))).lower()}, "
        f"linear_done_mutated={str(bool(materialization.get('linear_done_mutated'))).lower()}."
    )
    if materialization.get("status") == "started":
        lines.append("Closeout guard: Linear Done remains forbidden until executor evidence verifies the Done criteria.")
    else:
        dry_run = materialization.get("dry_run") or {}
        blocker = dry_run.get("reason") or materialization.get("reason")
        candidates = dry_run.get("candidates") if isinstance(dry_run, Mapping) else None
        if candidates:
            for candidate in candidates[:5]:
                candidate_id = candidate.get("identifier") or "unknown"
                candidate_reason = candidate.get("reason") or "unknown"
                eligible = str(bool(candidate.get("eligible"))).lower()
                lines.append(
                    f"Admission candidate: {candidate_id} eligible={eligible} reason={candidate_reason}"
                )
        if blocker:
            lines.append(f"Blocked/no-op reason: {blocker}")
    return "\n".join(lines)


def run_autopilot_once(
    *,
    state_store: Optional[AutopilotStateStore] = None,
    work_state_store: Any = None,
    linear_client: Optional[LinearIssueClient] = None,
    executor_spawner: Optional[Callable[..., Any]] = None,
    actor: Optional[str] = None,
    owner_session_id: Optional[str] = None,
    target_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Materialize at most one admitted autopilot task.

    This is the CH-389 controller boundary below the slash-command entrypoint:
    it reuses dry-run admission, writes exactly one work_state lock, then calls a
    caller-supplied executor spawner with the generated `/goal` contract.  It
    never mutates Linear Done and never loops to a second task.
    """

    event_at = now or _utcnow()
    store = state_store or AutopilotStateStore()
    state = store.status()
    if not target_id and not bool(state.get("enabled")):
        return {
            "status": "paused",
            "reason": "controller_disabled_noop",
            "selected_issue": None,
            "work_id": None,
            "owner_session_id": owner_session_id,
            "work_state_written": False,
            "executor_spawned": False,
            "linear_done_mutated": False,
            "generated_at": event_at.isoformat(),
        }
    if executor_spawner is None:
        return {
            "status": "blocked",
            "reason": "executor_spawner_missing_fail_closed",
            "selected_issue": None,
            "work_id": None,
            "owner_session_id": owner_session_id,
            "work_state_written": False,
            "executor_spawned": False,
            "linear_done_mutated": False,
            "generated_at": event_at.isoformat(),
        }

    client = linear_client or EnvLinearIssueClient()
    command = AutopilotCommand(
        action=ACTION_DRY_RUN,
        target_id=target_id.upper() if target_id else None,
        raw_args=(f"dry-run {target_id.upper()}" if target_id else "dry-run"),
    )
    dry_run = evaluate_dry_run_admission(
        command=command,
        state=state,
        linear_client=client,
        work_state_store=work_state_store,
    )
    if dry_run.get("status") != "would_run":
        return {
            "status": str(dry_run.get("status") or "blocked"),
            "reason": str(dry_run.get("reason") or "admission_refused"),
            "selected_issue": dry_run.get("selected_issue"),
            "work_id": None,
            "owner_session_id": owner_session_id,
            "dry_run": dry_run,
            "work_state_written": False,
            "executor_spawned": False,
            "linear_done_mutated": False,
            "generated_at": event_at.isoformat(),
        }

    selected_issue = dry_run.get("selected_issue") or {}
    work_id = str(selected_issue.get("identifier") or "").strip()
    if not work_id:
        return {
            "status": "blocked",
            "reason": "selected_issue_missing_identifier_fail_closed",
            "selected_issue": selected_issue,
            "work_id": None,
            "owner_session_id": owner_session_id,
            "dry_run": dry_run,
            "work_state_written": False,
            "executor_spawned": False,
            "linear_done_mutated": False,
            "generated_at": event_at.isoformat(),
        }
    owner_session = owner_session_id or f"autopilot:{work_id}"
    goal = dry_run.get("would_goal_contract") or {}
    goal_contract = str(goal.get("command") or "")
    if not goal_contract.startswith("/goal"):
        return {
            "status": "blocked",
            "reason": "goal_contract_unavailable_fail_closed",
            "selected_issue": selected_issue,
            "work_id": work_id,
            "owner_session_id": owner_session,
            "dry_run": dry_run,
            "work_state_written": False,
            "executor_spawned": False,
            "linear_done_mutated": False,
            "generated_at": event_at.isoformat(),
        }

    from gateway.work_state import WorkRecord

    record = WorkRecord(
        work_id=work_id,
        title=str(selected_issue.get("title") or work_id),
        objective=str(goal.get("summary") or f"Execute Linear {work_id}"),
        owner="hermes",
        executor="hermes",
        mode="autopilot",
        owner_session_id=owner_session,
        state="created",
        started_at=event_at,
        last_progress_at=event_at,
        next_action="Spawn selected executor from generated /goal contract",
        proof="work_state lock created before executor spawn",
    )
    if work_state_store is None or not hasattr(work_state_store, "upsert"):
        return {
            "status": "blocked",
            "reason": "work_state_store_missing_upsert_fail_closed",
            "selected_issue": selected_issue,
            "work_id": work_id,
            "owner_session_id": owner_session,
            "dry_run": dry_run,
            "work_state_written": False,
            "executor_spawned": False,
            "linear_done_mutated": False,
            "generated_at": event_at.isoformat(),
        }
    work_state_store.upsert(record)

    try:
        spawn_result = executor_spawner(
            goal_contract=goal_contract,
            work_id=work_id,
            owner_session_id=owner_session,
            selected_issue=selected_issue,
            actor=actor,
            dry_run=dry_run,
        )
    except Exception as exc:  # fail closed and leave an auditable blocked record
        blocked_reason = f"executor_spawn_failed:{type(exc).__name__}:{exc}"
        if hasattr(work_state_store, "update_record"):
            work_state_store.update_record(
                work_id,
                owner_session,
                state="blocked",
                last_progress_at=event_at,
                blocked_reason=blocked_reason,
                proof=blocked_reason,
                usable_outcome="blocked",
                close_disposition="close",
                next_action="Executor spawn failed; operator review required before retry",
            )
        return {
            "status": "blocked",
            "reason": "executor_spawn_failed",
            "selected_issue": selected_issue,
            "work_id": work_id,
            "owner_session_id": owner_session,
            "dry_run": dry_run,
            "work_state_written": True,
            "executor_spawned": False,
            "linear_done_mutated": False,
            "error": blocked_reason,
            "generated_at": event_at.isoformat(),
        }

    spawn_payload = spawn_result if isinstance(spawn_result, Mapping) else {"result": spawn_result}
    executor_session_id = spawn_payload.get("session_id") or spawn_payload.get("executor_session_id")
    proof = str(spawn_payload.get("proof") or "executor spawned from generated /goal contract")
    if hasattr(work_state_store, "update_record"):
        work_state_store.update_record(
            work_id,
            owner_session,
            state="running",
            executor_session_id=str(executor_session_id) if executor_session_id else None,
            proof=proof,
            last_progress_at=event_at,
            next_action="Await executor evidence; do not mark Linear Done without verification",
        )
    return {
        "status": "started",
        "reason": "one_task_autopilot_started",
        "selected_issue": selected_issue,
        "work_id": work_id,
        "owner_session_id": owner_session,
        "executor_session_id": executor_session_id,
        "dry_run": dry_run,
        "work_state_written": True,
        "executor_spawned": True,
        "linear_done_mutated": False,
        "spawn_result": spawn_payload,
        "generated_at": event_at.isoformat(),
    }


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
    target_issue: Optional[Mapping[str, Any]] = None
    client = linear_client or EnvLinearIssueClient()
    if command.target_id:
        target_issue = client.fetch_issue(command.target_id)
        linear = dict(classify_linear_target(target_issue, command.target_id))
        work_state = classify_work_state_target(command.target_id, work_state_store)
    else:
        work_state = summarize_work_state(work_state_store)

    dry_run: Optional[dict[str, Any]] = None
    if command.action == ACTION_DRY_RUN:
        dry_run = evaluate_dry_run_admission(
            command=command,
            state=state,
            linear_client=client,
            work_state_store=work_state_store,
            target_issue=target_issue,
            target_classification=linear,
        )

    admission = _admission_decision(command=command, state=state, linear=linear, dry_run=dry_run)
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
    if dry_run is not None:
        decision["dry_run"] = dry_run
    return AutopilotResult(
        ok=True,
        command=command,
        message=_format_result_message(decision),
        decision=decision,
    )


def handle_autopilot_runtime_command(
    raw_args: str,
    *,
    actor: Optional[str] = None,
    state_store: Optional[AutopilotStateStore] = None,
    work_state_store: Any = None,
    linear_client: Optional[LinearIssueClient] = None,
    executor_spawner: Optional[Callable[..., Any]] = None,
    owner_session_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> AutopilotResult:
    """Handle the live operator /autopilot path.

    Read-only/status/OFF commands keep the CH-385 fail-closed controller
    behavior.  Execution commands (``ON`` and targeted one-shot ``CH-123``)
    must traverse ``run_autopilot_once`` so the user-facing entrypoint proves
    the same path as the materialization helper: live admission -> work_state
    lock -> generated /goal contract -> executor spawner.  Linear Done remains
    forbidden here.
    """

    try:
        command = parse_autopilot_args(raw_args)
    except AutopilotParseError as exc:
        return handle_autopilot_command(
            raw_args,
            actor=actor,
            state_store=state_store,
            work_state_store=work_state_store,
            linear_client=linear_client,
            now=now,
        )

    if command.action in {ACTION_STATUS, ACTION_DRY_RUN, ACTION_DISABLE}:
        return handle_autopilot_command(
            raw_args,
            actor=actor,
            state_store=state_store,
            work_state_store=work_state_store,
            linear_client=linear_client,
            now=now,
        )

    store = state_store or AutopilotStateStore()
    state_written = False
    if command.action == ACTION_ENABLE:
        store.set_enabled(True, actor=actor, now=now)
        state_written = True

    materialization = run_autopilot_once(
        state_store=store,
        work_state_store=work_state_store,
        linear_client=linear_client,
        executor_spawner=executor_spawner,
        actor=actor,
        owner_session_id=owner_session_id,
        target_id=command.target_id,
        now=now,
    )
    decision: dict[str, Any] = {
        "ok": materialization.get("status") == "started",
        "fail_closed": materialization.get("status") != "started",
        "command": {
            "action": command.action,
            "target_id": command.target_id,
            "read_only": False,
        },
        "materialization": materialization,
        "side_effects": {
            "state_written": state_written,
            "work_state_written": bool(materialization.get("work_state_written")),
            "executor_spawned": bool(materialization.get("executor_spawned")),
            "linear_done_mutated": bool(materialization.get("linear_done_mutated")),
        },
        "generated_at": (now or _utcnow()).isoformat(),
    }
    return AutopilotResult(
        ok=bool(decision["ok"]),
        command=command,
        fail_closed=bool(decision["fail_closed"]),
        message=_format_runtime_result_message(command, materialization),
        decision=decision,
    )


def plugin_command_entrypoint(raw_args: str) -> str:
    """Plugin-compatible fail-closed slash-command entrypoint."""

    return handle_autopilot_command(raw_args).message
