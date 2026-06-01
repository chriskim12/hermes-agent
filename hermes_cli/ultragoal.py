"""Hermes Ultragoal source-compatible durable goal artifacts.

This module ports the durable parts of oh-my-codex Ultragoal into Hermes:
source-compatible goal/story state, JSON/JSONL artifacts, steering audit, final
quality gate reconciliation, and a shell CLI surface. Hermes keeps Kanban as the
authority SSOT; these artifacts are executor evidence, not task authority.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RUN_ID = "default"
DEFAULT_AGGREGATE_OBJECTIVE = (
    "Complete the Hermes Ultragoal run by following the durable story ledger in "
    ".hermes/ultragoal/runs/<run-id>/goals.json. Do not clear /goal hidden state; "
    "checkpoint progress through Ultragoal artifacts and reconcile with Hermes /goal snapshots."
)
VALID_GOAL_STATUSES = {
    "pending",
    "in_progress",
    "complete",
    "failed",
    "review_blocked",
    "needs_user_decision",
}
VALID_STEERING_KINDS = {
    "add_subgoal",
    "split_subgoal",
    "reorder_pending",
    "revise_pending_wording",
    "annotate_ledger",
    "mark_blocked_superseded",
}
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_run_id(run_id: str) -> str:
    if not _SAFE_RUN_ID_RE.match(run_id):
        raise ValueError("run_id must be 1-128 chars of letters, digits, dot, underscore, or dash; path separators are forbidden")
    return run_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "goal"


def _run_root(workdir: Path, run_id: str) -> Path:
    run_id = _validate_run_id(run_id)
    return workdir / ".hermes" / "ultragoal" / "runs" / run_id


def _artifact_path(run_id: str, name: str) -> str:
    run_id = _validate_run_id(run_id)
    return f".hermes/ultragoal/runs/{run_id}/{name}"


@dataclass
class UltragoalItem:
    id: str
    title: str
    objective: str
    status: str = "pending"
    token_budget: int | None = None
    attempt: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    review_blocked_at: str | None = None
    evidence: str | None = None
    failure_reason: str | None = None
    steering_status: str | None = None
    superseded_by: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    blocker_signature: str | None = None
    blocker_occurrence_count: int | None = None
    required_external_decision: str | None = None
    non_retriable: bool = False
    steering_evidence: str | None = None
    steering_rationale: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return _snake_to_camel_dict(data)

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "UltragoalItem":
        snake = _camel_to_snake_dict(data)
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: snake[k] for k in snake if k in allowed})


@dataclass
class UltragoalPlan:
    version: int = 1
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    brief_path: str = _artifact_path(DEFAULT_RUN_ID, "brief.md")
    goals_path: str = _artifact_path(DEFAULT_RUN_ID, "goals.json")
    ledger_path: str = _artifact_path(DEFAULT_RUN_ID, "ledger.jsonl")
    codex_goal_mode: str = "aggregate"
    codex_objective: str = DEFAULT_AGGREGATE_OBJECTIVE
    codex_objective_aliases: list[str] = field(default_factory=list)
    aggregate_completion: dict[str, Any] | None = None
    active_goal_id: str | None = None
    goals: list[UltragoalItem] = field(default_factory=list)
    brief: str = field(default="", repr=False, compare=False)

    def to_json_dict(self) -> dict[str, Any]:
        data = _snake_to_camel_dict(asdict(self))
        data.pop("brief", None)
        data["goals"] = [g.to_json_dict() for g in self.goals]
        return data

    @classmethod
    def from_json_dict(cls, data: dict[str, Any], *, brief: str = "") -> "UltragoalPlan":
        snake = _camel_to_snake_dict(data)
        goals = [UltragoalItem.from_json_dict(g) for g in data.get("goals", [])]
        allowed = cls.__dataclass_fields__.keys() - {"goals", "brief"}
        kwargs = {k: snake[k] for k in snake if k in allowed}
        return cls(**kwargs, goals=goals, brief=brief)


def _snake_to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def _snake_to_camel_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {_snake_to_camel(k): v for k, v in data.items() if v not in (None, [], {})}


def _camel_to_snake_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {_camel_to_snake(k): v for k, v in data.items()}


def parse_goals_from_brief(brief: str) -> list[tuple[str, str]]:
    """Derive story goals from a brief, preferring `Story:` sections."""
    lines = brief.splitlines()
    stories: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    story_re = re.compile(r"^#{1,6}\s*(?:story|goal)\s*:?\s*(.+)$", re.I)
    for line in lines:
        m = story_re.match(line.strip())
        if m:
            if current:
                stories.append(current)
            current = (m.group(1).strip(), [])
        elif current is not None:
            current[1].append(line)
    if current:
        stories.append(current)
    if stories:
        return [(title, "\n".join(body).strip() or title) for title, body in stories]

    bullets: list[tuple[str, str]] = []
    for line in lines:
        m = re.match(r"^\s*[-*]\s+(?:\[[ xX]\]\s*)?(.+?)\s*$", line)
        if m:
            text = m.group(1).strip()
            bullets.append((text, text))
    if bullets:
        return _dedupe_goals(bullets)
    text = brief.strip() or "Untitled goal"
    return [(text.splitlines()[0][:80], text)]


def _dedupe_goals(goals: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Normalize goal tuples without dropping source-authored stories.

    Original Ultragoal stories are durable user intent. Even if two titles
    slugify to the same value, the sequence prefix keeps ids unique, so dropping
    either item would silently lose scope.
    """
    return [(title, objective) for title, objective in goals]


