"""web_extract public-route resolver integration tests."""

from __future__ import annotations

import json

import pytest

from agent.web_search_provider import WebSearchProvider
from agent.web_search_registry import _reset_for_tests, register_provider
from plugins.web.insane_search.contract import PublicBoundary, RetrievalResult, RetrievalVerdict, SourceType


class FakeExtractProvider(WebSearchProvider):
    def __init__(self, results: list[dict[str, object]]) -> None:
        self._results = results
        self.calls: list[list[str]] = []

    @property
    def name(self) -> str:
        return "fake-extract"

    @property
    def display_name(self) -> str:
        return "Fake Extract"

    def supports_extract(self) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def extract(self, urls: list[str], **kwargs: object) -> list[dict[str, object]]:
        self.calls.append(urls)
        return self._results


def _enabled_config(**overrides: object) -> dict[str, object]:
    resolver = {
        "enabled": True,
        "fallback_after_primary": True,
        "provider_failure_triggers": ["empty_content", "blocked", "challenge", "suspect_ok", "inaccessible"],
        "timeout_s": 20,
        "phase0_before_primary": False,
        "allow_learning": True,
        "include_diagnostics": False,
    }
    resolver.update(overrides)
    return {"extract_backend": "fake-extract", "public_route_resolver": resolver}


@pytest.fixture(autouse=True)
def _fake_provider_registry(monkeypatch):
    import tools.web_tools as web_tools

    _reset_for_tests()
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "check_auxiliary_model", lambda: False)
    yield
    _reset_for_tests()


async def _extract(monkeypatch, provider: FakeExtractProvider, config: dict[str, object]) -> dict[str, object]:
    import tools.web_tools as web_tools

    register_provider(provider)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: config)
    payload = await web_tools.web_extract_tool(["https://example.com/article"], use_llm_processing=False)
    return json.loads(payload)


@pytest.mark.asyncio
async def test_resolver_disabled_preserves_primary_result(monkeypatch) -> None:
    import plugins.web.insane_search.adapter as adapter

    provider = FakeExtractProvider([{"url": "https://example.com/article", "content": ""}])
    monkeypatch.setattr(adapter, "resolve_public_url", lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolver called")))

    payload = await _extract(monkeypatch, provider, {"extract_backend": "fake-extract", "public_route_resolver": {"enabled": False}})

    assert payload["results"][0]["url"] == "https://example.com/article"
    assert payload["results"][0]["content"] == ""
    assert provider.calls == [["https://example.com/article"]]


@pytest.mark.asyncio
async def test_primary_success_keeps_resolver_idle(monkeypatch) -> None:
    import plugins.web.insane_search.adapter as adapter

    provider = FakeExtractProvider([{"url": "https://example.com/article", "content": "primary"}])
    calls: list[str] = []
    monkeypatch.setattr(adapter, "resolve_public_url", lambda url, **kwargs: calls.append(url))

    payload = await _extract(monkeypatch, provider, _enabled_config())

    assert payload["results"][0]["content"] == "primary"
    assert calls == []


@pytest.mark.asyncio
async def test_empty_primary_content_uses_public_resolver(monkeypatch) -> None:
    import plugins.web.insane_search.adapter as adapter

    provider = FakeExtractProvider([{"url": "https://example.com/article", "content": ""}])

    def fake_resolver(url: str, **kwargs: object) -> RetrievalResult:
        return RetrievalResult(
            success=True,
            url=url,
            final_url=url,
            content="resolved content",
            raw_content="resolved content",
            source_type=SourceType.INSANE_FALLBACK,
            route_used="grid:curl_cffi:original",
            verdict=RetrievalVerdict.STRONG_OK,
            confidence=0.95,
        )

    monkeypatch.setattr(adapter, "resolve_public_url", fake_resolver)
    payload = await _extract(monkeypatch, provider, _enabled_config())

    assert payload["results"][0]["content"] == "resolved content"
    assert payload["results"][0]["title"] == ""


@pytest.mark.asyncio
async def test_resolver_unavailable_preserves_primary_result(monkeypatch) -> None:
    import plugins.web.insane_search.adapter as adapter

    provider = FakeExtractProvider([{"url": "https://example.com/article", "content": "", "error": "blocked"}])
    monkeypatch.setattr(adapter, "resolve_public_url", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("missing deps")))

    payload = await _extract(monkeypatch, provider, _enabled_config())

    assert payload["results"][0]["error"] == "blocked"
    assert "public_route_resolver" not in payload["results"][0]


@pytest.mark.asyncio
async def test_policy_stop_from_resolver_is_terminal(monkeypatch) -> None:
    import plugins.web.insane_search.adapter as adapter

    provider = FakeExtractProvider([{"url": "https://example.com/login", "content": "", "error": "blocked"}])

    def fake_resolver(url: str, **kwargs: object) -> RetrievalResult:
        return RetrievalResult(
            success=False,
            url=url,
            final_url=url,
            source_type=SourceType.POLICY_STOP,
            route_used="policy",
            verdict=RetrievalVerdict.AUTH_REQUIRED,
            confidence=1.0,
            public_boundary=PublicBoundary(auth_or_paywall=True, stop_reason="auth_or_paywall_path"),
        )

    monkeypatch.setattr(adapter, "resolve_public_url", fake_resolver)
    payload = await _extract(monkeypatch, provider, _enabled_config())

    result = payload["results"][0]
    assert result["content"] == ""
    assert result["error"] == "auth_or_paywall_path"


def test_default_config_preserves_existing_web_backend_keys() -> None:
    from hermes_cli.config import DEFAULT_CONFIG

    web = DEFAULT_CONFIG["web"]
    assert web["backend"] == ""
    assert web["search_backend"] == ""
    assert web["extract_backend"] == ""
    assert web["public_route_resolver"]["enabled"] is False
