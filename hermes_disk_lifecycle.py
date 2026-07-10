"""Dependency-neutral disk lifecycle policy for Hermes runtime paths.

This module deliberately contains no tool, CLI, scheduler, Docker, database, or
filesystem-write side effects. Callers pass host/profile/env/path facts in and
receive policy decisions, blockers, and manifest validation results back.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


class _StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class HostMode(_StrEnum):
    REQUIRED_HERMES_HOST = "required_hermes_host"
    COMPATIBILITY_HOST = "compatibility_host"
    TEST_DEV_HOST = "test_dev_host"


class Surface(_StrEnum):
    CLI = "cli"
    TERMINAL = "terminal"
    BACKGROUND = "background"
    CRON = "cron"
    KANBAN_READY = "kanban_ready"
    KANBAN_REVIEW_READY = "kanban_review_ready"
    KANBAN_DONE = "kanban_done"
    GJC = "gjc"
    DOCKER = "docker"
    SANDBOX = "sandbox"
    SCANNER = "scanner"


class WorkKind(_StrEnum):
    CONTROL = "control"
    DURABLE_TRUTH = "durable_truth"
    DURABLE_EVIDENCE = "durable_evidence"
    HEAVY_WORK = "heavy_work"
    CACHE = "cache"
    TOOLCHAIN = "toolchain"
    DOCKER_PROOF = "docker_proof"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class MountRole(_StrEnum):
    ROOT = "root"
    DATA = "data"
    EXTRA = "extra"
    TEST = "test"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class TruthSurface(_StrEnum):
    RUNTIME_STATE = "runtime_state"
    AUDIT = "audit"
    LOGS = "logs"
    CRON_OUTPUT = "cron_output"
    FINAL_EVIDENCE = "final_evidence"
    REBUILDABLE = "rebuildable"
    TEMPORARY = "temporary"
    CONTROL_METADATA = "control_metadata"
    NONE = "none"


class CleanupPolicy(_StrEnum):
    PERMANENT = "permanent"
    RETAIN_UNTIL = "retain_until"
    REMOVE_BY = "remove_by"
    EPHEMERAL = "ephemeral"
    APPROVAL_REQUIRED = "approval_required"


class EnforcementMode(_StrEnum):
    OFF = "off"
    OBSERVE = "observe"
    WARN = "warn"
    BLOCK = "block"


class Decision(_StrEnum):
    ALLOW = "allow"
    OBSERVE = "observe"
    WARN = "warn"
    BLOCK = "block"
    REROUTE = "reroute"
    REQUIRES_APPROVAL = "requires_approval"


class PathClass(_StrEnum):
    CONTROL_PLANE = "control_plane"
    DURABLE_TRUTH = "durable_truth"
    DURABLE_EVIDENCE = "durable_evidence"
    HEAVY_WORKBENCH = "heavy_workbench"
    REBUILDABLE_CACHE = "rebuildable_cache"
    TEMPORARY = "temporary"
    UNKNOWN_ROOT_MASS = "unknown_root_mass"
    INVALID_MOUNT = "invalid_mount"


class BlockerCode(_StrEnum):
    DATA_MOUNT_MISSING = "data_mount_missing"
    EXTRA_MOUNT_MISSING = "extra_mount_missing"
    MOUNT_IDENTITY_INVALID = "mount_identity_invalid"
    ROOT_HEAVY_WORK = "root_heavy_work"
    ROOT_ABOVE_WARN = "root_above_warn"
    ROOT_ABOVE_BLOCK = "root_above_block"
    ROOT_ABOVE_EMERGENCY = "root_above_emergency"
    MANIFEST_MISSING_FIELD = "manifest_missing_field"
    MANIFEST_INVALID_FIELD = "manifest_invalid_field"
    MANIFEST_INVALID_TIMESTAMP = "manifest_invalid_timestamp"
    MANIFEST_PATH_MOUNT_CONFLICT = "manifest_path_mount_conflict"
    MANIFEST_TRUTH_SURFACE_CONFLICT = "manifest_truth_surface_conflict"
    MANIFEST_OWNERLESS_RESIDUE = "manifest_ownerless_residue"
    MANIFEST_EXPIRED = "manifest_expired"
    UNAPPROVED_ROOT_OVERRIDE = "unapproved_root_override"
    UNACCOUNTED_ROOT_DELTA = "unaccounted_root_delta"


@dataclass(frozen=True)
class Thresholds:
    root_target_percent: float = 60.0
    root_warn_percent: float = 70.0
    root_block_percent: float = 80.0
    root_emergency_percent: float = 90.0
    data_warn_percent: float = 80.0
    data_block_percent: float = 90.0
    extra_warn_percent: float = 85.0
    extra_block_percent: float = 95.0


@dataclass(frozen=True)
class MountIdentity:
    role: MountRole
    path: str
    present: bool
    real_mount: bool
    device_id: str = ""
    fs_type: str = ""
    source: str = ""
    root_device_id: str = ""
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class RolloutFlags:
    mode: EnforcementMode = EnforcementMode.OBSERVE
    host_mode: HostMode = HostMode.COMPATIBILITY_HOST
    block_new_root_heavy: bool = False
    require_manifest_on_closeout: bool = False
    post_run_root_delta: EnforcementMode = EnforcementMode.OBSERVE
    allow_root_override: bool = False
    mount_identity_required: bool = False
    approval_id: str = ""
    owner: str = ""
    reason: str = ""
    remove_by: str = ""


@dataclass(frozen=True)
class LifecycleContext:
    active_profile: str = "default"
    hermes_home: str = ""
    cwd: str = ""
    data_root: str = "/mnt/hermes-data"
    extra_root: str = "/mnt/hermes-extra"
    sandbox_root: str = ""
    evidence_root: str = ""
    workspace_root: str = ""
    cache_root: str = ""
    toolchain_root: str = ""
    state_db_path: str = ""
    logs_path: str = ""
    cron_output_path: str = ""
    run_id: str = ""
    card_id: str = ""
    surface: Surface = Surface.CLI
    work_kind: WorkKind = WorkKind.UNKNOWN
    expected_bytes: int = 0
    thresholds: Thresholds = field(default_factory=Thresholds)
    rollout: RolloutFlags = field(default_factory=RolloutFlags)
    mount_table: tuple[Mapping[str, Any], ...] = ()
    root_used_percent: float | None = None


@dataclass(frozen=True)
class PathClassification:
    path: str
    path_class: PathClass
    mount_role: MountRole
    truth_surface: TruthSurface
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifecycleDecision:
    decision: Decision
    enforcement_mode: EnforcementMode
    host_mode: HostMode
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    classification: PathClassification | None = None
    mount_identities: tuple[MountIdentity, ...] = ()
    message: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision not in {Decision.BLOCK, Decision.REROUTE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "allowed": self.allowed,
            "enforcement_mode": self.enforcement_mode.value,
            "host_mode": self.host_mode.value,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "classification": _classification_to_dict(self.classification),
            "mount_identities": [_mount_identity_to_dict(item) for item in self.mount_identities],
            "message": self.message,
        }


@dataclass(frozen=True)
class ManifestValidation:
    valid: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    normalized: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "normalized": dict(self.normalized),
        }


_REQUIRED_MANIFEST_FIELDS = (
    "schema", "owner", "run_id", "card_id", "created_at", "purpose",
    "truth_surface", "cleanup_policy", "remove_by", "path", "mount_role",
    "adapter", "disposition", "source_surface", "profile", "hermes_home",
    "mount_device_id",
)

_SCHEMA = "hermes_artifact_manifest.v1"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_HEAVY_NAMES = {
    "workspaces",
    "workspace",
    "build",
    "dist",
    "node_modules",
    ".cache",
    ".next",
    ".pytest_cache",
    ".turbo",
    "cache",
    "target",
    "toolchains",
    "docker",
    "sandboxes",
    "venv",
    ".venv",
}
_DATA_NAMES = {"state.db", "sessions", "logs", "cron", "audit", "evidence", "final-evidence", "final_evidence"}
_BUILD_OUTPUT_NAMES = ("target", "node_modules", ".next", ".turbo", "dist", "build", ".pytest_cache")
_INSTALL_OUTPUT_NAMES = ("node_modules", ".venv", "venv")
_CARGO_COMMANDS = {"cargo"}
_JS_PACKAGE_MANAGERS = {"bun", "npm", "pnpm", "yarn"}
_JS_BUILD_COMMANDS = {"build", "dev", "start", "preview", "tauri", "test:e2e", "test:red-team"}
_NATIVE_BUILD_COMMANDS = {"make", "cmake", "ninja", "meson", "pip", "python", "python3"}


def _clean_path(value: str | os.PathLike[str] | None) -> str:
    text = os.fspath(value) if value is not None else ""
    text = text.strip()
    if not text:
        return ""
    return os.path.normpath(os.path.expanduser(text))


def _under(path: str, root: str) -> bool:
    path = _clean_path(path)
    root = _clean_path(root)
    if not path or not root:
        return False
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in PurePosixPath(path).parts if part not in {"/", ""})


def _is_root_heavy_path(path: str) -> bool:
    """Return whether ``path`` names rebuildable mass on the root disk.

    Most heavy outputs have an unambiguous component name. Containerd is
    location-sensitive because ``/etc/containerd`` is configuration while
    ``/var/lib/containerd`` is the large runtime data root. Git's
    ``.git/worktrees`` directory is metadata, unlike user-facing ``worktrees``
    and ``.worktrees`` directories that contain complete checkouts.
    """
    parts = _parts(path)
    if _HEAVY_NAMES & set(parts):
        return True
    if _under(path, "/var/lib/containerd"):
        return True
    return any(
        part in {"worktrees", ".worktrees"}
        and not (part == "worktrees" and index > 0 and parts[index - 1] == ".git")
        for index, part in enumerate(parts)
    )


def _enum_value(enum: type[_StrEnum], value: Any, default: _StrEnum) -> _StrEnum:
    text = str(value or "").strip().lower().replace("-", "_")
    for item in enum:
        if item.value == text or item.name.lower() == text:
            return item
    return default


def _is_valid_enum_value(enum: type[_StrEnum], value: Any) -> bool:
    text = str(value or "").strip().lower().replace("-", "_")
    return any(item.value == text or item.name.lower() == text for item in enum)


def _bool_env(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default


def rollout_flags_from_env(env: Mapping[str, str] | None = None) -> RolloutFlags:
    """Resolve rollout flags from an env mapping without mutating process state."""
    env = env or os.environ
    mode = _enum_value(EnforcementMode, env.get("HERMES_DISK_LIFECYCLE_MODE"), EnforcementMode.OBSERVE)
    host_mode = _enum_value(HostMode, env.get("HERMES_DISK_HOST_MODE"), HostMode.COMPATIBILITY_HOST)
    post_delta = _enum_value(EnforcementMode, env.get("HERMES_DISK_POST_RUN_ROOT_DELTA"), EnforcementMode.OBSERVE)
    return RolloutFlags(
        mode=mode,  # type: ignore[arg-type]
        host_mode=host_mode,  # type: ignore[arg-type]
        block_new_root_heavy=_bool_env(env.get("HERMES_DISK_BLOCK_NEW_ROOT_HEAVY"), mode == EnforcementMode.BLOCK),
        require_manifest_on_closeout=_bool_env(env.get("HERMES_DISK_REQUIRE_MANIFEST_ON_CLOSEOUT"), False),
        post_run_root_delta=post_delta,  # type: ignore[arg-type]
        allow_root_override=_bool_env(env.get("HERMES_DISK_ALLOW_ROOT_OVERRIDE"), False),
        mount_identity_required=_bool_env(env.get("HERMES_DISK_MOUNT_IDENTITY_REQUIRED"), host_mode == HostMode.REQUIRED_HERMES_HOST),
        approval_id=str(env.get("HERMES_DISK_ROOT_OVERRIDE_APPROVAL_ID") or ""),
        owner=str(env.get("HERMES_DISK_ROOT_OVERRIDE_OWNER") or ""),
        reason=str(env.get("HERMES_DISK_ROOT_OVERRIDE_REASON") or ""),
        remove_by=str(env.get("HERMES_DISK_ROOT_OVERRIDE_REMOVE_BY") or ""),
    )


def parse_mountinfo(text: str) -> tuple[Mapping[str, Any], ...]:
    """Parse Linux /proc/self/mountinfo text into neutral mount records."""
    records: list[Mapping[str, Any]] = []
    for line in (text or "").splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        left_parts = left.split()
        right_parts = right.split()
        if len(left_parts) < 5 or len(right_parts) < 3:
            continue
        records.append({
            "mount_point": left_parts[4].replace("\040", " "),
            "device_id": left_parts[2],
            "fs_type": right_parts[0],
            "source": right_parts[1],
        })
    return tuple(records)


def _find_mount(path: str, mount_table: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    clean = _clean_path(path) or "/"
    best: Mapping[str, Any] | None = None
    best_len = -1
    for item in mount_table:
        mp = _clean_path(str(item.get("mount_point") or item.get("path") or ""))
        if mp and _under(clean, mp) and len(mp) > best_len:
            best = item
            best_len = len(mp)
    return best


def mount_identity_for(path: str, role: MountRole, mount_table: Sequence[Mapping[str, Any]]) -> MountIdentity:
    clean = _clean_path(path)
    root = _find_mount("/", mount_table)
    exact = None
    for item in mount_table:
        mp = _clean_path(str(item.get("mount_point") or item.get("path") or ""))
        if mp == clean:
            exact = item
            break
    root_device = str((root or {}).get("device_id") or "")
    device = str((exact or {}).get("device_id") or "")
    blockers: list[str] = []
    present = exact is not None
    real_mount = bool(exact)
    if not present:
        blockers.append(BlockerCode.MOUNT_IDENTITY_INVALID.value)
    elif role in {MountRole.DATA, MountRole.EXTRA} and root_device and device == root_device:
        real_mount = False
        blockers.append(BlockerCode.MOUNT_IDENTITY_INVALID.value)
    return MountIdentity(role, clean, present, real_mount, device, str((exact or {}).get("fs_type") or ""), str((exact or {}).get("source") or ""), root_device, tuple(blockers))


def classify_path(path: str, context: LifecycleContext | None = None) -> PathClassification:
    """Classify a requested path using caller-provided roots and profile facts."""
    context = context or LifecycleContext()
    clean = _clean_path(path)
    blockers: list[str] = []
    if not clean:
        return PathClassification("", PathClass.INVALID_MOUNT, MountRole.INVALID, TruthSurface.NONE, (BlockerCode.MOUNT_IDENTITY_INVALID.value,))

    if context.data_root and _under(clean, context.data_root):
        parts = set(_parts(clean))
        truth = TruthSurface.FINAL_EVIDENCE if {"evidence", "final-evidence", "final_evidence"} & parts else TruthSurface.RUNTIME_STATE
        if "logs" in parts:
            truth = TruthSurface.LOGS
        if "cron" in parts:
            truth = TruthSurface.CRON_OUTPUT
        return PathClassification(clean, PathClass.DURABLE_EVIDENCE if truth == TruthSurface.FINAL_EVIDENCE else PathClass.DURABLE_TRUTH, MountRole.DATA, truth)

    if context.extra_root and _under(clean, context.extra_root):
        parts = set(_parts(clean))
        path_class = PathClass.REBUILDABLE_CACHE if {"cache", ".cache", "toolchains", "node_modules"} & parts else PathClass.HEAVY_WORKBENCH
        return PathClassification(clean, path_class, MountRole.EXTRA, TruthSurface.REBUILDABLE)

    if context.sandbox_root and _under(clean, context.sandbox_root):
        return PathClassification(clean, PathClass.HEAVY_WORKBENCH, MountRole.EXTRA if _under(clean, context.extra_root) else MountRole.ROOT, TruthSurface.REBUILDABLE)
    if context.workspace_root and _under(clean, context.workspace_root):
        return PathClassification(clean, PathClass.HEAVY_WORKBENCH, MountRole.EXTRA if _under(clean, context.extra_root) else MountRole.ROOT, TruthSurface.REBUILDABLE)
    if context.cache_root and _under(clean, context.cache_root):
        return PathClassification(clean, PathClass.REBUILDABLE_CACHE, MountRole.EXTRA if _under(clean, context.extra_root) else MountRole.ROOT, TruthSurface.REBUILDABLE)

    if context.hermes_home and _under(clean, context.hermes_home):
        parts = set(_parts(clean))
        if _DATA_NAMES & parts:
            return PathClassification(clean, PathClass.DURABLE_TRUTH, MountRole.ROOT, TruthSurface.RUNTIME_STATE)
        if _is_root_heavy_path(clean):
            return PathClassification(clean, PathClass.UNKNOWN_ROOT_MASS, MountRole.ROOT, TruthSurface.REBUILDABLE)
        return PathClassification(clean, PathClass.CONTROL_PLANE, MountRole.ROOT, TruthSurface.CONTROL_METADATA)

    if clean.startswith("/tmp") or clean.startswith("/var/tmp"):
        return PathClassification(clean, PathClass.TEMPORARY, MountRole.ROOT, TruthSurface.TEMPORARY)
    if _is_root_heavy_path(clean):
        blockers.append(BlockerCode.ROOT_HEAVY_WORK.value)
        return PathClassification(clean, PathClass.UNKNOWN_ROOT_MASS, MountRole.ROOT, TruthSurface.REBUILDABLE, tuple(blockers))
    if clean.startswith("/"):
        return PathClassification(clean, PathClass.CONTROL_PLANE, MountRole.ROOT, TruthSurface.CONTROL_METADATA)
    return PathClassification(clean, PathClass.INVALID_MOUNT, MountRole.INVALID, TruthSurface.NONE, (BlockerCode.MOUNT_IDENTITY_INVALID.value,))


def infer_command_output_paths(command: str, cwd: str) -> tuple[str, ...]:
    """Infer root-heavy outputs a command is likely to create before it runs."""
    clean_cwd = _clean_path(cwd) or os.getcwd()
    try:
        tokens = shlex.split(command or "", posix=True)
    except ValueError:
        tokens = (command or "").split()
    normalized = [token.strip() for token in tokens if token.strip()]
    if not normalized:
        return ()

    executable = PurePosixPath(normalized[0]).name
    lowered = [token.lower() for token in normalized]
    outputs: set[str] = set()

    if executable in _CARGO_COMMANDS and any(token in {"build", "test", "bench", "run", "check"} for token in lowered[1:]):
        outputs.add("target")

    if executable in _JS_PACKAGE_MANAGERS:
        if any(token in {"install", "add", "ci"} for token in lowered[1:]):
            outputs.update(_INSTALL_OUTPUT_NAMES)
        if "run" in lowered:
            run_index = lowered.index("run")
            script = lowered[run_index + 1] if run_index + 1 < len(lowered) else ""
            if script in _JS_BUILD_COMMANDS or any(name in script for name in ("build", "next", "tauri", "napi")):
                outputs.update(("node_modules", ".next", ".turbo", "dist", "build"))
        if any(token in {"build", "dev", "start", "preview", "test", "test:e2e"} for token in lowered[1:]):
            outputs.update(("node_modules", ".next", ".turbo", "dist", "build"))

    if executable == "next" and any(token in {"build", "dev", "start"} for token in lowered[1:]):
        outputs.update((".next", "node_modules"))

    if executable in _NATIVE_BUILD_COMMANDS:
        if executable in {"make", "cmake", "ninja", "meson"}:
            outputs.update(("build", "dist"))
        elif any(token in {"install", "wheel", "build"} for token in lowered[1:]):
            outputs.update((".venv", "venv", "build", "dist", ".pytest_cache"))

    ordered = _BUILD_OUTPUT_NAMES + tuple(name for name in _INSTALL_OUTPUT_NAMES if name not in _BUILD_OUTPUT_NAMES)
    return tuple(os.path.join(clean_cwd, name) for name in ordered if name in outputs)


def _decision_rank(decision: Decision) -> int:
    return {
        Decision.ALLOW: 0,
        Decision.OBSERVE: 1,
        Decision.WARN: 2,
        Decision.REQUIRES_APPROVAL: 3,
        Decision.REROUTE: 4,
        Decision.BLOCK: 5,
    }[decision]


def evaluate_command_request(command: str, cwd: str, context: LifecycleContext | None = None) -> LifecycleDecision:
    """Evaluate a command plus cwd by preflighting its likely generated outputs."""
    context = context or LifecycleContext(cwd=cwd)
    inferred_paths = infer_command_output_paths(command, cwd)
    if not inferred_paths:
        return evaluate_path_request(cwd, context)

    decisions = [evaluate_path_request(path, context) for path in inferred_paths]
    worst = max(decisions, key=lambda item: _decision_rank(item.decision))
    blockers = tuple(dict.fromkeys(blocker for item in decisions for blocker in item.blockers))
    warnings = tuple(dict.fromkeys(warning for item in decisions for warning in item.warnings))
    message = (
        "disk lifecycle command decision: "
        f"{worst.decision.value}; inferred outputs="
        + ",".join(inferred_paths)
    )
    return LifecycleDecision(
        worst.decision,
        worst.enforcement_mode,
        worst.host_mode,
        blockers,
        warnings,
        worst.classification,
        worst.mount_identities,
        message,
    )

def _escalate(base: Decision, mode: EnforcementMode) -> Decision:
    if mode == EnforcementMode.OFF:
        return Decision.ALLOW
    if mode == EnforcementMode.OBSERVE and base in {Decision.WARN, Decision.BLOCK, Decision.REROUTE, Decision.REQUIRES_APPROVAL}:
        return Decision.OBSERVE
    if mode == EnforcementMode.WARN and base in {Decision.BLOCK, Decision.REROUTE}:
        return Decision.WARN
    return base


def _has_complete_root_override(rollout: RolloutFlags) -> bool:
    return all(str(value or "").strip() for value in (rollout.approval_id, rollout.owner, rollout.reason, rollout.remove_by))


def evaluate_path_request(path: str, context: LifecycleContext | None = None) -> LifecycleDecision:
    """Evaluate one requested path against host mode, rollout, mounts, and root pressure."""
    context = context or LifecycleContext()
    rollout = context.rollout
    classification = classify_path(path, context)
    blockers = list(classification.blockers)
    warnings: list[str] = []
    mount_ids: list[MountIdentity] = []

    if context.mount_table:
        data_id = mount_identity_for(context.data_root, MountRole.DATA, context.mount_table)
        extra_id = mount_identity_for(context.extra_root, MountRole.EXTRA, context.mount_table)
        mount_ids.extend([data_id, extra_id])
        if rollout.mount_identity_required or rollout.host_mode == HostMode.REQUIRED_HERMES_HOST:
            if not data_id.real_mount:
                blockers.append(BlockerCode.DATA_MOUNT_MISSING.value)
            if not extra_id.real_mount:
                blockers.append(BlockerCode.EXTRA_MOUNT_MISSING.value)
        else:
            if not data_id.real_mount:
                warnings.append(BlockerCode.DATA_MOUNT_MISSING.value)
            if not extra_id.real_mount:
                warnings.append(BlockerCode.EXTRA_MOUNT_MISSING.value)

    root_heavy = classification.mount_role == MountRole.ROOT and classification.path_class in {PathClass.UNKNOWN_ROOT_MASS, PathClass.HEAVY_WORKBENCH, PathClass.REBUILDABLE_CACHE}
    root_durable_truth = classification.mount_role == MountRole.ROOT and classification.truth_surface in {
        TruthSurface.RUNTIME_STATE,
        TruthSurface.AUDIT,
        TruthSurface.LOGS,
        TruthSurface.CRON_OUTPUT,
        TruthSurface.FINAL_EVIDENCE,
    }
    root_used = context.root_used_percent
    if root_used is not None:
        if root_used >= context.thresholds.root_emergency_percent:
            if classification.mount_role == MountRole.ROOT:
                blockers.append(BlockerCode.ROOT_ABOVE_EMERGENCY.value)
            else:
                warnings.append(BlockerCode.ROOT_ABOVE_EMERGENCY.value)
        elif root_used >= context.thresholds.root_block_percent:
            if classification.mount_role == MountRole.ROOT:
                blockers.append(BlockerCode.ROOT_ABOVE_BLOCK.value)
            else:
                warnings.append(BlockerCode.ROOT_ABOVE_BLOCK.value)
        elif root_used >= context.thresholds.root_warn_percent:
            warnings.append(BlockerCode.ROOT_ABOVE_WARN.value)

    root_override_requested = root_heavy and rollout.allow_root_override
    root_override_complete = root_override_requested and _has_complete_root_override(rollout)
    if root_override_complete:
        blockers = [blocker for blocker in blockers if blocker != BlockerCode.ROOT_HEAVY_WORK.value]
    if root_heavy and (rollout.block_new_root_heavy or rollout.host_mode == HostMode.REQUIRED_HERMES_HOST):
        if root_override_complete:
            warnings.append(BlockerCode.ROOT_HEAVY_WORK.value)
        else:
            blockers.append(BlockerCode.ROOT_HEAVY_WORK.value)
    elif root_heavy:
        warnings.append(BlockerCode.ROOT_HEAVY_WORK.value)
    if root_durable_truth and rollout.host_mode == HostMode.REQUIRED_HERMES_HOST:
        blockers.append(BlockerCode.DATA_MOUNT_MISSING.value)
    elif root_durable_truth:
        warnings.append(BlockerCode.DATA_MOUNT_MISSING.value)

    if root_override_requested and not root_override_complete:
        blockers.append(BlockerCode.UNAPPROVED_ROOT_OVERRIDE.value)

    base = Decision.BLOCK if blockers else (Decision.WARN if warnings else Decision.ALLOW)
    decision = _escalate(base, rollout.mode)
    return LifecycleDecision(decision, rollout.mode, rollout.host_mode, tuple(dict.fromkeys(blockers)), tuple(dict.fromkeys(warnings)), classification, tuple(mount_ids), "disk lifecycle decision: " + decision.value)


def evaluate_root_delta(before_bytes: int, after_bytes: int, *, expected_bytes: int = 0, mode: EnforcementMode = EnforcementMode.OBSERVE) -> LifecycleDecision:
    """Evaluate post-run root growth. Positive deltas beyond expectation are unaccounted."""
    delta = int(after_bytes) - int(before_bytes)
    blockers: list[str] = []
    warnings: list[str] = []
    if delta > max(0, int(expected_bytes)):
        if mode == EnforcementMode.BLOCK:
            blockers.append(BlockerCode.UNACCOUNTED_ROOT_DELTA.value)
        elif mode in {EnforcementMode.OBSERVE, EnforcementMode.WARN}:
            warnings.append(BlockerCode.UNACCOUNTED_ROOT_DELTA.value)
    decision = Decision.BLOCK if blockers else (Decision.WARN if warnings and mode == EnforcementMode.WARN else (Decision.OBSERVE if warnings else Decision.ALLOW))
    return LifecycleDecision(decision, mode, HostMode.COMPATIBILITY_HOST, tuple(blockers), tuple(warnings), message=f"root delta bytes={delta}")


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def validate_manifest(manifest: Mapping[str, Any], context: LifecycleContext | None = None, *, now: datetime | None = None) -> ManifestValidation:
    """Validate a hermes_artifact_manifest.v1 mapping."""
    context = context or LifecycleContext()
    now = now or datetime.now(timezone.utc)
    normalized = dict(manifest or {})
    blockers: list[str] = []
    warnings: list[str] = []

    for field_name in _REQUIRED_MANIFEST_FIELDS:
        if str(normalized.get(field_name) or "").strip() == "":
            blockers.append(f"{BlockerCode.MANIFEST_MISSING_FIELD.value}:{field_name}")

    if normalized.get("schema") and normalized.get("schema") != _SCHEMA:
        blockers.append(f"{BlockerCode.MANIFEST_INVALID_FIELD.value}:schema")

    for field_name, enum in (
        ("truth_surface", TruthSurface),
        ("cleanup_policy", CleanupPolicy),
        ("mount_role", MountRole),
        ("source_surface", Surface),
    ):
        if normalized.get(field_name) and not _is_valid_enum_value(enum, normalized.get(field_name)):
            blockers.append(f"{BlockerCode.MANIFEST_INVALID_FIELD.value}:{field_name}")

    created = _parse_iso_datetime(normalized.get("created_at"))
    remove_by = _parse_iso_datetime(normalized.get("remove_by"))
    if normalized.get("created_at") and created is None:
        blockers.append(BlockerCode.MANIFEST_INVALID_TIMESTAMP.value)
    if normalized.get("remove_by") and remove_by is None:
        blockers.append(BlockerCode.MANIFEST_INVALID_TIMESTAMP.value)
    if remove_by is not None and remove_by < now:
        blockers.append(BlockerCode.MANIFEST_EXPIRED.value)

    classification = classify_path(str(normalized.get("path") or ""), context)
    declared_role = _enum_value(MountRole, normalized.get("mount_role"), MountRole.UNKNOWN)
    declared_truth = _enum_value(TruthSurface, normalized.get("truth_surface"), TruthSurface.NONE)
    if classification.mount_role != MountRole.INVALID and declared_role != MountRole.UNKNOWN and declared_role != classification.mount_role:
        blockers.append(BlockerCode.MANIFEST_PATH_MOUNT_CONFLICT.value)
    if declared_truth == TruthSurface.REBUILDABLE and classification.mount_role == MountRole.DATA:
        blockers.append(BlockerCode.MANIFEST_TRUTH_SURFACE_CONFLICT.value)
    if declared_truth in {TruthSurface.RUNTIME_STATE, TruthSurface.AUDIT, TruthSurface.LOGS, TruthSurface.CRON_OUTPUT, TruthSurface.FINAL_EVIDENCE} and classification.mount_role == MountRole.EXTRA:
        blockers.append(BlockerCode.MANIFEST_TRUTH_SURFACE_CONFLICT.value)
    if classification.mount_role == MountRole.ROOT and declared_truth != TruthSurface.CONTROL_METADATA:
        blockers.append(BlockerCode.MANIFEST_PATH_MOUNT_CONFLICT.value)
    if context.mount_table and classification.mount_role in {MountRole.DATA, MountRole.EXTRA}:
        root = context.data_root if classification.mount_role == MountRole.DATA else context.extra_root
        identity = mount_identity_for(root, classification.mount_role, context.mount_table)
        manifest_device = str(normalized.get("mount_device_id") or "").strip()
        if not identity.real_mount or (manifest_device and identity.device_id and manifest_device != identity.device_id):
            blockers.append(BlockerCode.MANIFEST_PATH_MOUNT_CONFLICT.value)

    if not str(normalized.get("owner") or "").strip():
        blockers.append(BlockerCode.MANIFEST_OWNERLESS_RESIDUE.value)

    normalized["classification"] = _classification_to_dict(classification)
    return ManifestValidation(not blockers, tuple(dict.fromkeys(blockers)), tuple(dict.fromkeys(warnings)), normalized)


def validate_manifests(manifests: Sequence[Mapping[str, Any]], context: LifecycleContext | None = None) -> dict[str, Any]:
    results = [validate_manifest(item, context).to_dict() for item in manifests]
    return {
        "schema": "hermes_disk_lifecycle_manifest_report.v1",
        "count": len(results),
        "valid_count": sum(1 for item in results if item["valid"]),
        "invalid_count": sum(1 for item in results if not item["valid"]),
        "blockers": sorted({blocker for item in results for blocker in item["blockers"]}),
        "results": results,
    }


def lifecycle_report(*, context: LifecycleContext, paths: Sequence[str] = (), manifests: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Build a dry-run report from supplied paths/manifests."""
    decisions = [evaluate_path_request(path, context).to_dict() for path in paths]
    manifest_report = validate_manifests(manifests, context)
    return {
        "schema": "hermes_disk_lifecycle_report.v1",
        "profile": context.active_profile,
        "hermes_home": context.hermes_home,
        "rollout": {
            "mode": context.rollout.mode.value,
            "host_mode": context.rollout.host_mode.value,
            "require_manifest_on_closeout": context.rollout.require_manifest_on_closeout,
            "post_run_root_delta": context.rollout.post_run_root_delta.value,
        },
        "thresholds": context.thresholds.__dict__,
        "paths": decisions,
        "manifest_report": manifest_report,
        "blocked_attempts": sum(1 for item in decisions if item["decision"] == Decision.BLOCK.value),
        "warnings": sorted({warning for item in decisions for warning in item["warnings"]}),
    }


