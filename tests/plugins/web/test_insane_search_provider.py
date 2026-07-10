"""Tests for the safe insane web_search provider."""

from __future__ import annotations

import json
from typing import Any

from plugins.web.insane_search.provider import InsaneWebSearchProvider
from plugins.web.insane_search.routes import FetchResponse, RouteCandidate


def _fetcher(routes: dict[str, tuple[int, str] | str]):
    calls: list[str] = []

    def fetch(url: str) -> FetchResponse:
        calls.append(url)
        value = routes.get(url)
        if value is None:
            return FetchResponse(url=url, status_code=404, text="missing")
        if isinstance(value, tuple):
            status, text = value
        else:
            status, text = 200, value
        return FetchResponse(url=url, status_code=status, text=text)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_direct_supported_routes_enrich_reddit_x_youtube_and_hackernews() -> None:
    reddit_fetch = _fetcher({
        "https://www.reddit.com/r/hermes/comments/abc123/title/.rss": """
        <rss><channel><item><title>Reddit title</title><link>https://www.reddit.com/r/hermes/comments/abc123/title/</link><description>Reddit body</description></item></channel></rss>
        """,
    })
    x_fetch = _fetcher({
        "https://publish.twitter.com/oembed?url=https%3A%2F%2Fx.com%2FNousResearch%2Fstatus%2F12345&omit_script=1": json.dumps({
            "author_name": "Nous Research",
            "html": "<blockquote>safe public tweet</blockquote>",
            "url": "https://x.com/NousResearch/status/12345",
        }),
    })
    youtube_fetch = _fetcher({
        "https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2Fabc123&format=json": json.dumps({
            "title": "Video title",
            "author_name": "Hermes",
        }),
    })
    hn_fetch = _fetcher({
        "https://hacker-news.firebaseio.com/v0/item/4242.json": json.dumps({
            "title": "HN title",
            "url": "https://example.com/story",
            "by": "pg",
            "score": 42,
            "descendants": 7,
        }),
    })

    cases = [
        (InsaneWebSearchProvider(fetcher=reddit_fetch), "https://www.reddit.com/r/hermes/comments/abc123/title/", "reddit_rss", "Reddit title"),
        (InsaneWebSearchProvider(fetcher=x_fetch), "https://x.com/NousResearch/status/12345", "x_oembed", "X post by Nous Research"),
        (InsaneWebSearchProvider(fetcher=youtube_fetch), "https://youtu.be/abc123", "youtube_oembed", "Video title"),
        (InsaneWebSearchProvider(fetcher=hn_fetch), "https://news.ycombinator.com/item?id=4242", "hackernews_item_api", "HN title"),
    ]

    for provider, query, route_used, title in cases:
        result = provider.search(query, limit=10)
        assert result["success"] is True
        item = result["data"]["web"][0]
        assert item["route_used"] == route_used
        assert item["title"] == title
        assert item["insane_search"]["public_only"] is True


def test_unsafe_and_unsupported_direct_urls_fail_closed_without_fetching() -> None:
    fetch = _fetcher({})
    provider = InsaneWebSearchProvider(fetcher=fetch)

    for query in (
        "http://127.0.0.1/admin",
        "https://example.com/?token=secret",
        "https://example.com/login",
        "https://user:pass@x.com/NousResearch/status/123",
        "https://example.com/public",
    ):
        result = provider.search(query, limit=5)
        assert result["success"] is False
        assert result["data"]["web"] == []

    assert fetch.calls == []  # type: ignore[attr-defined]


