"""Read-only Kanban relation-type drift auditor.

The audit classifies parents by how their children are linked:

* ``DEPENDENCY_ONLY`` — every child is a blocking dependency.
* ``HIERARCHY_ONLY`` — every child is a non-blocking hierarchy child.
* ``MIXED`` — both relation types are present, making parent semantics ambiguous.

It opens the Kanban DB with SQLite ``mode=ro`` and only issues SELECTs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

RC_DEPENDENCY_ONLY = "DEPENDENCY_ONLY"
RC_HIERARCHY_ONLY = "HIERARCHY_ONLY"
RC_MIXED = "MIXED"
RC_ACTIVE_PARENT_GATING = "ACTIVE_PARENT_GATING"
RC_IDLE_GATING = "IDLE_GATING"

_TASK_SAFE_COLS = (
    "id",
    "title",
    "status",
    "assignee",
    "tenant",
    "priority",
    "workspace_kind",
    "created_at",
    "started_at",
    "completed_at",
)


@dataclass(frozen=True)
class ChildLink:
    child_id: str
    child_title: str
    child_status: str
    relation_type: str


@dataclass(frozen=True)
class ParentAuditEntry:
    parent_id: str
    parent_title: str
    parent_status: str
    classification: str
    reason_codes: list[str]
    children: list[ChildLink]
    dependency_count: int
    hierarchy_count: int
    assignee: str | None = None
    tenant: str | None = None
    priority: int = 0
    child_status_summary: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DriftReport:
    generated_at: str
    board: str
    total_links: int
    total_parents: int
    dependency_only: list[ParentAuditEntry]
    hierarchy_only: list[ParentAuditEntry]
    mixed: list[ParentAuditEntry]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        def child_to_dict(child: ChildLink) -> dict[str, Any]:
            return {
                "child_id": child.child_id,
                "child_title": child.child_title,
                "child_status": child.child_status,
                "relation_type": child.relation_type,
            }

        def entry_to_dict(entry: ParentAuditEntry) -> dict[str, Any]:
            return {
                "parent_id": entry.parent_id,
                "parent_title": entry.parent_title,
                "parent_status": entry.parent_status,
                "classification": entry.classification,
                "reason_codes": list(entry.reason_codes),
                "dependency_count": entry.dependency_count,
                "hierarchy_count": entry.hierarchy_count,
                "assignee": entry.assignee,
                "tenant": entry.tenant,
                "priority": entry.priority,
                "child_status_summary": dict(entry.child_status_summary),
                "children": [child_to_dict(child) for child in entry.children],
            }

        return {
            "generated_at": self.generated_at,
            "board": self.board,
            "total_links": self.total_links,
            "total_parents": self.total_parents,
            "dependency_only": [entry_to_dict(entry) for entry in self.dependency_only],
            "hierarchy_only": [entry_to_dict(entry) for entry in self.hierarchy_only],
            "mixed": [entry_to_dict(entry) for entry in self.mixed],
            "summary": dict(self.summary),
        }


def _kanban_db_path(board: str | None = None) -> str:
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return os.path.expanduser(override)

    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root()
    slug = board or os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if not slug:
        current = root / "kanban" / "current"
        try:
            if current.exists():
                slug = current.read_text(encoding="utf-8").strip()
        except OSError:
            slug = ""
    if not slug or slug == "default":
        return str(root / "kanban.db")
    return str(root / "kanban" / "boards" / slug / "kanban.db")


def _clean_title(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if not text:
        return "(untitled)"
    return text[:117] + "..." if len(text) > 120 else text


def classify_parent(parent_id: str, children_rows: list[tuple[Any, ...]], parent_info: dict[str, Any]) -> ParentAuditEntry:
    children: list[ChildLink] = []
    dependency_count = 0
    hierarchy_count = 0
    status_summary: dict[str, int] = defaultdict(int)

    for child_id, child_title, child_status, relation_type in children_rows:
        relation = str(relation_type or "")
        child = ChildLink(
            child_id=str(child_id),
            child_title=_clean_title(child_title),
            child_status=str(child_status or "unknown"),
            relation_type=relation,
        )
        children.append(child)
        status_summary[child.child_status] = status_summary.get(child.child_status, 0) + 1
        if relation == "dependency":
            dependency_count += 1
        elif relation == "hierarchy":
            hierarchy_count += 1

    reason_codes: list[str]
    if dependency_count and not hierarchy_count:
        classification = RC_DEPENDENCY_ONLY
        reason_codes = [RC_DEPENDENCY_ONLY]
    elif hierarchy_count and not dependency_count:
        classification = RC_HIERARCHY_ONLY
        reason_codes = [RC_HIERARCHY_ONLY]
    else:
        classification = RC_MIXED
        reason_codes = [RC_MIXED]

    parent_status = str(parent_info.get("status") or "unknown")
    if dependency_count:
        if parent_status == "done":
            reason_codes.append(RC_IDLE_GATING)
        else:
            reason_codes.append(RC_ACTIVE_PARENT_GATING)

    return ParentAuditEntry(
        parent_id=parent_id,
        parent_title=_clean_title(parent_info.get("title")),
        parent_status=parent_status,
        classification=classification,
        reason_codes=reason_codes,
        children=children,
        dependency_count=dependency_count,
        hierarchy_count=hierarchy_count,
        assignee=parent_info.get("assignee"),
        tenant=parent_info.get("tenant"),
        priority=int(parent_info.get("priority") or 0),
        child_status_summary=dict(status_summary),
    )


def run_audit(board: str | None = None) -> DriftReport:
    db_path = _kanban_db_path(board)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                l.parent_id,
                l.child_id,
                t.title AS child_title,
                t.status AS child_status,
                l.relation_type
            FROM task_links l
            JOIN tasks t ON t.id = l.child_id
            WHERE l.relation_type IN ('dependency', 'hierarchy')
            ORDER BY l.parent_id, l.child_id
            """
        ).fetchall()

        parent_children: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
        for row in rows:
            parent_children[str(row["parent_id"])].append(
                (row["child_id"], row["child_title"], row["child_status"], row["relation_type"])
            )

        parent_info: dict[str, dict[str, Any]] = {}
        if parent_children:
            parent_ids = list(parent_children)
            placeholders = ",".join("?" for _ in parent_ids)
            parent_rows = conn.execute(
                f"SELECT {', '.join(_TASK_SAFE_COLS)} FROM tasks WHERE id IN ({placeholders})",
                parent_ids,
            ).fetchall()
            parent_info = {str(row["id"]): dict(row) for row in parent_rows}

        dependency_only: list[ParentAuditEntry] = []
        hierarchy_only: list[ParentAuditEntry] = []
        mixed: list[ParentAuditEntry] = []
        for parent_id in sorted(parent_children):
            entry = classify_parent(
                parent_id,
                parent_children[parent_id],
                parent_info.get(parent_id, {"id": parent_id, "status": "unknown"}),
            )
            if entry.classification == RC_DEPENDENCY_ONLY:
                dependency_only.append(entry)
            elif entry.classification == RC_HIERARCHY_ONLY:
                hierarchy_only.append(entry)
            else:
                mixed.append(entry)

        total_links = sum(len(children) for children in parent_children.values())
        return DriftReport(
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            board=board or os.environ.get("HERMES_KANBAN_BOARD", "default") or "default",
            total_links=total_links,
            total_parents=len(parent_children),
            dependency_only=dependency_only,
            hierarchy_only=hierarchy_only,
            mixed=mixed,
            summary={
                "dependency_only_count": len(dependency_only),
                "hierarchy_only_count": len(hierarchy_only),
                "mixed_count": len(mixed),
                "total_parents_with_children": len(parent_children),
                "total_links": total_links,
                "dependency_children_total": sum(entry.dependency_count for entry in dependency_only + mixed),
                "hierarchy_children_total": sum(entry.hierarchy_count for entry in hierarchy_only + mixed),
                "active_gating_parents": sum(
                    1 for entry in dependency_only + mixed if RC_ACTIVE_PARENT_GATING in entry.reason_codes
                ),
            },
        )
    finally:
        conn.close()