def resolver_contract(context: LifecycleContext) -> dict[str, str]:
    """Return the profile-aware lifecycle path contract adapters can log/test."""
    hermes_home = _clean_path(context.hermes_home)
    data_root = _clean_path(context.data_root)
    extra_root = _clean_path(context.extra_root)
    profile = context.active_profile or "default"
    return {
        "profile": profile,
        "hermes_home": hermes_home,
        "state_db_path": context.state_db_path or os.path.join(hermes_home, "state.db"),
        "logs_path": context.logs_path or os.path.join(hermes_home, "logs"),
        "cron_output_path": context.cron_output_path or os.path.join(hermes_home, "cron", "output"),
        "data_runtime_root": os.path.join(data_root, "profiles", profile, "runtime") if data_root else "",
        "data_evidence_root": context.evidence_root or (os.path.join(data_root, "profiles", profile, "evidence") if data_root else ""),
        "extra_workspace_root": context.workspace_root or (os.path.join(extra_root, "workspaces", profile) if extra_root else ""),
        "extra_cache_root": context.cache_root or (os.path.join(extra_root, "caches", profile) if extra_root else ""),
        "extra_toolchain_root": context.toolchain_root or (os.path.join(extra_root, "toolchains", profile) if extra_root else ""),
        "sandbox_root": context.sandbox_root or (os.path.join(extra_root, "sandboxes", profile) if extra_root else ""),
    }


def _classification_to_dict(item: PathClassification | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {"path": item.path, "path_class": item.path_class.value, "mount_role": item.mount_role.value, "truth_surface": item.truth_surface.value, "blockers": list(item.blockers)}


def _mount_identity_to_dict(item: MountIdentity) -> dict[str, Any]:
    return {"role": item.role.value, "path": item.path, "present": item.present, "real_mount": item.real_mount, "device_id": item.device_id, "fs_type": item.fs_type, "source": item.source, "root_device_id": item.root_device_id, "blockers": list(item.blockers)}
