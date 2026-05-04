"""Validated persistent `$ralph` session surfaces for Hermes→OMX handoff.

Hermes non-TTY CLI commands such as ``omx ralph "task"`` are deliberately not
persistent Ralph handoffs, and on this host plain PTY ``omx ralph <task>`` is
also not enough evidence to treat the lane as safe or progressing.  Persistent
Ralph must run on a real OMX/Codex PTY or tmux surface, but Hermes starts the
trusted operator path as an interactive ``omx --madmax --high`` leader and then
submits prompt-side ``$ralph <task>`` into that surface.  This module keeps the
surface contract small, testable, and reusable by gateway/operator code without
pretending that surface materialization is runtime progress.
"""

from __future__ import annotations

import json
import os
import shlex
from datetime import datetime, timedelta
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from tools.registry import registry


DEFAULT_FIRST_PROGRESS_WATCHDOG_SECONDS = 180


@dataclass(frozen=True)
class RalphSessionSurface:
    """Inspectable metadata for a real persistent Ralph handoff surface."""

    executor_session_id: Optional[str]
    tmux_session: Optional[str]
    repo_path: str
    worktree_path: str
    command: str
    injected_message: str
    current_lane: str = "ralph"
    planning_gate: str = "closed"
    next_execution_branch: str = "ralph"
    close_authority: str = "hermes"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_omx_leader_command() -> str:
    """Return the upstream-aligned interactive OMX leader command."""

    return "omx --madmax --high"


def _normalize_task(task: str) -> str:
    text = " ".join(str(task or "").split())
    if not text:
        raise ValueError("missing_ralph_task")
    return text


def build_omx_ralph_command(task: str) -> str:
    """Return the legacy/plain Ralph launcher shape for detection only.

    Hermes must not use this as the trusted persistent PTY launcher until a
    host-specific proof shows it reaches useful first progress without approval
    or sandbox loops. Use ``build_omx_leader_command`` plus
    ``build_ralph_in_session_message`` for the operator path.
    """

    return f"omx ralph {shlex.quote(_normalize_task(task))}"


def build_ralph_in_session_message(task: str) -> str:
    """Build the prompt-side message sent *inside* an already-ready OMX/Codex leader."""

    return f"$ralph {shlex.quote(_normalize_task(task))}"


def validate_ralph_session_surface(
    *,
    executor_session_id: Optional[str] = None,
    tmux_session: Optional[str] = None,
    pty: bool = False,
) -> Dict[str, str]:
    """Fail closed unless the handoff targets a real interactive surface."""

    if executor_session_id and pty:
        return {"status": "ok", "surface": "pty_process"}
    if tmux_session:
        return {"status": "ok", "surface": "tmux_session"}
    return {
        "status": "error",
        "reason": "missing_real_ralph_session_surface",
        "message": (
            "Persistent Ralph requires official `omx ralph <task>` or prompt-side "
            "`$ralph` inside an OMX/Codex PTY/tmux leader surface; non-TTY CLI "
            "`omx ralph` is not valid."
        ),
    }


def materialize_ralph_session_surface(
    *,
    task: str,
    repo_path: str,
    worktree_path: Optional[str] = None,
    executor_session_id: Optional[str] = None,
    tmux_session: Optional[str] = None,
    pty: bool = False,
) -> RalphSessionSurface:
    """Create normalized metadata for an approved persistent Ralph handoff."""

    validation = validate_ralph_session_surface(
        executor_session_id=executor_session_id,
        tmux_session=tmux_session,
        pty=pty,
    )
    if validation["status"] != "ok":
        raise ValueError(validation["reason"])

    repo = str(Path(repo_path).expanduser().resolve()) if repo_path else ""
    if not repo:
        raise ValueError("missing_repo_path")
    worktree = str(Path(worktree_path or repo).expanduser().resolve())

    return RalphSessionSurface(
        executor_session_id=executor_session_id,
        tmux_session=tmux_session,
        repo_path=repo,
        worktree_path=worktree,
        command=build_omx_leader_command(),
        injected_message=build_ralph_in_session_message(task),
    )


def launch_pty_ralph_session(
    *,
    task: str,
    repo_path: str,
    worktree_path: Optional[str] = None,
    session_key: str = "",
    process_registry: Any = None,
) -> RalphSessionSurface:
    """Launch an OMX leader under PTY and submit ``$ralph`` inside it.

    The caller still owns runtime verification (poll/log inspection and cleanup).
    This function only creates the valid interactive surface and submits the
    Ralph request, returning correlation metadata for work-state tracking. It
    intentionally avoids both the Hermes non-PTY `omx ralph` path guarded by
    terminal_tool and the plain PTY `omx ralph <task>` path that previously
    looked alive while looping on approval/sandbox behavior. Since Hermes is
    already providing the real PTY, force OMX's leader launch policy to
    ``direct`` to avoid nested detached-tmux/HUD attach prompts being mistaken
    for Ralph progress.
    """

    if process_registry is None:
        from tools.process_registry import process_registry as process_registry  # type: ignore[no-redef]

    repo = str(Path(repo_path).expanduser().resolve())
    worktree = str(Path(worktree_path or repo).expanduser().resolve())
    command = build_omx_leader_command()
    injected_message = build_ralph_in_session_message(task)
    env_vars = {
        "TERM": os.environ.get("TERM") or "xterm-256color",
        "OMX_LAUNCH_POLICY": "direct",
    }
    if env_vars["TERM"] in {"", "dumb", "unknown"}:
        env_vars["TERM"] = "xterm-256color"
    session = process_registry.spawn_local(
        command,
        cwd=worktree,
        session_key=session_key,
        env_vars=env_vars,
        use_pty=True,
    )
    submit_result = process_registry.submit_stdin(session.id, injected_message)
    if str(submit_result.get("status") or "").lower() != "ok":
        raise RuntimeError(
            "ralph_prompt_submit_failed:"
            f"{submit_result.get('error') or submit_result.get('status') or 'unknown'}"
        )
    surface = materialize_ralph_session_surface(
        task=task,
        repo_path=repo,
        worktree_path=worktree,
        executor_session_id=session.id,
        pty=True,
    )
    return RalphSessionSurface(
        executor_session_id=surface.executor_session_id,
        tmux_session=surface.tmux_session,
        repo_path=surface.repo_path,
        worktree_path=surface.worktree_path,
        command=command,
        injected_message=surface.injected_message,
    )


