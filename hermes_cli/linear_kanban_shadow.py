"""Small Linear→Kanban compatibility helpers.

This module is intentionally narrow: it preserves deterministic idempotency keys
for historical Linear references without making Linear an execution authority.
"""

from __future__ import annotations

import re

_LINEAR_ID_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b", re.IGNORECASE)


def linear_idempotency_key(identifier: str) -> str:
    """Return the canonical Kanban idempotency key for a Linear identifier."""

    raw = str(identifier or "").strip()
    match = _LINEAR_ID_RE.search(raw)
    ident = (match.group(1) if match else raw).strip().upper()
    return f"linear:{ident}"
