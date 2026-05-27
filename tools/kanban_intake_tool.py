"""Tool wrapper for reusable Kanban intake logic.

The handler accepts an already-parsed intake request and returns the structured
outcome from :mod:`hermes_cli.kanban_intake`.  It does not dispatch workers or
write Kanban records; callers receive a handoff payload they can review/admit
through the normal Kanban authority path.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Mapping

from hermes_cli.kanban_intake import evaluate_intake_request
from tools.registry import registry, tool_error


def _profile_has_kanban_toolset() -> bool:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        return "kanban" in (cfg.get("toolsets") or [])
    except Exception:
        return False


def _check_kanban_intake_available() -> bool:
    return bool(os.environ.get("HERMES_KANBAN_TASK")) or _profile_has_kanban_toolset()


def _kanban_available() -> bool:
    try:
        from hermes_cli import kanban_db as kb

        db_path = kb.kanban_db_path()
        if not db_path.is_file():
            return False
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def kanban_intake_tool(
    *,
    request: Mapping[str, Any],
    lifecycle: str = "interview",
    approve_admission: bool = False,
    kanban_available: bool | None = None,
) -> str:
    if not isinstance(request, Mapping):
        return tool_error("request must be an object containing parsed intake fields")
    available = _kanban_available() if kanban_available is None else bool(kanban_available)
    result = evaluate_intake_request(
        request,
        lifecycle=lifecycle,
        kanban_available=available,
        approve_admission=bool(approve_admission),
    )
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


KANBAN_INTAKE_SCHEMA = {
    "name": "kanban_intake",
    "description": (
        "Evaluate a parsed Kanban intake request through the reusable domain lifecycle. "
        "Returns structured outcomes and, when approved, a Kanban admission handoff request. "
        "Does not create cards, dispatch workers, restart gateways, mutate repos, or perform live side effects."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request": {
                "type": "object",
                "description": "Parsed intake request: goal, project, optional tenant/context/non_goals/acceptance_criteria/side_effect_boundary/open_questions/suggested_breakdown.",
                "additionalProperties": True,
            },
            "lifecycle": {
                "type": "string",
                "enum": ["interview", "draft", "admit"],
                "description": "Lifecycle phase to evaluate: interview asks questions, draft renders a Seed Contract, admit can emit an admission handoff if approved.",
                "default": "interview",
            },
            "approve_admission": {
                "type": "boolean",
                "description": "Approval to emit an admission handoff request only. This never approves executor dispatch or live side effects.",
                "default": False,
            },
        },
        "required": ["request"],
    },
}


registry.register(
    name="kanban_intake",
    toolset="kanban",
    schema=KANBAN_INTAKE_SCHEMA,
    handler=lambda args, **kw: kanban_intake_tool(
        request=args.get("request"),
        lifecycle=args.get("lifecycle", "interview"),
        approve_admission=args.get("approve_admission", False),
    ),
    check_fn=_check_kanban_intake_available,
    emoji="📥",
)
