"""Helpers for safely resolving the current working directory.

Some agent entry points can run after their original process cwd has been
removed (for example, after a task worktree is cleaned up).  Plain
``os.getcwd()`` raises ``FileNotFoundError`` in that state, so startup and
checkpoint code must use this helper instead of eager cwd reads.
"""

from __future__ import annotations

import os


def safe_process_cwd(*fallbacks: str | None) -> str:
    """Return process cwd, or the first existing fallback if cwd was deleted."""
    try:
        return os.getcwd()
    except FileNotFoundError:
        for fallback in fallbacks:
            if fallback and os.path.isdir(os.path.expanduser(fallback)):
                return os.path.expanduser(fallback)
        home = os.getenv("HOME")
        if home and os.path.isdir(os.path.expanduser(home)):
            return os.path.expanduser(home)
        return "/tmp"
