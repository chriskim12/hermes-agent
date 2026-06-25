"""Public-only URL policy for the insane-search provider."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from ipaddress import ip_address, ip_network
from urllib.parse import parse_qsl, unquote, urlsplit


class RetrievalVerdict(str, Enum):
    BLOCKED = "blocked"
    AUTH_REQUIRED = "auth_required"
    SSRF_BLOCKED = "ssrf_blocked"
    SECRET_URL_BLOCKED = "secret_url_blocked"
    UNKNOWN = "unknown"


_SECRET_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "code",
    "jwt",
    "key",
    "password",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
}
_PRIVATE_NETWORKS = tuple(
    ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
_LOCAL_HOSTS = {"localhost", "localhost.localdomain", "metadata.google.internal"}


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    verdict: RetrievalVerdict = RetrievalVerdict.UNKNOWN
    reason: str = ""


def evaluate_public_url(url: str) -> PolicyDecision:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return PolicyDecision(False, RetrievalVerdict.BLOCKED, "unsupported_url_scheme")
    if parsed.username or parsed.password:
        return PolicyDecision(False, RetrievalVerdict.SECRET_URL_BLOCKED, "url_userinfo_credentials")
    if _has_secret_material(parsed.query):
        return PolicyDecision(False, RetrievalVerdict.SECRET_URL_BLOCKED, "secret_url_query")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return PolicyDecision(False, RetrievalVerdict.BLOCKED, "missing_host")
    if _is_private_host(host):
        return PolicyDecision(False, RetrievalVerdict.SSRF_BLOCKED, "private_or_local_host")
    if _looks_like_auth_or_paywall_path(parsed.path):
        return PolicyDecision(False, RetrievalVerdict.AUTH_REQUIRED, "auth_or_paywall_path")
    return PolicyDecision(True)


def _has_secret_material(query: str) -> bool:
    for raw_key, value in parse_qsl(query, keep_blank_values=True):
        key = unquote(raw_key).strip().lower()
        if key in _SECRET_QUERY_KEYS or key.endswith("_token") or key.endswith("_secret"):
            return True
        if key in {"url", "redirect", "redirect_uri", "next"} and _has_secret_material(urlsplit(value).query):
            return True
    return False


def _is_private_host(host: str) -> bool:
    if host in _LOCAL_HOSTS or host.endswith(".local") or host.endswith(".localhost"):
        return True
    try:
        addr = ip_address(host.strip("[]"))
    except ValueError:
        addr = _parse_legacy_ipv4(host)
        if addr is None:
            return False
    return any(addr in network for network in _PRIVATE_NETWORKS) or addr.is_private or addr.is_loopback or addr.is_link_local


def _parse_legacy_ipv4(host: str):
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or any(part == "" for part in parts):
        return None
    try:
        values = [_parse_ipv4_int(part) for part in parts]
    except ValueError:
        return None
    if len(values) == 1:
        value = values[0]
        if value > 0xFFFFFFFF:
            return None
    elif len(values) == 2:
        if values[0] > 0xFF or values[1] > 0xFFFFFF:
            return None
        value = (values[0] << 24) | values[1]
    elif len(values) == 3:
        if values[0] > 0xFF or values[1] > 0xFF or values[2] > 0xFFFF:
            return None
        value = (values[0] << 24) | (values[1] << 16) | values[2]
    else:
        if any(part > 0xFF for part in values):
            return None
        value = (values[0] << 24) | (values[1] << 16) | (values[2] << 8) | values[3]
    return ip_address(value)


def _parse_ipv4_int(part: str) -> int:
    lowered = part.lower()
    if lowered.startswith("0x"):
        base = 16
    elif len(lowered) > 1 and lowered.startswith("0"):
        base = 8
    else:
        base = 10
    return int(lowered, base)


def _looks_like_auth_or_paywall_path(path: str) -> bool:
    lowered = unquote(path).lower()
    markers = ("/login", "/signin", "/sign-in", "/auth", "/oauth", "/subscribe", "/paywall")
    return any(marker in lowered for marker in markers)
