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
from hermes_disk_lifecycle import LifecycleContext, Surface, lifecycle_report, parse_mountinfo, resolver_contract, rollout_flags_from_env

_DISK_ENV_KEYS = {
    "HERMES_DISK_LIFECYCLE_MODE",
    "HERMES_DISK_HOST_MODE",
    "HERMES_DISK_BLOCK_NEW_ROOT_HEAVY",
    "HERMES_DISK_POST_RUN_ROOT_DELTA",
    "HERMES_DISK_ALLOW_ROOT_OVERRIDE",
    "HERMES_DISK_MOUNT_IDENTITY_REQUIRED",
}


def runtime_env_projection(settings: Mapping[str, Any]) -> dict[str, str]:
    """Render config-backed lifecycle settings as runtime env projection only."""
    return {
        "HERMES_DISK_LIFECYCLE_MODE": str(settings.get("mode") or "observe"),
        "HERMES_DISK_HOST_MODE": str(settings.get("host_mode") or "compatibility_host"),
        "HERMES_DISK_BLOCK_NEW_ROOT_HEAVY": "true" if bool(settings.get("block_new_root_heavy")) else "false",
        "HERMES_DISK_POST_RUN_ROOT_DELTA": str(settings.get("post_run_root_delta") or "observe"),
        "HERMES_DISK_ALLOW_ROOT_OVERRIDE": "true" if bool(settings.get("allow_root_override")) else "false",
        "HERMES_DISK_MOUNT_IDENTITY_REQUIRED": "true" if bool(settings.get("mount_identity_required")) else "false",
    }


def _bool_setting(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _settings_from_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict((config or {}).get("disk_lifecycle") or {})
    return {
        "mode": raw.get("mode") or "observe",
        "host_mode": raw.get("host_mode") or "compatibility_host",
        "block_new_root_heavy": _bool_setting(raw.get("block_new_root_heavy"), False),
        "post_run_root_delta": raw.get("post_run_root_delta") or "observe",
        "allow_root_override": _bool_setting(raw.get("allow_root_override"), False),
        "mount_identity_required": _bool_setting(raw.get("mount_identity_required"), False),
        "data_root": raw.get("data_root") or "/mnt/hermes-data",
        "extra_root": raw.get("extra_root") or "/mnt/hermes-extra",
        "sandbox_root": raw.get("sandbox_root") or "",
        "workspace_root": raw.get("workspace_root") or "",
        "cache_root": raw.get("cache_root") or "",
        "toolchain_root": raw.get("toolchain_root") or "",
    }


def _live_env_status(env: Mapping[str, str], projection: Mapping[str, str]) -> dict[str, Any]:
    return {
        "configured": dict(projection),
        "live": {key: env.get(key, "") for key in sorted(_DISK_ENV_KEYS)},
        "matches_projection": {key: env.get(key, "") == value for key, value in projection.items()},
        "missing_from_live": [key for key in projection if key not in env],
        "mismatched_live": [key for key, value in projection.items() if key in env and env.get(key, "") != value],
    }

def _normalize_root(value: str) -> str:
    return str(Path(value).expanduser().resolve(strict=False)) if value else ""


def _load_config_if_needed(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if config is not None:
        return config
    from hermes_cli.config import load_config_readonly

    return load_config_readonly()

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


def build_context(
    *,
    surface: Surface = Surface.SCANNER,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> LifecycleContext:
    env = env or os.environ
    loaded_config = _load_config_if_needed(config)
    settings = _settings_from_config(loaded_config)
    projection = runtime_env_projection(settings)
    hermes_home = str(get_hermes_home())
    base_context = LifecycleContext(
        active_profile=str(env.get("HERMES_PROFILE") or "default"),
        hermes_home=hermes_home,
        cwd=os.getcwd(),
        data_root=_normalize_root(str(settings["data_root"])),
        extra_root=_normalize_root(str(settings["extra_root"])),
        sandbox_root=_normalize_root(str(settings.get("sandbox_root") or "")),
        workspace_root=_normalize_root(str(settings.get("workspace_root") or "")),
        cache_root=_normalize_root(str(settings.get("cache_root") or "")),
        toolchain_root=_normalize_root(str(settings.get("toolchain_root") or "")),
        state_db_path=str(Path(hermes_home) / "state.db"),
        logs_path=str(Path(hermes_home) / "logs"),
        cron_output_path=str(Path(hermes_home) / "cron" / "output"),
        surface=surface,
        rollout=rollout_flags_from_env(projection),
        mount_table=_read_mount_table(),
        root_used_percent=_root_used_percent("/"),
    )
    contract = resolver_contract(base_context)
    return LifecycleContext(
        active_profile=base_context.active_profile,
        hermes_home=base_context.hermes_home,
        cwd=base_context.cwd,
        data_root=base_context.data_root,
        extra_root=base_context.extra_root,
        sandbox_root=contract["sandbox_root"],
        workspace_root=contract["extra_workspace_root"],
        cache_root=contract["extra_cache_root"],
        toolchain_root=contract["extra_toolchain_root"],
        state_db_path=contract["state_db_path"],
        logs_path=contract["logs_path"],
        cron_output_path=contract["cron_output_path"],
        surface=surface,
        rollout=base_context.rollout,
        mount_table=base_context.mount_table,
        root_used_percent=base_context.root_used_percent,
    )


def _candidate_summary(report: Mapping[str, Any]) -> dict[str, list[str]]:
    root_reclaiming: list[str] = []
    data_disk_cache: list[str] = []
    for item in report.get("paths", []):
        classification = item.get("classification") or {}
        path = str(classification.get("path") or "")
        if not path:
            continue
        mount_role = classification.get("mount_role")
        path_class = classification.get("path_class")
        truth_surface = classification.get("truth_surface")
        if mount_role == "root" and path_class in {"unknown_root_mass", "heavy_workbench", "rebuildable_cache", "temporary"}:
            root_reclaiming.append(path)
        elif mount_role in {"data", "extra"} and truth_surface == "rebuildable":
            data_disk_cache.append(path)
    return {"root_reclaiming_candidates": root_reclaiming, "data_disk_cache_candidates": data_disk_cache}


def dry_run_report(
    paths: Sequence[str] | None = None,
    manifests: Sequence[Mapping[str, Any]] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    env = env or os.environ
    loaded_config = _load_config_if_needed(config)
    settings = _settings_from_config(loaded_config)
    context = build_context(env=env, config=loaded_config)
    default_paths = [context.hermes_home, context.logs_path, context.cron_output_path, context.sandbox_root, context.cwd]
    selected_paths = list(paths) if paths is not None else [p for p in default_paths if p]
    report = lifecycle_report(context=context, paths=selected_paths, manifests=manifests or [])
    projection = runtime_env_projection(settings)
    report["runtime_env_projection"] = projection
    report["live_env_status"] = _live_env_status(env, projection)
    report["resolver_contract"] = resolver_contract(context)
    report["candidate_summary"] = _candidate_summary(report)
    report["utilization"] = {"root_used_percent": context.root_used_percent}
    report["mount_identity"] = {
        "data_root": context.data_root,
        "extra_root": context.extra_root,
        "mount_table_present": bool(context.mount_table),
    }
    return report


def dry_run_report_json(paths: Sequence[str] | None = None, manifests: Sequence[Mapping[str, Any]] | None = None, *, env: Mapping[str, str] | None = None, config: Mapping[str, Any] | None = None) -> str:
    return json.dumps(dry_run_report(paths, manifests, env=env, config=config), indent=2, sort_keys=True)
