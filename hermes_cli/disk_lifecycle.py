"""Dry-run reporting adapters for Hermes disk lifecycle policy.

The functions here inspect local filesystem facts but never migrate, delete, or
create lifecycle roots. They are safe for tests, CLI diagnostics, cron checks,
and closeout evidence generation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_constants import get_hermes_home
from hermes_disk_lifecycle import LifecycleContext, Surface, lifecycle_report, parse_mountinfo, rollout_flags_from_env


def _read_mount_table() -> tuple[Mapping[str, Any], ...]:
    try:
        return parse_mountinfo(Path("/proc/self/mountinfo").read_text(encoding="utf-8"))
    except OSError:
        return ()


def _root_used_percent(path: str = "/") -> float | None:
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    total = stats.f_blocks * stats.f_frsize
    free = stats.f_bavail * stats.f_frsize
    if total <= 0:
        return None
    return round(((total - free) / total) * 100.0, 2)


def build_context(*, surface: Surface = Surface.SCANNER, env: Mapping[str, str] | None = None) -> LifecycleContext:
    env = env or os.environ
    hermes_home = str(get_hermes_home())
    return LifecycleContext(
        active_profile=str(env.get("HERMES_PROFILE") or "default"),
        hermes_home=hermes_home,
        cwd=os.getcwd(),
        data_root=str(env.get("HERMES_DISK_DATA_ROOT") or "/mnt/hermes-data"),
        extra_root=str(env.get("HERMES_DISK_EXTRA_ROOT") or "/mnt/hermes-extra"),
        sandbox_root=str(env.get("TERMINAL_SANDBOX_DIR") or Path(hermes_home) / "sandboxes"),
        workspace_root=str(env.get("HERMES_WORKSPACE_ROOT") or env.get("GJC_WORKSPACE_ROOT") or ""),
        cache_root=str(env.get("HERMES_CACHE_ROOT") or ""),
        toolchain_root=str(env.get("HERMES_TOOLCHAIN_ROOT") or ""),
        state_db_path=str(Path(hermes_home) / "state.db"),
        logs_path=str(Path(hermes_home) / "logs"),
        cron_output_path=str(Path(hermes_home) / "cron" / "output"),
        surface=surface,
        rollout=rollout_flags_from_env(env),
        mount_table=_read_mount_table(),
        root_used_percent=_root_used_percent("/"),
    )


def dry_run_report(paths: Sequence[str] | None = None, manifests: Sequence[Mapping[str, Any]] | None = None, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    context = build_context(env=env)
    default_paths = [context.hermes_home, context.logs_path, context.cron_output_path, context.sandbox_root, context.cwd]
    selected_paths = list(paths) if paths is not None else [p for p in default_paths if p]
    report = lifecycle_report(context=context, paths=selected_paths, manifests=manifests or [])
    report["utilization"] = {"root_used_percent": context.root_used_percent}
    report["mount_identity"] = {
        "data_root": context.data_root,
        "extra_root": context.extra_root,
        "mount_table_present": bool(context.mount_table),
    }
    return report


def dry_run_report_json(paths: Sequence[str] | None = None, manifests: Sequence[Mapping[str, Any]] | None = None, *, env: Mapping[str, str] | None = None) -> str:
    return json.dumps(dry_run_report(paths, manifests, env=env), indent=2, sort_keys=True)
