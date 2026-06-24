"""Hermes insane-search public retrieval plugin package.

Slice 1 vendors the upstream engine only; Hermes adapter registration is added in later Ultragoal slices.
"""

from __future__ import annotations


def register(ctx) -> None:
    """Reserved plugin entry point; no live providers are registered in vendor-only slice."""
    return None
