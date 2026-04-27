"""Gateway-scoped Hermes work-state tracking for targeted owner ingress.

This module keeps a small persistent ledger of live Hermes work records so
owner-ingress packets can be resolved against explicit work state rather than
broad chat/session heuristics.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
})
WORK_SESSION_WATCH_STATUSES = frozenset({"unwatched", "active", "inactive", "failed"})

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
    stopped_at: Optional[datetime] = None
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
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "native_event_metadata": self.native_event_metadata or {},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkSessionRecord":
        stopped_at = data.get("stopped_at")
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
            stopped_at=datetime.fromisoformat(stopped_at) if stopped_at else None,
            native_event_metadata=data.get("native_event_metadata") or {},
        )


class WorkSessionRegistry:
    """Hermes-owned registry for provider-native clawhip work sessions.

    clawhip stays the generic native-event router. This registry stores Hermes
    work-session linkage and lifecycle state from additive native-event metadata.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else get_hermes_home() / "gateway_work_sessions.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def ingest_clawhip_native_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {"status": "rejected", "reason": "invalid_native_event_payload"}

        provider = str(payload.get("provider") or "").strip()
        provider_session_id = str(
            payload.get("session_id") or payload.get("provider_session_id") or ""
        ).strip()
        event = str(payload.get("event") or payload.get("native_event") or "").strip()
        if not provider or not provider_session_id or not event:
            return {"status": "rejected", "reason": "missing_provider_session_or_event"}

        event_at = _parse_event_datetime(payload.get("timestamp") or payload.get("event_at"))
        metadata = _compact_native_metadata(payload)
        normalized_event = event.lower().replace("_", "-")

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
                watch_status = str(payload.get("watch_status") or "unwatched").strip() or "unwatched"
                if watch_status not in WORK_SESSION_WATCH_STATUSES:
                    watch_status = "unwatched"
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
                    watch_status=watch_status,
                    native_event_metadata=metadata,
                )
                self._records.append(record)
            else:
                if normalized_event in {"userpromptsubmit", "session.prompt-submitted"}:
                    existing.lifecycle_state = "waiting_user"
                elif normalized_event in {"stop", "sessionstop", "session-stopped", "session.stopped"}:
                    existing.lifecycle_state = "stopped"
                    existing.stopped_at = event_at
                    existing.watch_status = "inactive"
                elif existing.lifecycle_state in {"stopped", "resolved"}:
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

            if record.lifecycle_state not in WORK_SESSION_LIFECYCLE_STATES:
                return {"status": "rejected", "reason": "invalid_lifecycle_state"}
            self._save_locked()
            return {"status": "accepted", "reason": "registered", "record": WorkSessionRecord.from_dict(record.to_dict())}


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