def _quality_gate_passed(gate: dict[str, Any] | None) -> bool:
    if not isinstance(gate, dict):
        return False
    if ((gate.get("aiSlopCleaner") or {}).get("status") != "passed"):
        return False
    if ((gate.get("verification") or {}).get("status") != "passed"):
        return False
    review = gate.get("codeReview") or {}
    if review.get("recommendation") != "APPROVE" or review.get("architectStatus") != "CLEAR":
        return False
    independent = review.get("independentReview") or {}
    return bool(independent.get("codeReviewer") and independent.get("architect"))


def _plan_run_id(plan: UltragoalPlan) -> str:
    parts = Path(plan.goals_path).parts
    if "runs" in parts:
        idx = len(parts) - 1 - parts[::-1].index("runs")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return DEFAULT_RUN_ID


def reconcile_kanban_authority(plan: UltragoalPlan, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the Kanban-authority snapshot for an Ultragoal run.

    Kanban remains the authority SSOT. The Ultragoal ledger can only serve as
    executor evidence when a caller supplies a current Kanban snapshot that
    names the same run/card and routes through the direct Kanban lane. Missing
    or mismatched snapshots fail closed instead of inventing authority from
    local artifacts.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Kanban authority snapshot is required")
    expected_run_id = _plan_run_id(plan)
    actual_run_id = snapshot.get("runId") or snapshot.get("run_id") or snapshot.get("taskId") or snapshot.get("task_id")
    if actual_run_id != expected_run_id:
        raise ValueError(f"Kanban authority runId mismatch: expected {expected_run_id}, got {actual_run_id}")
    routing = snapshot.get("routingVerdict") or snapshot.get("routing_verdict")
    if routing != "direct-kanban":
        raise ValueError(f"Kanban authority routing mismatch: {routing}")
    authority = snapshot.get("authority")
    if authority != "kanban":
        raise ValueError(f"Kanban authority snapshot must explicitly declare authority=kanban, got {authority}")
    return {
        "authority": "kanban",
        "runId": expected_run_id,
        "status": snapshot.get("status"),
        "routingVerdict": routing,
        "taskId": snapshot.get("taskId") or snapshot.get("task_id"),
    }


class UltragoalStore:
    def __init__(
        self,
        workdir: str | Path = ".",
        run_id: str = DEFAULT_RUN_ID,
        kanban_snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.workdir = Path(workdir)
        self.run_id = run_id
        self.root = _run_root(self.workdir, run_id)
        self.brief_path = self.root / "brief.md"
        self.goals_path = self.root / "goals.json"
        self.ledger_path = self.root / "ledger.jsonl"
        self.lock_path = self.root / ".mutation.lock"
        self.kanban_snapshot = kanban_snapshot

    def _require_kanban_authority(self, plan: UltragoalPlan | None = None) -> dict[str, Any]:
        authority_plan = plan or UltragoalPlan(
            brief_path=_artifact_path(self.run_id, "brief.md"),
            goals_path=_artifact_path(self.run_id, "goals.json"),
            ledger_path=_artifact_path(self.run_id, "ledger.jsonl"),
        )
        return reconcile_kanban_authority(authority_plan, self.kanban_snapshot)

    def create_plan(
        self,
        *,
        brief: str,
        goals: list[tuple[str, str]] | None = None,
        force: bool = False,
        codex_goal_mode: str = "aggregate",
    ) -> UltragoalPlan:
        if self.goals_path.exists() and not force:
            raise FileExistsError(f"Ultragoal plan already exists at {self.goals_path}; pass --force to overwrite")
        if codex_goal_mode not in {"aggregate", "per_story"}:
            raise ValueError("codex_goal_mode must be aggregate or per_story")
        self.root.mkdir(parents=True, exist_ok=True)
        parsed = _dedupe_goals(goals or parse_goals_from_brief(brief))
        now = _now()
        items = [
            UltragoalItem(
                id=f"G{i:03d}-{_slug(title)}",
                title=title,
                objective=objective,
                created_at=now,
                updated_at=now,
            )
            for i, (title, objective) in enumerate(parsed, start=1)
        ]
        plan = UltragoalPlan(
            created_at=now,
            updated_at=now,
            brief_path=_artifact_path(self.run_id, "brief.md"),
            goals_path=_artifact_path(self.run_id, "goals.json"),
            ledger_path=_artifact_path(self.run_id, "ledger.jsonl"),
            codex_goal_mode=codex_goal_mode,
            codex_objective=DEFAULT_AGGREGATE_OBJECTIVE if codex_goal_mode == "aggregate" else "",
            goals=items,
            brief=brief,
        )
        self._require_kanban_authority(plan)
        self.brief_path.write_text(brief, encoding="utf-8")
        self.save_plan(plan)
        self.ledger_path.write_text("", encoding="utf-8")
        self.append_ledger("plan_created", message=f"created {len(items)} ultragoal item(s)")
        return plan

    def load_plan(self) -> UltragoalPlan:
        if not self.goals_path.exists():
            raise FileNotFoundError(f"No Ultragoal plan at {self.goals_path}")
        data = json.loads(self.goals_path.read_text(encoding="utf-8"))
        brief = self.brief_path.read_text(encoding="utf-8") if self.brief_path.exists() else ""
        return UltragoalPlan.from_json_dict(data, brief=brief)

    def save_plan(self, plan: UltragoalPlan) -> None:
        plan.updated_at = _now()
        self.root.mkdir(parents=True, exist_ok=True)
        self.goals_path.write_text(json.dumps(plan.to_json_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def append_ledger(self, event: str, **payload: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        entry = {"ts": _now(), "event": event, **{k: v for k, v in payload.items() if v is not None}}
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _ledger_entries(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(line) for line in self.ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def start_next_goal(self, *, retry_failed: bool = False) -> UltragoalItem | None:
        plan = self.load_plan()
        self._require_kanban_authority(plan)
        active = next((g for g in plan.goals if g.status == "in_progress"), None)
        if active:
            self.append_ledger("goal_resumed", goalId=active.id, status=active.status)
            return active
        candidates = []
        for g in plan.goals:
            if g.steering_status in {"blocked", "superseded"}:
                continue
            if g.status == "pending" or (retry_failed and g.status == "failed" and not g.non_retriable):
                candidates.append(g)
        if not candidates:
            return None
        goal = candidates[0]
        goal.status = "in_progress"
        goal.attempt += 1
        goal.started_at = goal.started_at or _now()
        goal.updated_at = _now()
        plan.active_goal_id = goal.id
        self.save_plan(plan)
        self.append_ledger("goal_started", goalId=goal.id, status=goal.status)
        return goal

    def checkpoint(
        self,
        *,
        goal_id: str,
        status: str,
        evidence: str,
        hermes_goal_snapshot: dict[str, Any] | None = None,
        quality_gate: dict[str, Any] | None = None,
    ) -> UltragoalPlan:
        if not evidence.strip():
            raise ValueError("checkpoint evidence is required")
        plan = self.load_plan()
        self._require_kanban_authority(plan)
        goal = self._find_goal(plan, goal_id)
        if status not in {"complete", "failed", "blocked"}:
            raise ValueError("checkpoint status must be complete, failed, or blocked")
        if status == "complete":
            if not isinstance(hermes_goal_snapshot, dict):
                raise ValueError("complete checkpoint requires a fresh Hermes goal snapshot")
            is_final = self._is_final_unresolved(plan, goal_id)
            if is_final:
                if not _quality_gate_passed(quality_gate):
                    raise ValueError("final completion requires a passing quality gate JSON")
                if hermes_goal_snapshot.get("status") != "complete":
                    raise ValueError("final completion requires a complete Hermes goal snapshot")
            elif hermes_goal_snapshot.get("status") not in {"active", "complete"}:
                raise ValueError("complete checkpoint requires active or complete Hermes goal snapshot")
            goal.status = "complete"
            goal.completed_at = _now()
            goal.evidence = evidence
            plan.active_goal_id = None if plan.active_goal_id == goal_id else plan.active_goal_id
            if is_final:
                plan.aggregate_completion = {
                    "status": "complete",
                    "completedAt": _now(),
                    "evidence": evidence,
                    "codexGoal": hermes_goal_snapshot,
                }
                event = "aggregate_completed"
            else:
                event = "goal_completed"
        elif status == "failed":
            goal.status = "failed"
            goal.failed_at = _now()
            goal.failure_reason = evidence
            if plan.active_goal_id == goal_id:
                plan.active_goal_id = None
            event = "goal_failed"
        else:
            goal.status = "failed"
            goal.failed_at = _now()
            goal.steering_status = "blocked"
            goal.blocked_reason = evidence
            goal.evidence = evidence
            if plan.active_goal_id == goal_id:
                plan.active_goal_id = None
            event = "goal_blocked"
        goal.updated_at = _now()
        self.save_plan(plan)
        self.append_ledger(event, goalId=goal.id, status=goal.status, evidence=evidence, qualityGate=quality_gate, codexGoal=hermes_goal_snapshot)
        return plan

    def apply_steering(self, directive: dict[str, Any]) -> UltragoalPlan:
        kind = directive.get("kind")
        evidence = str(directive.get("evidence") or "").strip()
        rationale = str(directive.get("rationale") or "").strip()
        idem = directive.get("idempotencyKey") or directive.get("idempotency_key")
        try:
            if kind not in VALID_STEERING_KINDS:
                raise ValueError(f"unknown steering kind: {kind}")
            if not evidence or not rationale:
                raise ValueError("steering evidence and rationale are required")
            if self._seen_idempotency_key(str(idem)):
                return self.load_plan()
            plan = self.load_plan()
            self._require_kanban_authority(plan)
            if kind == "add_subgoal":
                title = str(directive.get("title") or "").strip()
                objective = str(directive.get("objective") or "").strip()
                if not title or not objective:
                    raise ValueError("add_subgoal requires title and objective")
                next_num = len(plan.goals) + 1
                plan.goals.append(
                    UltragoalItem(
                        id=f"G{next_num:03d}-{_slug(title)}",
                        title=title,
                        objective=objective,
                        steering_evidence=evidence,
                        steering_rationale=rationale,
                    )
                )
            elif kind == "annotate_ledger":
                message = str(directive.get("message") or directive.get("note") or "").strip()
                if not message:
                    raise ValueError("annotate_ledger requires message")
                self.append_ledger("ledger_annotated", message=message, evidence=evidence, steering=directive)
            else:
                # Conservative first port: audit accepted structured mutations but
                # only mutate add_subgoal until each complex mutation has dedicated tests.
                raise ValueError(f"steering kind {kind} is not implemented in Hermes port yet")
            self.save_plan(plan)
            self.append_ledger("steering_accepted", steering=directive, mutationKind=kind, idempotencyKey=idem)
            return plan
        except ValueError as exc:
            self.append_ledger("steering_rejected", steering=directive, mutationKind=kind, idempotencyKey=idem, message=str(exc))
            raise

    def record_review_blockers(
        self,
        *,
        goal_id: str,
        title: str,
        objective: str,
        evidence: str,
        hermes_goal_snapshot: dict[str, Any] | None = None,
    ) -> UltragoalPlan:
        if not evidence.strip():
            raise ValueError("review blocker evidence is required")
        plan = self.load_plan()
        self._require_kanban_authority(plan)
        if not self._is_final_unresolved(plan, goal_id):
            raise ValueError("record-review-blockers is only valid for the final unresolved goal")
        goal = self._find_goal(plan, goal_id)
        goal.status = "review_blocked"
        goal.review_blocked_at = _now()
        goal.evidence = evidence
        blocker = UltragoalItem(
            id=f"G{len(plan.goals)+1:03d}-{_slug(title)}",
            title=title,
            objective=objective,
            steering_evidence=evidence,
        )
        plan.goals.append(blocker)
        plan.active_goal_id = None
        self.save_plan(plan)
        self.append_ledger("goal_review_blocked", goalId=goal_id, evidence=evidence, codexGoal=hermes_goal_snapshot)
        self.append_ledger("goal_added", goalId=blocker.id, evidence=evidence)
        return plan

    def status(self) -> dict[str, Any]:
        plan = self.load_plan()
        counts: dict[str, int] = {}
        for g in plan.goals:
            counts[g.status] = counts.get(g.status, 0) + 1
        blocking = [g.id for g in plan.goals if g.status not in {"complete"} and g.steering_status != "superseded"]
        return {
            "runId": self.run_id,
            "root": str(self.root),
            "codexGoalMode": plan.codex_goal_mode,
            "activeGoalId": plan.active_goal_id,
            "aggregateComplete": bool(plan.aggregate_completion),
            "counts": counts,
            "blockingGoalIds": blocking,
        }

    def _seen_idempotency_key(self, key: str | None) -> bool:
        if not key:
            return False
        return any(e.get("idempotencyKey") == key and e.get("event") == "steering_accepted" for e in self._ledger_entries())

    @staticmethod
    def _find_goal(plan: UltragoalPlan, goal_id: str) -> UltragoalItem:
        for goal in plan.goals:
            if goal.id == goal_id:
                return goal
        raise ValueError(f"unknown goal id: {goal_id}")

    @staticmethod
    def _is_final_unresolved(plan: UltragoalPlan, goal_id: str) -> bool:
        unresolved = [g for g in plan.goals if g.status != "complete" and g.steering_status != "superseded"]
        return len(unresolved) == 1 and unresolved[0].id == goal_id


def _json_arg(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return json.loads(raw)


def _read_brief(args: argparse.Namespace) -> str:
    if getattr(args, "brief_file", None):
        return Path(args.brief_file).read_text(encoding="utf-8")
    if getattr(args, "from_stdin", False):
        return sys.stdin.read()
    return args.brief or ""


def _parse_goal_arg(raw: str) -> tuple[str, str]:
    if "::" not in raw:
        return raw.strip(), raw.strip()
    title, objective = raw.split("::", 1)
    return title.strip(), objective.strip()


def ultragoal_command(args: argparse.Namespace) -> int:
    snapshot = _json_arg(getattr(args, "kanban_snapshot_json", None))
    store = UltragoalStore(
        Path(getattr(args, "workdir", ".")),
        getattr(args, "run_id", DEFAULT_RUN_ID),
        kanban_snapshot=snapshot,
    )
    cmd = args.ultragoal_command
    if cmd in {"create", "create-goals"}:
        plan = store.create_plan(
            brief=_read_brief(args),
            goals=[_parse_goal_arg(g) for g in (args.goal or [])] or None,
            force=bool(args.force),
            codex_goal_mode=args.codex_goal_mode,
        )
        _emit(args, plan.to_json_dict(), f"created {len(plan.goals)} Ultragoal goal(s) at {store.root}")
        return 0
    if cmd in {"complete", "complete-goals", "next", "start-next"}:
        goal = store.start_next_goal(retry_failed=bool(args.retry_failed))
        _emit(args, goal.to_json_dict() if goal else {"done": True}, goal and f"active {goal.id}: {goal.title}" or "no schedulable goal")
        return 0
    if cmd == "checkpoint":
        plan = store.checkpoint(
            goal_id=args.goal_id,
            status=args.status,
            evidence=args.evidence,
            hermes_goal_snapshot=_json_arg(args.codex_goal_json),
            quality_gate=_json_arg(args.quality_gate_json),
        )
        _emit(args, plan.to_json_dict(), f"checkpointed {args.goal_id} as {args.status}")
        return 0
    if cmd == "steer":
        directive = _json_arg(args.directive_json) if args.directive_json else {
            "kind": args.kind,
            "evidence": args.evidence,
            "rationale": args.rationale,
            "title": args.title,
            "objective": args.objective,
            "idempotencyKey": args.idempotency_key,
        }
        plan = store.apply_steering(directive)
        _emit(args, plan.to_json_dict(), "steering accepted")
        return 0
    if cmd == "record-review-blockers":
        plan = store.record_review_blockers(
            goal_id=args.goal_id,
            title=args.title,
            objective=args.objective,
            evidence=args.evidence,
            hermes_goal_snapshot=_json_arg(args.codex_goal_json),
        )
        _emit(args, plan.to_json_dict(), "review blockers recorded")
        return 0
    if cmd == "status":
        _emit(args, store.status(), "")
        return 0
    raise SystemExit(f"unknown ultragoal command: {cmd}")


def _emit(args: argparse.Namespace, data: dict[str, Any], text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif text:
        print(text)
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def run_slash(rest: str) -> str:
    """Run `/ultragoal ...` from the interactive CLI/gateway slash path."""
    parser = argparse.ArgumentParser(prog="/ultragoal")
    sub = parser.add_subparsers(dest="cmd")
    build_parser(sub)
    argv = ["ultragoal", *shlex.split(rest or "status")]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            ns = parser.parse_args(argv)
            rc = ns.func(ns)
        except SystemExit as exc:
            rc = int(exc.code or 0) if isinstance(exc.code, int) else 2
    output = buf.getvalue().strip()
    if rc and not output:
        output = f"ultragoal exited with status {rc}"
    return output


def build_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "ultragoal",
        help="Source-compatible Ultragoal durable goal artifacts for Hermes",
        description=(
            "Manage Hermes Ultragoal artifacts. Commands include create-goals, "
            "complete-goals, checkpoint, steer, status, and record-review-blockers. "
            "aggregate mode is the default; Ultragoal does not call /goal clear automatically."
        ),
        epilog=(
            "Source contract: aggregate mode is the default; record-review-blockers "
            "preserves final review gates; Ultragoal never performs hidden /goal clear."
        ),
    )
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument(
        "--kanban-snapshot-json",
        help="Current Kanban authority snapshot; required for mutating commands and must route direct-kanban",
    )
    parser.add_argument("--json", action="store_true")
    subs = parser.add_subparsers(dest="ultragoal_command", required=True)

    def add_create(name: str) -> None:
        p = subs.add_parser(name, help="create Ultragoal artifacts")
        p.add_argument("--brief", default="")
        p.add_argument("--brief-file")
        p.add_argument("--from-stdin", action="store_true")
        p.add_argument("--goal", action="append")
        p.add_argument("--codex-goal-mode", choices=("aggregate", "per_story"), default="aggregate")
        p.add_argument("--force", action="store_true")

    add_create("create-goals")
    add_create("create")

    for name in ("complete-goals", "complete", "next", "start-next"):
        p = subs.add_parser(name, help="start or resume the next schedulable goal")
        p.add_argument("--retry-failed", action="store_true")

    p = subs.add_parser("checkpoint", help="record goal checkpoint with Hermes /goal reconciliation")
    p.add_argument("--goal-id", required=True)
    p.add_argument("--status", required=True, choices=("complete", "failed", "blocked"))
    p.add_argument("--evidence", required=True)
    p.add_argument("--codex-goal-json")
    p.add_argument("--quality-gate-json")

    p = subs.add_parser("steer", help="apply explicit evidence-backed steering")
    p.add_argument("--kind")
    p.add_argument("--evidence")
    p.add_argument("--rationale")
    p.add_argument("--title")
    p.add_argument("--objective")
    p.add_argument("--idempotency-key")
    p.add_argument("--directive-json")

    p = subs.add_parser("record-review-blockers", help="insert final review blocker resolution goal")
    p.add_argument("--goal-id", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--objective", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--codex-goal-json")

    subs.add_parser("status", help="show artifact-backed Ultragoal status")
    parser.set_defaults(func=ultragoal_command)
    return parser


__all__ = [
    "DEFAULT_AGGREGATE_OBJECTIVE",
    "UltragoalItem",
    "UltragoalPlan",
    "UltragoalStore",
    "build_parser",
    "parse_goals_from_brief",
    "reconcile_kanban_authority",
    "ultragoal_command",
    "run_slash",
]
