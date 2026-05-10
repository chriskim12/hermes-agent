"""Projection adapters for the Kanban Work Ledger Linear-exit path.

The adapters in this module are deliberately pure: they build bounded,
authority-labeled projection bodies and an idempotency decision for an external
surface, but they do not send Discord messages, mutate GitHub/Linear, or write
wiki files themselves.  Hermes remains the orchestrator that applies the
returned ``create``/``update``/``noop`` decision through the appropriate tool.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from hermes_cli import kanban_control_surface as control_surface
from hermes_cli.linear_kanban_shadow import linear_idempotency_key


PROJECTION_SCHEMA = "kanban_work_ledger_projection.v1"
PROJECTION_AUTHORITY = (
    "projection_only; source=kanban.work_ledger; "
    "authorities=kanban.tasks,kanban.task_runs,kanban.closeout_evidence"
)

_MARKER_RE = re.compile(
    r"<!--\s*hermes-kanban-projection\s+"
    r"key=\"(?P<key>[^\"]+)\"\s+"
    r"checksum=\"(?P<checksum>[a-f0-9]{16})\"\s+"
    r"authority=\"(?P<authority>[^\"]+)\"\s*-->",
)
_BLOCK_RE_TEMPLATE = (
    r"(?s)<!--\s*hermes-kanban-projection:start\s+key=\"{key}\"\s*-->"
    r".*?"
    r"<!--\s*hermes-kanban-projection:end\s+key=\"{key}\"\s*-->"
)


@dataclass(frozen=True)
class ExistingProjection:
    """A previously posted projection body and its editable reference."""

    body: str
    ref: Optional[str] = None


@dataclass(frozen=True)
class ProjectionEnvelope:
    """Outbound projection plus the idempotent action Hermes should take."""

    adapter: str
    key: str
    checksum: str
    authority: str
    body: str
    action: str
    existing_ref: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PROJECTION_SCHEMA,
            "adapter": self.adapter,
            "key": self.key,
            "checksum": self.checksum,
            "authority": self.authority,
            "body": self.body,
            "action": self.action,
            "existing_ref": self.existing_ref,
            "metadata": dict(self.metadata),
        }


def _projection_key(adapter: str, scope: str, tenant: Optional[str]) -> str:
    tenant_part = (tenant or "all").strip() or "all"
    scope_part = (scope or "default").strip() or "default"
    return f"{PROJECTION_SCHEMA}:{adapter}:{tenant_part}:{scope_part}"


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _marker(key: str, checksum: str) -> str:
    return (
        f"<!-- hermes-kanban-projection key=\"{key}\" "
        f"checksum=\"{checksum}\" authority=\"projection_only\" -->"
    )


def _coerce_existing(existing: Optional[Iterable[Any]]) -> list[ExistingProjection]:
    out: list[ExistingProjection] = []
    for item in existing or ():
        if isinstance(item, ExistingProjection):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(
                ExistingProjection(
                    body=str(item.get("body") or item.get("content") or ""),
                    ref=str(item.get("ref") or item.get("id") or item.get("url") or "") or None,
                )
            )
        else:
            out.append(ExistingProjection(body=str(item)))
    return out


def _find_existing(
    existing: Optional[Iterable[Any]],
    *,
    key: str,
    checksum: str,
) -> tuple[str, Optional[str]]:
    """Return ``(action, ref)`` for a projection retry.

    ``noop`` means the same key+checksum already exists.  ``update`` means the
    same idempotency key exists with different content, so callers should edit
    that item instead of posting a duplicate.  ``create`` means no projection
    with this key was found.
    """

    for item in _coerce_existing(existing):
        for match in _MARKER_RE.finditer(item.body):
            if match.group("key") != key:
                continue
            if match.group("checksum") == checksum:
                return ("noop", item.ref)
            return ("update", item.ref)
    return ("create", None)


def _entry_ref(entry: control_surface.LedgerEntry) -> str:
    return entry.public_id or entry.task_id


def _entry_line(entry: control_surface.LedgerEntry, *, include_evidence: bool) -> str:
    phase = entry.phase or "-"
    parts = [
        f"- `{_entry_ref(entry)}`",
        f"status={entry.status}",
        f"phase={phase}",
        f"next={control_surface.redact_secret_text(entry.next_action, limit=100)}",
    ]
    if include_evidence and entry.evidence:
        parts.append("evidence=" + ", ".join(entry.evidence[:3]))
    parts.append(f":: {control_surface.redact_secret_text(entry.title, limit=96)}")
    return " ".join(parts)


def _bucket_lines(
    surface: control_surface.LedgerSurface,
    *,
    include_evidence: bool = False,
    limit_per_bucket: int = 5,
) -> list[str]:
    lines: list[str] = []
    for bucket in control_surface.BUCKETS:
        entries = tuple(surface.queues.get(bucket, ()))[: max(0, limit_per_bucket)]
        if not entries:
            continue
        lines.append(f"### {bucket} ({len(entries)})")
        lines.extend(_entry_line(entry, include_evidence=include_evidence) for entry in entries)
    if not lines:
        lines.append("No active Work Ledger entries matched this projection.")
    return lines


def _counts_line(surface: control_surface.LedgerSurface) -> str:
    counts = surface.counts()
    return " ".join(f"{bucket}={counts[bucket]}" for bucket in control_surface.BUCKETS)


def _envelope(
    *,
    adapter: str,
    scope: str,
    tenant: Optional[str],
    content: str,
    existing: Optional[Iterable[Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> ProjectionEnvelope:
    key = _projection_key(adapter, scope, tenant)
    digest = _checksum(content)
    action, ref = _find_existing(existing, key=key, checksum=digest)
    body = "\n".join([
        _marker(key, digest),
        f"authority: {PROJECTION_AUTHORITY}",
        content.strip(),
        "",
    ])
    return ProjectionEnvelope(
        adapter=adapter,
        key=key,
        checksum=digest,
        authority=PROJECTION_AUTHORITY,
        body=body,
        action=action,
        existing_ref=ref,
        metadata={"tenant": tenant, **dict(metadata or {})},
    )


def build_discord_summary_projection(
    surface: control_surface.LedgerSurface,
    *,
    channel_ref: str,
    existing: Optional[Iterable[Any]] = None,
    limit_per_bucket: int = 4,
) -> ProjectionEnvelope:
    """Build a bounded Discord Work Ledger summary projection."""

    content = "\n".join([
        "**Kanban Work Ledger summary**",
        f"target: {control_surface.redact_secret_text(channel_ref, limit=80)}",
        f"counts: {_counts_line(surface)}",
        "",
        *_bucket_lines(surface, include_evidence=False, limit_per_bucket=limit_per_bucket),
        "",
        "_Projection only: verify source Kanban/GitHub evidence before closeout._",
    ])
    return _envelope(
        adapter="discord_summary",
        scope=channel_ref,
        tenant=surface.tenant,
        content=content,
        existing=existing,
        metadata={"channel_ref": channel_ref},
    )


def build_wiki_adr_projection(
    surface: control_surface.LedgerSurface,
    *,
    adr_ref: str = "kanban-work-ledger-linear-exit",
    existing: Optional[Iterable[Any]] = None,
) -> ProjectionEnvelope:
    """Build a wiki ADR projection describing projection authority boundaries."""

    content = "\n".join([
        f"# ADR: {adr_ref}",
        "",
        "## Status",
        "Accepted as projection-only operational record.",
        "",
        "## Decision",
        "Kanban remains the Work Ledger source used by Hermes orchestration. "
        "Discord, wiki, GitHub PR, and optional Linear comments are derived "
        "projections and must not be treated as closeout authority.",
        "",
        "## Authority",
        PROJECTION_AUTHORITY,
        "",
        "## Current ledger counts",
        _counts_line(surface),
    ])
    return _envelope(
        adapter="wiki_adr",
        scope=adr_ref,
        tenant=surface.tenant,
        content=content,
        existing=existing,
        metadata={"wiki_ref": adr_ref},
    )


def build_wiki_log_projection(
    surface: control_surface.LedgerSurface,
    *,
    log_ref: str = "kanban-work-ledger-log",
    existing: Optional[Iterable[Any]] = None,
    limit_per_bucket: int = 8,
) -> ProjectionEnvelope:
    """Build a wiki log projection from the compact Work Ledger surface."""

    content = "\n".join([
        f"# Work Ledger projection log: {log_ref}",
        "",
        f"generated_at: {surface.generated_at}",
        f"counts: {_counts_line(surface)}",
        "",
        *_bucket_lines(surface, include_evidence=True, limit_per_bucket=limit_per_bucket),
    ])
    return _envelope(
        adapter="wiki_log",
        scope=log_ref,
        tenant=surface.tenant,
        content=content,
        existing=existing,
        metadata={"wiki_ref": log_ref},
    )


def build_github_pr_evidence_projection(
    surface: control_surface.LedgerSurface,
    *,
    pr_ref: str,
    existing: Optional[Iterable[Any]] = None,
    limit_per_bucket: int = 6,
) -> ProjectionEnvelope:
    """Build a GitHub PR comment/check body with Work Ledger evidence pointers."""

    content = "\n".join([
        "## Kanban Work Ledger evidence",
        "",
        f"PR: {control_surface.redact_secret_text(pr_ref, limit=120)}",
        f"counts: {_counts_line(surface)}",
        "",
        *_bucket_lines(surface, include_evidence=True, limit_per_bucket=limit_per_bucket),
        "",
        "Closeout note: this is projection-only evidence; merge/close requires "
        "the repository verifier and source Kanban/GitHub checks.",
    ])
    return _envelope(
        adapter="github_pr_evidence",
        scope=pr_ref,
        tenant=surface.tenant,
        content=content,
        existing=existing,
        metadata={"pr_ref": pr_ref},
    )


def resolve_linear_reference(conn: Any, identifier: str) -> dict[str, Any]:
    """Resolve historical Linear references even when Linear comments are off.

    Unlike shadow task creation, this resolver intentionally includes archived
    rows so retired Linear domains keep historical references navigable.
    """

    key = linear_idempotency_key(identifier)
    ident = identifier.strip().upper()
    rows = conn.execute(
        """
        SELECT id, public_id, status, tenant, idempotency_key
          FROM tasks
         WHERE idempotency_key = ?
            OR public_id = ?
            OR id IN (SELECT task_id FROM task_aliases WHERE alias = ?)
         ORDER BY created_at DESC
        """,
        (key, ident, ident),
    ).fetchall()
    return {
        "identifier": ident,
        "idempotency_key": key,
        "status": "resolved" if rows else "missing",
        "matches": [
            {
                "task_id": row["id"],
                "public_id": row["public_id"],
                "status": row["status"],
                "tenant": row["tenant"],
                "idempotency_key": row["idempotency_key"],
            }
            for row in rows
        ],
    }


def build_linear_compatibility_projection(
    surface: control_surface.LedgerSurface,
    *,
    issue_identifier: str,
    conn: Any = None,
    enabled: bool = True,
    retired_domains: Iterable[str] = (),
    existing: Optional[Iterable[Any]] = None,
) -> ProjectionEnvelope:
    """Build an optional Linear compatibility comment projection.

    If disabled directly or by a retired tenant/domain, the returned envelope is
    ``skipped`` and contains no comment body, while metadata still carries the
    historical Linear→Kanban resolution when a DB connection is provided.
    """

    retired = {str(domain).strip().lower() for domain in retired_domains if str(domain).strip()}
    tenant = (surface.tenant or "").strip().lower()
    reference = resolve_linear_reference(conn, issue_identifier) if conn is not None else None
    if not enabled or (tenant and tenant in retired):
        key = _projection_key("linear_compatibility", issue_identifier.strip().upper(), surface.tenant)
        return ProjectionEnvelope(
            adapter="linear_compatibility",
            key=key,
            checksum="",
            authority=PROJECTION_AUTHORITY,
            body="",
            action="skipped",
            metadata={
                "tenant": surface.tenant,
                "issue_identifier": issue_identifier.strip().upper(),
                "disabled": True,
                "historical_reference": reference,
            },
        )

    content = "\n".join([
        "Kanban Work Ledger compatibility projection",
        f"Linear issue: {issue_identifier.strip().upper()}",
        f"counts: {_counts_line(surface)}",
        "",
        *_bucket_lines(surface, include_evidence=True, limit_per_bucket=5),
        "",
        "This Linear comment is compatibility-only; Kanban/GitHub evidence is authoritative.",
    ])
    return _envelope(
        adapter="linear_compatibility",
        scope=issue_identifier.strip().upper(),
        tenant=surface.tenant,
        content=content,
        existing=existing,
        metadata={
            "issue_identifier": issue_identifier.strip().upper(),
            "historical_reference": reference,
        },
    )


def upsert_projection_block(existing_text: str, projection: ProjectionEnvelope) -> str:
    """Insert or replace a projection block in wiki-style markdown text."""

    start = f"<!-- hermes-kanban-projection:start key=\"{projection.key}\" -->"
    end = f"<!-- hermes-kanban-projection:end key=\"{projection.key}\" -->"
    block = f"{start}\n{projection.body.rstrip()}\n{end}"
    pattern = re.compile(_BLOCK_RE_TEMPLATE.format(key=re.escape(projection.key)))
    text = existing_text or ""
    if pattern.search(text):
        return pattern.sub(block, text)
    separator = "\n\n" if text.strip() else ""
    return text.rstrip() + separator + block + "\n"


__all__ = [
    "ExistingProjection",
    "ProjectionEnvelope",
    "PROJECTION_AUTHORITY",
    "PROJECTION_SCHEMA",
    "build_discord_summary_projection",
    "build_github_pr_evidence_projection",
    "build_linear_compatibility_projection",
    "build_wiki_adr_projection",
    "build_wiki_log_projection",
    "resolve_linear_reference",
    "upsert_projection_block",
]