def test_query_discovery_enriches_dedupes_and_caps_results() -> None:
    urls = [
        "https://youtu.be/dup123",
        "https://youtu.be/dup123?utm_source=x",
        "https://news.ycombinator.com/item?id=1",
        "https://example.com/ignored",
    ]
    for i in range(30):
        urls.append(f"https://news.ycombinator.com/item?id={i + 100}")

    def discovery(_query: str, limit: int) -> list[RouteCandidate]:
        assert limit == 20
        candidates: list[RouteCandidate] = []
        for url in urls:
            platform = "youtube" if "youtu" in url else "hackernews" if "ycombinator" in url else "unsupported"
            candidates.append(RouteCandidate(url=url, platform=platform, discovery_source="test"))
        return candidates

    routes: dict[str, tuple[int, str] | str] = {
        "https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2Fdup123&format=json": json.dumps({"title": "Deduped video"}),
        "https://hacker-news.firebaseio.com/v0/item/1.json": json.dumps({"title": "HN 1", "url": "http://127.0.0.1/internal"}),
    }
    for i in range(30):
        item_id = i + 100
        routes[f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"] = json.dumps({"title": f"HN {item_id}"})
    fetch = _fetcher(routes)
    provider = InsaneWebSearchProvider(fetcher=fetch, discovery=discovery)

    result = provider.search("public route query", limit=99)

    assert result["success"] is True
    items = result["data"]["web"]
    assert len(items) == 20
    assert items[0]["title"] == "Deduped video"
    assert [item["position"] for item in items] == list(range(1, 21))
    assert len({item["url"] for item in items}) == 20
    assert any(row["reason"] == "unsupported_public_route" for row in result["data"]["rejected"])
    assert items[1]["url"] == "https://news.ycombinator.com/item?id=1"


def test_route_body_challenge_markers_fail_closed() -> None:
    fetch = _fetcher({
        "https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2Fabc123&format=json": "captcha required",
    })
    provider = InsaneWebSearchProvider(fetcher=fetch)

    result = provider.search("https://youtu.be/abc123", limit=5)

    assert result["success"] is False
    assert result["data"]["web"] == []
    assert result["data"]["rejected"][0]["reason"].startswith("youtube_oembed_blocked_marker")


def test_default_fetch_validates_redirect_location_before_following(monkeypatch) -> None:
    from plugins.web.insane_search import routes

    calls: list[str] = []

    class Response:
        def __init__(self, url: str, status_code: int, location: str = "") -> None:
            self.url = url
            self.status_code = status_code
            self.headers = {"location": location} if location else {}
            self.text = ""

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert kwargs["follow_redirects"] is False

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get(self, url: str) -> Response:
            calls.append(url)
            return Response(url, 302, "http://127.0.0.1/internal")

    monkeypatch.setattr(routes.httpx, "Client", Client)

    result = InsaneWebSearchProvider().search("https://youtu.be/abc123", limit=5)

    assert result["success"] is False
    assert calls == ["https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2Fabc123&format=json"]


def test_insane_provider_is_explicitly_selectable_but_not_auto_selected(monkeypatch) -> None:
    from agent.web_search_registry import get_active_search_provider, register_provider, _reset_for_tests
    from tools import web_tools

    _reset_for_tests()
    fetch = _fetcher({
        "https://www.youtube.com/oembed?url=https%3A%2F%2Fyoutu.be%2Fselected&format=json": json.dumps({"title": "Selected provider"})
    })
    register_provider(InsaneWebSearchProvider(fetcher=fetch))
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"search_backend": "insane"})
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    assert web_tools._get_search_backend() == "insane"
    tool_result = json.loads(web_tools.web_search_tool("https://youtu.be/selected", limit=1))
    assert tool_result["success"] is True
    assert tool_result["data"]["web"][0]["title"] == "Selected provider"
    assert web_tools.check_web_search_available() is True
    assert web_tools.check_web_extract_available() is False
    assert web_tools.check_web_api_key() is True

    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "insane"})
    assert web_tools.check_web_search_available() is True
    assert web_tools.check_web_extract_available() is False
    assert web_tools.check_web_api_key() is True

    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {})
    assert get_active_search_provider() is None
    assert web_tools._get_backend() == "firecrawl"
    _reset_for_tests()
