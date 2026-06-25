"""Hermes-native safe insane web_search provider.

The provider ports the safe public-route subset of GJC's insane-search provider
surface: direct supported URL routing plus keyless query discovery/enrichment for
Reddit, X/Twitter, YouTube, and Hacker News. It is intentionally search-only and
explicit opt-in; extraction fallback, browser automation, cookies, and credentialed
routes are outside this provider surface.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.web_search_provider import WebSearchProvider
from plugins.web.insane_search.routes import (
    DiscoveryFn,
    FetchFn,
    PublicRouteError,
    RouteCandidate,
    UnsafeRouteError,
    candidate_from_url,
    clamp_limit,
    dedupe_key,
    default_discover,
    default_fetch,
    enrich_candidate,
    is_probable_url,
)

logger = logging.getLogger(__name__)


class InsaneWebSearchProvider(WebSearchProvider):
    """Keyless, public-only, search-only route-enrichment provider."""

    def __init__(
        self,
        *,
        fetcher: FetchFn | None = None,
        discovery: DiscoveryFn | None = None,
    ) -> None:
        self._fetcher = fetcher or default_fetch
        self._discovery = discovery

    @property
    def name(self) -> str:
        return "insane"

    @property
    def display_name(self) -> str:
        return "Insane Search (safe public routes)"

    def is_available(self) -> bool:
        # Keyless and no optional import is required for the safe provider path.
        # Network availability is intentionally not probed here.
        return True

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def auto_selectable(self) -> bool:
        """Keep keyless insane inert unless explicitly configured."""
        return False

    def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        safe_limit = clamp_limit(limit)
        raw_query = str(query or "").strip()
        if not raw_query:
            return {"success": False, "error": "insane search query is empty"}

        try:
            if is_probable_url(raw_query):
                candidates = [candidate_from_url(raw_query, discovery_source="direct")]
                mode = "direct_url"
            else:
                candidates = self._discover(raw_query, safe_limit)
                mode = "query_discovery"
        except UnsafeRouteError as exc:
            return self._error(
                f"Blocked unsafe URL: {exc.decision.reason or exc.decision.verdict.value}",
                mode="policy_stop",
                rejected=[{"url": exc.url, "reason": exc.decision.reason}],
            )
        except PublicRouteError as exc:
            return self._error(f"Unsupported public route: {exc}", mode="unsupported_route")

        if not candidates:
            return self._error(
                "No supported public-route candidates discovered for reddit, x/twitter, youtube, or hackernews",
                mode=mode,
            )

        web_results: list[dict[str, Any]] = []
        rejected: list[dict[str, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if len(web_results) >= safe_limit:
                break
            key = dedupe_key(candidate.url)
            if key in seen:
                continue
            seen.add(key)
            try:
                enriched = enrich_candidate(candidate, self._fetcher)
            except UnsafeRouteError as exc:
                rejected.append({"url": exc.url, "reason": exc.decision.reason or exc.decision.verdict.value})
                continue
            except PublicRouteError as exc:
                rejected.append({"url": candidate.url, "reason": str(exc)})
                continue
            result_key = dedupe_key(enriched.url)
            if result_key in seen and result_key != key:
                continue
            seen.add(result_key)
            web_results.append(enriched.to_web_result(len(web_results) + 1))

        if not web_results:
            return self._error(
                "All supported public-route candidates failed closed",
                mode=mode,
                rejected=rejected,
            )

        logger.info("Insane search '%s': %d enriched results", raw_query, len(web_results))
        return {
            "success": True,
            "data": {
                "web": web_results,
                "provider": "insane",
                "mode": mode,
                "public_only": True,
                "route_platforms": ["reddit", "x", "youtube", "hackernews"],
                "rejected": rejected,
            },
        }

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Insane Search (safe public routes)",
            "badge": "free · no key · search only · opt-in",
            "tag": (
                "Public-route enrichment for Reddit, X/Twitter, YouTube, and Hacker News. "
                "No browser/cookies/bypass; pair with an extract provider for web_extract."
            ),
            "env_vars": [],
        }

    def _discover(self, query: str, limit: int) -> list[RouteCandidate]:
        if self._discovery is not None:
            return self._discovery(query, limit)
        return default_discover(query, limit, self._fetcher)

    @staticmethod
    def _error(
        message: str,
        *,
        mode: str,
        rejected: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "error": message,
            "data": {
                "web": [],
                "provider": "insane",
                "mode": mode,
                "public_only": True,
                "rejected": rejected or [],
            },
        }