def start_omx_ralph_lane(
    *,
    task: str,
    repo_path: str,
    worktree_path: Optional[str] = None,
    session_key: str = "",
    work_id: Optional[str] = None,
    owner_session_id: Optional[str] = None,
    process_registry: Any = None,
    work_state_store: Any = None,
) -> Dict[str, Any]:
    """Start a real upstream-aligned `$ralph` lane and record lane truth.

    This is the Hermes operator path for CH-232/CH-229: launch the official
    upstream OMX leader inside a real PTY, submit `$ralph <task>`, and optionally
    mark the matching Hermes work record as delegated to the Ralph lane. It
    deliberately does not inspect or mutate ``.omx/state`` as completion
    evidence; runtime closeout and first-progress verification remain separate
    steps.
    """

    surface = launch_pty_ralph_session(
        task=task,
        repo_path=repo_path,
        worktree_path=worktree_path,
        session_key=session_key,
        process_registry=process_registry,
    )
    record_updated = None
    if work_id and owner_session_id:
        if work_state_store is None:
            from gateway.work_state import WorkStateStore

            work_state_store = WorkStateStore()
        now = datetime.now().astimezone()
        watchdog_seconds = DEFAULT_FIRST_PROGRESS_WATCHDOG_SECONDS
        record_updated = work_state_store.update_record(
            work_id,
            owner_session_id,
            executor="omx",
            mode="delegated",
            state="running",
            last_progress_at=now,
            executor_session_id=surface.executor_session_id,
            tmux_session=surface.tmux_session,
            repo_path=surface.repo_path,
            worktree_path=surface.worktree_path,
            next_action="Verify first useful Ralph progress before closeout",
            proof=(
                "ralph_session_surface:tmux_leader"
                if surface.tmux_session
                else "ralph_session_surface:pty_leader_injected"
            ),
            usable_outcome=None,
            close_disposition=None,
            current_lane=surface.current_lane,
            planning_gate=surface.planning_gate,
            next_execution_branch=surface.next_execution_branch,
            close_authority=surface.close_authority,
            surface_started_at=now,
            first_progress_at=None,
            first_progress_proof=None,
            no_progress_watchdog_seconds=watchdog_seconds,
            no_progress_deadline_at=now + timedelta(seconds=watchdog_seconds),
            blocked_reason=None,
            cleanup_required=False,
            cleanup_proof=None,
            reroute_recommendation="bounded omx --madmax --high exec if no first progress is observed",
        )

    return {
        "status": "accepted",
        "surface": surface.to_dict(),
        "work_state_updated": record_updated,
        "verification_required": (
            "Surface materialization is not progress. Poll/log the returned "
            "executor_session_id and verify diff/tool/progress evidence before closeout."
        ),
    }


OMX_RALPH_SCHEMA = {
    "name": "omx_ralph",
    "description": (
        "Start an upstream-aligned persistent Ralph lane by launching an interactive "
        "`omx --madmax --high` leader in a real PTY, then submitting `$ralph <task>`. "
        "Use this instead of noninteractive or plain PTY `omx ralph ...`."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Specific task text to pass to `$ralph` inside the OMX session.",
            },
            "repo_path": {
                "type": "string",
                "description": "Repository root for the OMX leader session.",
            },
            "worktree_path": {
                "type": "string",
                "description": "Optional worktree/current working directory for the session; defaults to repo_path.",
            },
            "session_key": {
                "type": "string",
                "description": "Optional Hermes owner session key for process correlation.",
            },
            "work_id": {
                "type": "string",
                "description": "Optional Hermes work-state id to mark as delegated to Ralph.",
            },
            "owner_session_id": {
                "type": "string",
                "description": "Owner session id required when work_id is provided.",
            },
        },
        "required": ["task", "repo_path"],
    },
}


def omx_ralph_tool(
    *,
    task: str,
    repo_path: str,
    worktree_path: Optional[str] = None,
    session_key: str = "",
    work_id: Optional[str] = None,
    owner_session_id: Optional[str] = None,
) -> str:
    try:
        result = start_omx_ralph_lane(
            task=task,
            repo_path=repo_path,
            worktree_path=worktree_path,
            session_key=session_key,
            work_id=work_id,
            owner_session_id=owner_session_id,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "error": str(exc),
                "blocked_reason": "omx_ralph_lane_start_failed",
            },
            ensure_ascii=False,
        )


registry.register(
    name="omx_ralph",
    toolset="terminal",
    schema=OMX_RALPH_SCHEMA,
    handler=lambda args, **kw: omx_ralph_tool(
        task=args.get("task", ""),
        repo_path=args.get("repo_path", ""),
        worktree_path=args.get("worktree_path"),
        session_key=args.get("session_key", ""),
        work_id=args.get("work_id"),
        owner_session_id=args.get("owner_session_id"),
    ),
    emoji="🧠",
)
