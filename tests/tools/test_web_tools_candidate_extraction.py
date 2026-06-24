"""Explicit search-candidate extraction helper tests."""

from __future__ import annotations

import json

import pytest

from agent.web_search_provider import WebSearchProvider
from agent.web_search_registry import _reset_for_tests, register_provider


class FinalUrlProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "final-url-provider"

    @property
    def display_name(self) -> str:
        return "Final URL Provider"

    def supports_extract(self) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def extract(self, urls: list[str], **kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "url": urls[0],
                "final_url": "https://canonical.example/article",
                "content": "canonical content",
            }
        ]


@pytest.mark.asyncio
async def test_extract_search_candidates_dedupes_by_final_url_and_confidence(monkeypatch) -> None:
    import tools.web_tools as web_tools

    calls: list[list[str]] = []

    async def fake_extract(urls, **kwargs):
        calls.append(list(urls))
        return json.dumps(
            {
                "results": [
                    {"url": "https://a.example", "final_url": "https://final.example", "content": "low"},
                    {"url": "https://b.example", "final_url": "https://final.example", "content": "high"},
                ]
            }
        )

    monkeypatch.setattr(web_tools, "web_extract_tool", fake_extract)

    payload = await web_tools.extract_search_candidates(
        [
            {"url": "https://a.example", "confidence": 0.2},
            {"url": "https://b.example", "confidence": 0.9},
        ]
    )

    assert calls == [["https://a.example", "https://b.example"]]
    assert payload == {
        "success": True,
        "results": [
            {
                "url": "https://b.example",
                "final_url": "https://final.example",
                "content": "high",
                "source_confidence": 0.9,
            }
        ],
    }


@pytest.mark.asyncio
async def test_extract_search_candidates_keeps_confidence_when_result_url_is_final(monkeypatch) -> None:
    import tools.web_tools as web_tools

    async def fake_extract(urls, **kwargs):
        return json.dumps({"results": [{"url": "https://final.example", "content": "resolved"}]})

    monkeypatch.setattr(web_tools, "web_extract_tool", fake_extract)

    payload = await web_tools.extract_search_candidates(
        [{"url": "https://source.example", "confidence": 0.7}]
    )

    assert payload["results"][0]["url"] == "https://final.example"
    assert payload["results"][0]["source_confidence"] == 0.7


@pytest.mark.asyncio
async def test_extract_search_candidates_requires_explicit_candidates(monkeypatch) -> None:
    import tools.web_tools as web_tools

    async def fake_extract(urls, **kwargs):
        raise AssertionError("web_extract_tool should not be called")

    monkeypatch.setattr(web_tools, "web_extract_tool", fake_extract)

    assert await web_tools.extract_search_candidates([]) == {"success": True, "results": []}
    assert await web_tools.extract_search_candidates([{"title": "missing URL"}]) == {
        "success": True,
        "results": [],
    }


@pytest.mark.asyncio
async def test_extract_search_candidates_dedupes_input_urls_before_extract(monkeypatch) -> None:
    import tools.web_tools as web_tools

    calls: list[list[str]] = []

    async def fake_extract(urls, **kwargs):
        calls.append(list(urls))
        return json.dumps({"results": [{"url": "https://a.example", "content": "best"}]})

    monkeypatch.setattr(web_tools, "web_extract_tool", fake_extract)

    payload = await web_tools.extract_search_candidates(
        [
            {"url": "https://a.example", "confidence": 0.1},
            {"url": "https://a.example", "confidence": 0.8},
        ]
    )
    assert calls == [["https://a.example"]]
    assert payload["results"][0]["source_confidence"] == 0.8


@pytest.mark.asyncio
async def test_web_extract_preserves_final_url_for_candidate_dedupe(monkeypatch) -> None:
    import tools.web_tools as web_tools

    _reset_for_tests()
    register_provider(FinalUrlProvider())
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"extract_backend": "final-url-provider"})
    monkeypatch.setattr(web_tools, "check_auxiliary_model", lambda: False)
    try:
        payload = json.loads(
            await web_tools.web_extract_tool(
                ["https://example.com/article"],
                use_llm_processing=False,
            )
        )
    finally:
        _reset_for_tests()

    assert payload["results"][0]["final_url"] == "https://canonical.example/article"


def test_web_search_tool_contract_remains_metadata_only() -> None:
    import tools.web_tools as web_tools

    assert web_tools.extract_search_candidates.__name__ == "extract_search_candidates"
    assert web_tools.web_search_tool.__name__ == "web_search_tool"
