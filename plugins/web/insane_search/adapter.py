"""Hermes adapter layer for the vendored insane-search engine."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from plugins.web.insane_search.contract import (
    BrowserEscalation,
    PublicBoundary,
    RetrievalResult,
    RetrievalVerdict,
    SourceType,
    TraceEntry,
)
from plugins.web.insane_search.policy import evaluate_public_url, policy_stop_result
from plugins.web.insane_search.readiness import check_readiness


_OK_CONFIDENCE = {
    RetrievalVerdict.STRONG_OK: 0.95,
    RetrievalVerdict.WEAK_OK: 0.72,
    RetrievalVerdict.SUSPECT_OK: 0.35,
}
_VERDICT_ALIASES = {
    "ok": RetrievalVerdict.WEAK_OK,
    "auth": RetrievalVerdict.AUTH_REQUIRED,
    "authentication_required": RetrievalVerdict.AUTH_REQUIRED,
    "forbidden": RetrievalVerdict.BLOCKED,
    "not_exhausted": RetrievalVerdict.UNKNOWN,
}
_AUTH_PAYWALL_MARKERS = ("auth", "login", "sign in", "signin", "paywall", "subscribe")


EngineFetch = Callable[..., Any]


def resolve_public_url(
    url: str,
    *,
    engine_fetch: EngineFetch | None = None,
    include_diagnostics: bool = False,
    **engine_kwargs: Any,
) -> RetrievalResult:
    """Run policy/readiness gates and map insane-search output to Hermes contract."""
    decision = evaluate_public_url(url)
    if not decision.allowed:
        return policy_stop_result(url, decision)

    readiness = check_readiness()
    if engine_fetch is None:
        try:
            from plugins.web.insane_search.vendor.insane_search_engine.fetch_chain import fetch as engine_fetch
        except Exception as exc:  # pragma: no cover - exercised by tests via fake fetch
            return RetrievalResult(
                success=False,
                url=url,
                final_url=url,
                source_type=SourceType.INSANE_FALLBACK,
                route_used="readiness",
                verdict=RetrievalVerdict.UNKNOWN,
                public_boundary=PublicBoundary(stop_reason="resolver_unavailable"),
                metadata={"readiness": readiness.to_dict(), "error": f"{type(exc).__name__}: {exc}"},
            )
    engine_kwargs["enable_playwright"] = False

    result = engine_fetch(url, **engine_kwargs)
    mapped = map_fetch_result(url, result, include_diagnostics=include_diagnostics)
    if include_diagnostics:
        mapped.metadata.setdefault("readiness", readiness.to_dict())
    return mapped


def map_fetch_result(
    url: str,
    fetch_result: Any,
    *,
    source_type: SourceType = SourceType.INSANE_FALLBACK,
    include_diagnostics: bool = False,
) -> RetrievalResult:
    verdict = _normalize_verdict(_get(fetch_result, "verdict", "unknown"))
    content = str(_get(fetch_result, "content", "") or "")
    final_url = str(_get(fetch_result, "final_url", "") or url)
    original_decision = evaluate_public_url(url)
    if not original_decision.allowed:
        return RetrievalResult(
            success=False,
            url=url,
            final_url=url,
            source_type=SourceType.POLICY_STOP,
            route_used="policy:input_url",
            verdict=original_decision.verdict,
            confidence=1.0,
            public_boundary=original_decision.to_boundary(),
            metadata={"policy_reason": original_decision.reason},
        )
    trace = [_trace_entry(item) for item in list(_get(fetch_result, "trace", []) or [])]
    untried_routes = [str(route) for route in list(_get(fetch_result, "untried_routes", []) or [])]
    stop_reason = str(_get(fetch_result, "stop_reason", "") or "")
    ok = bool(_get(fetch_result, "ok", False)) and verdict in {
        RetrievalVerdict.STRONG_OK,
        RetrievalVerdict.WEAK_OK,
    }
    browser_needed = bool(_get(fetch_result, "must_invoke_playwright_mcp", False))
    if browser_needed:
        untried_routes = untried_routes or ["playwright_mcp"]
    metadata: dict[str, Any] = {
        "profile_used": _get(fetch_result, "profile_used", None),
        "summary": _get(fetch_result, "summary", ""),
        "planned_attempts": _get(fetch_result, "planned_attempts", 0),
        "executed_attempts": _get(fetch_result, "executed_attempts", len(trace)),
        "grid_exhausted": _get(fetch_result, "grid_exhausted", False),
        "stop_reason": stop_reason,
    }
    if include_diagnostics and hasattr(fetch_result, "to_dict"):
        metadata["insane_search"] = fetch_result.to_dict()
    final_decision = evaluate_public_url(final_url)
    if not final_decision.allowed:
        return RetrievalResult(
            success=False,
            url=url,
            final_url=final_url,
            source_type=SourceType.POLICY_STOP,
            route_used="policy:final_url",
            verdict=final_decision.verdict,
            confidence=1.0,
            public_boundary=final_decision.to_boundary(),
            trace=trace,
            untried_routes=untried_routes,
            metadata={**metadata, "policy_reason": final_decision.reason},
        )
    return RetrievalResult(
        success=ok,
        url=url,
        final_url=final_url,
        content=content,
        raw_content=content,
        source_type=source_type,
        route_used=_route_used(trace, source_type),
        verdict=verdict,
        confidence=_confidence(verdict, trace),
        public_boundary=_public_boundary(verdict, stop_reason),
        trace=trace,
        untried_routes=untried_routes,
        browser_escalation=BrowserEscalation(
            needed=browser_needed,
            reason="upstream_mcp_required" if browser_needed else "",
            allowed_actions=["browser_navigate", "browser_network_requests", "browser_snapshot"] if browser_needed else [],
            candidate_public_urls=[final_url] if browser_needed and final_url else [],
        ),
        metadata=metadata,
    )


def _normalize_verdict(value: Any) -> RetrievalVerdict:
    raw = str(value or "unknown").strip().lower()
    if raw in _VERDICT_ALIASES:
        return _VERDICT_ALIASES[raw]
    for verdict in RetrievalVerdict:
        if verdict.value == raw:
            return verdict
    if any(marker in raw for marker in _AUTH_PAYWALL_MARKERS):
        return RetrievalVerdict.PAYWALL if "paywall" in raw or "subscribe" in raw else RetrievalVerdict.AUTH_REQUIRED
    return RetrievalVerdict.UNKNOWN


def _trace_entry(item: Any) -> TraceEntry:
    if hasattr(item, "to_dict"):
        item = item.to_dict()
    data = item if isinstance(item, dict) else {}
    return TraceEntry(
        phase=str(data.get("phase") or ""),
        executor=str(data.get("executor") or ""),
        url=str(data.get("url") or ""),
        status=int(data.get("status") or 0),
        body_size=int(data.get("body_size") or 0),
        verdict=str(data.get("verdict") or ""),
        reasons=[str(reason) for reason in list(data.get("reasons") or [])],
        elapsed_s=float(data.get("elapsed_s") or 0.0),
        error=data.get("error"),
        url_transform=str(data.get("url_transform") or ""),
        impersonate=data.get("impersonate"),
        referer=str(data.get("referer") or ""),
    )


def _route_used(trace: list[TraceEntry], source_type: SourceType) -> str:
    for entry in reversed(trace):
        if entry.verdict in {"strong_ok", "weak_ok"}:
            return ":".join(part for part in (entry.phase, entry.executor, entry.url_transform) if part)
    return source_type.value


def _confidence(verdict: RetrievalVerdict, trace: list[TraceEntry]) -> float:
    if verdict in _OK_CONFIDENCE:
        return _OK_CONFIDENCE[verdict]
    if any(entry.verdict in {"challenge", "blocked"} for entry in trace):
        return 0.1
    if verdict in {RetrievalVerdict.AUTH_REQUIRED, RetrievalVerdict.PAYWALL, RetrievalVerdict.SSRF_BLOCKED, RetrievalVerdict.SECRET_URL_BLOCKED}:
        return 1.0
    return 0.0


def _public_boundary(verdict: RetrievalVerdict, stop_reason: str) -> PublicBoundary:
    return PublicBoundary(
        public_only=True,
        private_url=verdict is RetrievalVerdict.SSRF_BLOCKED,
        credentials_used=False,
        auth_or_paywall=verdict in {RetrievalVerdict.AUTH_REQUIRED, RetrievalVerdict.PAYWALL},
        stop_reason=stop_reason or (verdict.value if verdict in {RetrievalVerdict.AUTH_REQUIRED, RetrievalVerdict.PAYWALL} else ""),
    )


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
