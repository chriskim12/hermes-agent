"""Hermes insane-search public retrieval and search provider plugin."""

from __future__ import annotations

from plugins.web.insane_search.provider import InsaneWebSearchProvider


def register(ctx) -> None:
    """Register the opt-in safe insane web_search provider."""
    ctx.register_web_search_provider(InsaneWebSearchProvider())
