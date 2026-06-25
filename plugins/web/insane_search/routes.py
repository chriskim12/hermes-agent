"""Safe public-route discovery and enrichment for the insane search provider.

This module intentionally implements only public, keyless HTTP(S) routes. It does
not use browser automation, cookies, credentialed APIs, TLS impersonation, or
vendored extraction engines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from plugins.web.insane_search.policy import PolicyDecision, evaluate_public_url


_MAX_RESULTS = 20
_DEFAULT_RESULTS = 10
_HTTP_TIMEOUT_S = 12.0
_USER_AGENT = "HermesAgent/insane-search public-route resolver"
_BLOCK_MARKERS = (
    "captcha",
    "cf-challenge",
    "cloudflare challenge",
    "access denied",
    "unusual traffic",
    "please log in",
    "login required",
    "sign in to continue",
    "subscribe to continue",
    "paywall",
)


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    """A public URL candidate that may be enriched through a supported route."""

    url: str
    platform: str
    route_hint: str = ""
    title: str = ""
    snippet: str = ""
    discovery_source: str = ""


@dataclass(slots=True)
class RouteResult:
    """Hermes web_search-shaped result plus insane-route metadata."""

    title: str
    url: str
    description: str
    route_used: str
    platform: str
    source_url: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_web_result(self, position: int) -> dict[str, Any]:
        payload = {
            "title": self.title,
            "url": self.url,
            "description": self.description,
            "position": position,
            "route_used": self.route_used,
            "source": "insane",
            "insane_search": {
                "platform": self.platform,
                "source_url": self.source_url,
                "route_used": self.route_used,
                "public_only": True,
            },
        }
        if self.metadata:
            payload["insane_search"].update(self.metadata)
        return payload


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Small response shim used by tests and by the default httpx fetcher."""

    url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.text)


FetchFn = Callable[[str], FetchResponse]
DiscoveryFn = Callable[[str, int], list[RouteCandidate]]


class PublicRouteError(RuntimeError):
    """Raised when a candidate cannot be safely enriched."""


class UnsafeRouteError(PublicRouteError):
    """Raised when policy rejects a route or candidate."""

    def __init__(self, url: str, decision: PolicyDecision):
        super().__init__(decision.reason or decision.verdict.value)
        self.url = url
        self.decision = decision


def clamp_limit(limit: int | str | None) -> int:
    try:
        parsed = int(limit or _DEFAULT_RESULTS)
    except (TypeError, ValueError):
        parsed = _DEFAULT_RESULTS
    return min(max(parsed, 1), _MAX_RESULTS)


def is_probable_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def supported_platform(url: str) -> str | None:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None
    if host == "redd.it" or host.endswith(".redd.it") or host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit"
    if host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com"):
        return "x"
    if host == "youtu.be" or host.endswith(".youtu.be") or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "news.ycombinator.com" or host == "hn.algolia.com":
        return "hackernews"
    return None


def candidate_from_url(url: str, *, discovery_source: str = "direct") -> RouteCandidate:
    original_decision = evaluate_public_url(url)
    if not original_decision.allowed:
        raise UnsafeRouteError(url, original_decision)
    normalized = normalize_url(url)
    decision = evaluate_public_url(normalized)
    if not decision.allowed:
        raise UnsafeRouteError(normalized, decision)
    platform = supported_platform(normalized)
    if platform is None:
        raise PublicRouteError("unsupported_public_route")
    return RouteCandidate(url=normalized, platform=platform, discovery_source=discovery_source)


def enrich_candidate(candidate: RouteCandidate, fetch: FetchFn | None = None) -> RouteResult:
    fetch = fetch or default_fetch
    decision = evaluate_public_url(candidate.url)
    if not decision.allowed:
        raise UnsafeRouteError(candidate.url, decision)
    platform = candidate.platform or supported_platform(candidate.url)
    if platform == "reddit":
        return _enrich_reddit(candidate, fetch)
    if platform == "x":
        return _enrich_x(candidate, fetch)
    if platform == "youtube":
        return _enrich_youtube(candidate, fetch)
    if platform == "hackernews":
        return _enrich_hackernews(candidate, fetch)
    raise PublicRouteError("unsupported_public_route")


def default_discover(query: str, limit: int, fetch: FetchFn | None = None) -> list[RouteCandidate]:
    """Discover public supported candidates through DuckDuckGo HTML routes."""

    fetch = fetch or default_fetch
    variants = _query_variants(query)
    candidates: list[RouteCandidate] = []
    seen: set[str] = set()
    for variant in variants:
        if len(candidates) >= limit:
            break
        route_url = "https://duckduckgo.com/html/?" + urlencode({"q": variant})
        response = _safe_fetch(route_url, fetch, route="duckduckgo_html")
        links = _parse_duckduckgo_links(response.text)
        for link in links:
            if len(candidates) >= limit:
                break
            try:
                candidate = candidate_from_url(link.url, discovery_source="duckduckgo")
            except PublicRouteError:
                continue
            key = dedupe_key(candidate.url)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                RouteCandidate(
                    url=candidate.url,
                    platform=candidate.platform,
                    title=link.title,
                    snippet=link.snippet,
                    discovery_source="duckduckgo",
                )
            )
    return candidates