def format_report_json(report: DriftReport) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)


def format_report_text(report: DriftReport) -> str:
    lines = [
        "KANBAN RELATION-TYPE DRIFT AUDIT (READ-ONLY)",
        f"Generated: {report.generated_at}",
        f"Board: {report.board}",
        f"Total links: {report.total_links}",
        f"Total parents: {report.total_parents}",
        f"DEPENDENCY_ONLY: {len(report.dependency_only)}",
        f"HIERARCHY_ONLY: {len(report.hierarchy_only)}",
        f"MIXED: {len(report.mixed)}",
        f"Active gating parents: {report.summary['active_gating_parents']}",
        "",
    ]
    for label, entries in (
        (RC_DEPENDENCY_ONLY, report.dependency_only),
        (RC_HIERARCHY_ONLY, report.hierarchy_only),
        (RC_MIXED, report.mixed),
    ):
        if not entries:
            continue
        lines.append(label)
        for entry in entries:
            lines.append(
                f"- {entry.parent_id} [{entry.parent_status}] {entry.parent_title} "
                f"deps={entry.dependency_count} hierarchy={entry.hierarchy_count} "
                f"reasons={','.join(entry.reason_codes)}"
            )
            for child in entry.children[:5]:
                lines.append(f"  -> {child.child_id} [{child.child_status}] [{child.relation_type}] {child.child_title}")
            if len(entry.children) > 5:
                lines.append(f"  ... and {len(entry.children) - 5} more")
        lines.append("")
    lines.append("AUDIT COMPLETE — NO MUTATIONS WERE PERFORMED")
    return "\n".join(lines)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "relation-drift",
        help="Read-only audit of dependency vs hierarchy Kanban child links",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("-b", "--board", default=None, help="Kanban board slug; defaults to active board")


def run_cli(args: argparse.Namespace) -> int:
    try:
        report = run_audit(board=getattr(args, "board", None))
    except Exception as exc:
        print(f"kanban relation-drift: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(format_report_json(report))
    else:
        print(format_report_text(report))
    return 0
