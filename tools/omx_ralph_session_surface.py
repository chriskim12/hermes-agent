"""Validated persistent `$ralph` session surfaces for Hermes→OMX handoff.

Hermes non-TTY CLI commands such as ``omx ralph "task"`` are deliberately not
persistent Ralph handoffs.  Persistent Ralph must be injected as the in-session
``$ralph`` workflow keyword into a real OMX/Codex leader surface (PTY process or
an existing tmux leader pane).  This module keeps the command/message contract
small, testable, and reusable by gateway/operator code without pretending that a
command-shape check is runtime progress.
"""

from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


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


def build_ralph_in_session_message(task: str) -> str:
    """Build the message sent *inside* the OMX/Codex leader session."""

    text = " ".join(str(task or "").split())
    if not text:
        raise ValueError("missing_ralph_task")
    return f"$ralph {shlex.quote(text)}"


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
            "Persistent Ralph requires in-session `$ralph` inside an OMX/Codex "
            "PTY/tmux leader surface; non-TTY CLI `omx ralph` is not valid."
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
    """Launch an OMX leader under PTY and inject `$ralph` into that session.

    The caller still owns runtime verification (poll/log inspection and cleanup).
    This function only creates the valid surface and performs the in-session
    injection, returning correlation metadata for work-state tracking.
    """

    if process_registry is None:
        from tools.process_registry import process_registry as process_registry  # type: ignore[no-redef]

    repo = str(Path(repo_path).expanduser().resolve())
    worktree = str(Path(worktree_path or repo).expanduser().resolve())
    command = build_omx_leader_command()
    session = process_registry.spawn_local(
        command,
        cwd=worktree,
        session_key=session_key,
        use_pty=True,
    )
    message = build_ralph_in_session_message(task)
    result = process_registry.submit_stdin(session.id, message)
    if result.get("status") != "ok":
        raise RuntimeError(f"ralph_session_injection_failed:{result.get('error') or result.get('reason') or 'unknown'}")

    return RalphSessionSurface(
        executor_session_id=session.id,
        tmux_session=None,
        repo_path=repo,
        worktree_path=worktree,
        command=command,
        injected_message=message,
    )
