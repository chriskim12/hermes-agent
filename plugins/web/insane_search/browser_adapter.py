"""Structured browser-escalation helpers for insane-search results.

This module intentionally does not drive a browser. It only normalizes the
Hermes-native escalation payload that a future, separately approved browser
reconnaissance slice may consume.
"""

from __future__ import annotations

from typing import Any

from plugins.web.insane_search.contract import BrowserEscalation, RetrievalResult
from plugins.web.insane_search.policy import evaluate_public_url

_ALLOWED_BROWSER_ACTIONS = [
    "browser_navigate",
    "browser_network_requests",
    "browser_snapshot",
]


def browser_escalation_needed(result: RetrievalResult | dict[str, Any]) -> dict[str, Any]:
    if isinstance(result, RetrievalResult):
        escalation = result.browser_escalation
    else:
        raw = result.get("browser_escalation") or {}
        escalation = BrowserEscalation(
            needed=bool(raw.get("needed")),
            reason=str(raw.get("reason") or ""),
            allowed_actions=[str(action) for action in raw.get("allowed_actions") or []],
            candidate_public_urls=[str(url) for url in raw.get("candidate_public_urls") or []],
        )
    if not escalation.needed:
        return BrowserEscalation().to_dict()
    allowed = [action for action in escalation.allowed_actions if action in _ALLOWED_BROWSER_ACTIONS]
    candidates = [
        url
        for url in dict.fromkeys(escalation.candidate_public_urls)
        if evaluate_public_url(url).allowed
    ]
    return BrowserEscalation(
        needed=True,
        reason=escalation.reason or "upstream_browser_escalation",
        allowed_actions=allowed,
        candidate_public_urls=candidates,
    ).to_dict()
