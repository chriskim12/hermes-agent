"""Hermes-native retrieval contract for the insane-search adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class RetrievalVerdict(str, Enum):
    STRONG_OK = "strong_ok"
    WEAK_OK = "weak_ok"
    SUSPECT_OK = "suspect_ok"
    CHALLENGE = "challenge"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    AUTH_REQUIRED = "auth_required"
    PAYWALL = "paywall"
    NOT_FOUND = "not_found"
    SSRF_BLOCKED = "ssrf_blocked"
    SECRET_URL_BLOCKED = "secret_url_blocked"
    UNKNOWN = "unknown"


class SourceType(str, Enum):
    PRIMARY_EXTRACT = "primary_extract"
    PHASE0_PUBLIC_ROUTE = "phase0_public_route"
    INSANE_FALLBACK = "insane_fallback"
    BROWSER_DISCOVERED_PUBLIC_API = "browser_discovered_public_api"
    POLICY_STOP = "policy_stop"


@dataclass(slots=True)
class PublicBoundary:
    public_only: bool = True
    private_url: bool = False
    credentials_used: bool = False
    auth_or_paywall: bool = False
    stop_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TraceEntry:
    phase: str = ""
    executor: str = ""
    url: str = ""
    status: int = 0
    body_size: int = 0
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str | None = None
    url_transform: str = ""
    impersonate: str | None = None
    referer: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BrowserEscalation:
    needed: bool = False
    reason: str = ""
    allowed_actions: list[str] = field(default_factory=list)
    candidate_public_urls: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RetrievalResult:
    success: bool
    url: str
    final_url: str = ""
    title: str = ""
    content: str = ""
    raw_content: str = ""
    source_type: SourceType = SourceType.INSANE_FALLBACK
    route_used: str = ""
    verdict: RetrievalVerdict = RetrievalVerdict.UNKNOWN
    confidence: float = 0.0
    public_boundary: PublicBoundary = field(default_factory=PublicBoundary)
    trace: list[TraceEntry] = field(default_factory=list)
    untried_routes: list[str] = field(default_factory=list)
    browser_escalation: BrowserEscalation = field(default_factory=BrowserEscalation)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "content": self.content,
            "raw_content": self.raw_content,
            "source_type": self.source_type.value,
            "route_used": self.route_used,
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "public_boundary": self.public_boundary.to_dict(),
            "trace": [entry.to_dict() for entry in self.trace],
            "untried_routes": list(self.untried_routes),
            "browser_escalation": self.browser_escalation.to_dict(),
            "metadata": dict(self.metadata),
        }