def default_fetch(url: str) -> FetchResponse:
    decision = evaluate_public_url(url)
    if not decision.allowed:
        raise UnsafeRouteError(url, decision)
    current_url = url
    with httpx.Client(
        timeout=_HTTP_TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for _ in range(6):
            response = client.get(current_url)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location", "")
            if not location:
                break
            next_url = urljoin(str(response.url), location)
            redirect_decision = evaluate_public_url(next_url)
            if not redirect_decision.allowed:
                raise UnsafeRouteError(next_url, redirect_decision)
            current_url = next_url
        else:
            raise PublicRouteError("too_many_redirects")
    final_url = str(response.url)
    final_decision = evaluate_public_url(final_url)
    if not final_decision.allowed:
        raise UnsafeRouteError(final_url, final_decision)
    return FetchResponse(
        url=final_url,
        status_code=response.status_code,
        text=response.text,
        headers={k.lower(): v for k, v in response.headers.items()},
    )


def normalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if not scheme and host:
        scheme = "https"
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def dedupe_key(url: str) -> str:
    parsed = urlsplit(normalize_url(url))
    query_pairs = []
    for key, values in sorted(parse_qs(parsed.query, keep_blank_values=True).items()):
        if key.lower().startswith("utm_") or key.lower() in {"ref", "fbclid", "gclid"}:
            continue
        for value in values:
            query_pairs.append((key, value))
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _safe_fetch(url: str, fetch: FetchFn, *, route: str) -> FetchResponse:
    decision = evaluate_public_url(url)
    if not decision.allowed:
        raise UnsafeRouteError(url, decision)
    response = fetch(url)
    final_url = response.url or url
    final_decision = evaluate_public_url(final_url)
    if not final_decision.allowed:
        raise UnsafeRouteError(final_url, final_decision)
    if response.status_code in {401, 402, 403, 407, 451}:
        raise PublicRouteError(f"{route}_blocked_status_{response.status_code}")
    if response.status_code == 429:
        raise PublicRouteError(f"{route}_rate_limited")
    if response.status_code >= 400:
        raise PublicRouteError(f"{route}_http_{response.status_code}")
    marker = _blocked_marker(response.text)
    if marker:
        raise PublicRouteError(f"{route}_blocked_marker_{marker}")
    return response


def _safe_result_url(url: str, fallback: str) -> str:
    decision = evaluate_public_url(url)
    return url if decision.allowed else fallback


def _blocked_marker(text: str) -> str:
    lowered = text[:20000].lower()
    for marker in _BLOCK_MARKERS:
        if marker in lowered:
            return marker.replace(" ", "_")
    return ""


def _enrich_reddit(candidate: RouteCandidate, fetch: FetchFn) -> RouteResult:
    route_url = _reddit_rss_url(candidate.url)
    response = _safe_fetch(route_url, fetch, route="reddit_rss")
    items = _parse_rss_items(response.text)
    if not items:
        raise PublicRouteError("reddit_rss_empty")
    first = items[0]
    return RouteResult(
        title=first.get("title") or candidate.title or "Reddit result",
        url=_safe_result_url(first.get("link") or candidate.url, candidate.url),
        description=first.get("description") or candidate.snippet or "Public Reddit RSS route result",
        route_used="reddit_rss",
        platform="reddit",
        source_url=candidate.url,
        metadata={"route_url": route_url, "discovery_source": candidate.discovery_source},
    )


def _reddit_rss_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if host.endswith("redd.it"):
        item_id = path.strip("/").split("/")[0]
        return f"https://www.reddit.com/comments/{item_id}/.rss"
    if not path:
        path = "/"
    if path.endswith("/.rss"):
        return urlunsplit(("https", parsed.netloc, path, parsed.query, ""))
    return urlunsplit(("https", parsed.netloc, path + "/.rss", parsed.query, ""))


def _enrich_x(candidate: RouteCandidate, fetch: FetchFn) -> RouteResult:
    parsed = urlsplit(candidate.url)
    parts = [part for part in parsed.path.split("/") if part]
    handle = parts[0] if parts else ""
    status_id = ""
    if len(parts) >= 3 and parts[1].lower() in {"status", "statuses"}:
        status_id = parts[2]
    if status_id:
        route_url = "https://publish.twitter.com/oembed?" + urlencode({"url": candidate.url, "omit_script": "1"})
        response = _safe_fetch(route_url, fetch, route="x_oembed")
        data = _json_object(response)
        author = str(data.get("author_name") or handle or "X")
        title = candidate.title or f"X post by {author}"
        description = _strip_html(str(data.get("html") or "")).strip() or candidate.snippet or "Public X oEmbed route result"
        return RouteResult(
            title=title,
            url=_safe_result_url(str(data.get("url") or candidate.url), candidate.url),
            description=description,
            route_used="x_oembed",
            platform="x",
            source_url=candidate.url,
            metadata={"route_url": route_url, "author_name": author, "discovery_source": candidate.discovery_source},
        )
    if handle:
        # X exposes very little unauthenticated profile metadata through stable public JSON.
        # A profile URL itself is still a public route candidate; return metadata without
        # escalating to browser/cookies or pretending private content was retrieved.
        return RouteResult(
            title=candidate.title or f"X profile @{handle}",
            url=candidate.url,
            description=candidate.snippet or f"Public X profile route for @{handle}",
            route_used="x_profile_metadata",
            platform="x",
            source_url=candidate.url,
            metadata={"handle": handle, "discovery_source": candidate.discovery_source},
        )
    raise PublicRouteError("x_route_unrecognized")


def _enrich_youtube(candidate: RouteCandidate, fetch: FetchFn) -> RouteResult:
    video_id = _youtube_video_id(candidate.url)
    if video_id:
        route_url = "https://www.youtube.com/oembed?" + urlencode({"url": candidate.url, "format": "json"})
        response = _safe_fetch(route_url, fetch, route="youtube_oembed")
        data = _json_object(response)
        title = str(data.get("title") or candidate.title or "YouTube video")
        author = str(data.get("author_name") or "")
        return RouteResult(
            title=title,
            url=candidate.url,
            description=candidate.snippet or (f"YouTube video by {author}" if author else "Public YouTube oEmbed route result"),
            route_used="youtube_oembed",
            platform="youtube",
            source_url=candidate.url,
            metadata={"route_url": route_url, "video_id": video_id, "author_name": author, "discovery_source": candidate.discovery_source},
        )
    feed_url = _youtube_feed_url(candidate.url)
    if feed_url:
        response = _safe_fetch(feed_url, fetch, route="youtube_feed")
        items = _parse_atom_entries(response.text)
        if not items:
            raise PublicRouteError("youtube_feed_empty")
        first = items[0]
        return RouteResult(
            title=first.get("title") or candidate.title or "YouTube feed result",
            url=_safe_result_url(first.get("link") or candidate.url, candidate.url),
            description=first.get("description") or candidate.snippet or "Public YouTube feed route result",
            route_used="youtube_feed",
            platform="youtube",
            source_url=candidate.url,
            metadata={"route_url": feed_url, "discovery_source": candidate.discovery_source},
        )
    raise PublicRouteError("youtube_route_unrecognized")


def _youtube_video_id(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if host.endswith("youtube.com"):
        query = parse_qs(parsed.query)
        if query.get("v"):
            return query["v"][0]
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return parts[1]
    return ""


def _youtube_feed_url(url: str) -> str:
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel":
        return "https://www.youtube.com/feeds/videos.xml?" + urlencode({"channel_id": parts[1]})
    return ""


def _enrich_hackernews(candidate: RouteCandidate, fetch: FetchFn) -> RouteResult:
    item_id = _hackernews_item_id(candidate.url)
    if item_id:
        route_url = f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
        response = _safe_fetch(route_url, fetch, route="hackernews_item_api")
        data = _json_object(response)
        title = str(data.get("title") or candidate.title or f"Hacker News item {item_id}")
        hn_url = f"https://news.ycombinator.com/item?id={item_id}"
        target_url = _safe_result_url(str(data.get("url") or hn_url), hn_url)
        description_bits = []
        if data.get("by"):
            description_bits.append(f"by {data['by']}")
        if data.get("score") is not None:
            description_bits.append(f"{data['score']} points")
        if data.get("descendants") is not None:
            description_bits.append(f"{data['descendants']} comments")
        description = candidate.snippet or "; ".join(description_bits) or "Public Hacker News item API route result"
        return RouteResult(
            title=title,
            url=target_url,
            description=description,
            route_used="hackernews_item_api",
            platform="hackernews",
            source_url=candidate.url,
            metadata={"route_url": route_url, "hn_url": hn_url, "item_id": item_id, "discovery_source": candidate.discovery_source},
        )
    if "hn.algolia.com" in (urlsplit(candidate.url).hostname or ""):
        return RouteResult(
            title=candidate.title or "Hacker News Algolia result",
            url=candidate.url,
            description=candidate.snippet or "Public Hacker News Algolia route result",
            route_used="hackernews_algolia_public",
            platform="hackernews",
            source_url=candidate.url,
            metadata={"discovery_source": candidate.discovery_source},
        )
    raise PublicRouteError("hackernews_route_unrecognized")


def _hackernews_item_id(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower() == "news.ycombinator.com":
        query = parse_qs(parsed.query)
        if query.get("id") and query["id"][0].isdigit():
            return query["id"][0]
    match = re.search(r"/(?:item|items)/(\d+)", parsed.path)
    if match:
        return match.group(1)
    return ""


def _query_variants(query: str) -> list[str]:
    stripped = query.strip()
    variants = [stripped]
    lowered = stripped.lower()
    platform_terms = {
        "reddit": "site:reddit.com OR site:redd.it",
        "x": "site:x.com OR site:twitter.com",
        "twitter": "site:x.com OR site:twitter.com",
        "youtube": "site:youtube.com OR site:youtu.be",
        "hacker news": "site:news.ycombinator.com OR site:hn.algolia.com",
        "hn": "site:news.ycombinator.com OR site:hn.algolia.com",
    }
    for marker, site_filter in platform_terms.items():
        if marker in lowered:
            variants.append(f"{stripped} {site_filter}")
    variants.extend([
        f"{stripped} site:reddit.com OR site:redd.it",
        f"{stripped} site:x.com OR site:twitter.com",
        f"{stripped} site:youtube.com OR site:youtu.be",
        f"{stripped} site:news.ycombinator.com OR site:hn.algolia.com",
    ])
    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        if variant and variant not in seen:
            seen.add(variant)
            deduped.append(variant)
    return deduped


def _json_object(response: FetchResponse) -> dict[str, Any]:
    data = response.json()
    if not isinstance(data, dict):
        raise PublicRouteError("route_json_not_object")
    return data


def _parse_rss_items(text: str) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise PublicRouteError("rss_parse_failed") from exc
    items: list[dict[str, str]] = []
    for item in root.findall(".//item"):
        items.append({
            "title": _element_text(item, "title"),
            "link": _element_text(item, "link"),
            "description": _strip_html(_element_text(item, "description")),
        })
    return items


def _parse_atom_entries(text: str) -> list[dict[str, str]]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise PublicRouteError("atom_parse_failed") from exc
    ns = {"atom": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/"}
    entries: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", ns):
        link = ""
        link_el = entry.find("atom:link", ns)
        if link_el is not None:
            link = str(link_el.attrib.get("href") or "")
        desc = _element_text(entry, "media:group/media:description", ns)
        entries.append({
            "title": _element_text(entry, "atom:title", ns),
            "link": link,
            "description": _strip_html(desc),
        })
    return entries


def _element_text(root: ElementTree.Element, path: str, ns: dict[str, str] | None = None) -> str:
    el = root.find(path, ns or {})
    return "" if el is None or el.text is None else str(el.text).strip()


@dataclass(frozen=True, slots=True)
class _DiscoveredLink:
    url: str
    title: str = ""
    snippet: str = ""


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[_DiscoveredLink] = []
        self._href: str = ""
        self._class: str = ""
        self._text: list[str] = []
        self._in_result = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        cls = attr.get("class", "")
        if tag == "a" and ("result__a" in cls or attr.get("data-testid") == "result-title-a"):
            self._href = attr.get("href", "")
            self._class = cls
            self._text = []
            self._in_result = True

    def handle_data(self, data: str) -> None:
        if self._in_result:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_result:
            url = _decode_duckduckgo_url(self._href)
            title = " ".join(part.strip() for part in self._text if part.strip())
            if url:
                self.links.append(_DiscoveredLink(url=url, title=title))
            self._href = ""
            self._text = []
            self._in_result = False


def _parse_duckduckgo_links(text: str) -> list[_DiscoveredLink]:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(text)
    return parser.links


def _decode_duckduckgo_url(href: str) -> str:
    if not href:
        return ""
    parsed = urlsplit(href)
    if parsed.path.startswith("/l/") or "duckduckgo.com" in (parsed.hostname or ""):
        query = parse_qs(parsed.query)
        if query.get("uddg"):
            return unquote(query["uddg"][0])
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return ""


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = unquote(text)
    return re.sub(r"\s+", " ", text).strip()


def filter_supported_public_urls(urls: Iterable[str]) -> list[RouteCandidate]:
    candidates: list[RouteCandidate] = []
    for url in urls:
        try:
            candidates.append(candidate_from_url(url, discovery_source="provided"))
        except PublicRouteError:
            continue
    return candidates
