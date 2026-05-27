from __future__ import annotations

import json
import os
import threading
import urllib.request
from typing import Any

import httpx
import pytest

from hermes_cli.hindsight_codex_bridge import (
    BridgeError,
    HindsightCodexBridge,
    HindsightCodexBridgeServer,
    _build_responses_payload,
    _to_embeddings_response,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeStreamResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def __enter__(self) -> "FakeStreamResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def iter_lines(self) -> list[str]:
        if self.status_code >= 400:
            return []
        if "__events__" in self._payload:
            return [f"data: {json.dumps(event)}" for event in self._payload["__events__"]]
        body = json.dumps({"type": "response.completed", "response": self._payload})
        return [f"data: {body}"]


class FakeClient:
    calls: list[dict[str, Any]] = []
    responses: list[FakeResponse] = []

    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def stream(self, method: str, url: str, *, json: dict[str, Any]) -> FakeStreamResponse:
        self.calls.append({"method": method, "url": url, "json": json, "headers": self.kwargs.get("headers", {})})
        response = self.responses.pop(0)
        return FakeStreamResponse(response.status_code, response._payload)


def test_build_responses_payload_converts_chat_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_HINDSIGHT_CODEX_MODEL", raising=False)

    payload = _build_responses_payload(
        {
            "model": "gpt-5.3-codex-spark",
            "messages": [
                {"role": "system", "content": "You are concise."},
                {"role": "user", "content": "remember this"},
            ],
            "temperature": 0,
            "max_tokens": 64,
        }
    )

    assert payload["model"] == "gpt-5.3-codex-spark"
    assert payload["instructions"] == "You are concise."
    assert payload["store"] is False
    assert payload["input"] == [{"role": "user", "content": "remember this"}]
    assert payload["temperature"] == 0
    assert payload["max_output_tokens"] == 64


def test_build_responses_payload_converts_tool_choice_for_responses_api() -> None:
    payload = _build_responses_payload(
        {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "search memory"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "recall_memory",
                        "description": "Search memories",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "recall_memory"}},
        }
    )

    assert payload["tools"][0]["name"] == "recall_memory"
    assert payload["tool_choice"] == {"type": "function", "name": "recall_memory"}


def test_build_responses_payload_adds_default_instructions_for_codex() -> None:
    payload = _build_responses_payload({"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": "ping"}]})

    assert payload["instructions"] == "You are a concise, helpful assistant."


def test_model_override_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HINDSIGHT_CODEX_MODEL", "gpt-5.5")
    payload = _build_responses_payload({"model": "ignored-by-bridge", "messages": [{"role": "user", "content": "x"}]})
    assert payload["model"] == "gpt-5.5"


def test_streaming_rejected_for_mvp() -> None:
    with pytest.raises(BridgeError) as exc:
        _build_responses_payload({"model": "gpt-5.5", "stream": True, "messages": []})
    assert exc.value.status_code == 400
    assert exc.value.code == "stream_not_supported"


def test_bridge_posts_to_codex_responses_and_returns_chat_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.calls = []
    FakeClient.responses = [
        FakeResponse(
            200,
            {
                "id": "resp_123",
                "created_at": 1779000000,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "stored"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    ]

    resolver_calls: list[bool] = []

    def resolver(*, force_refresh: bool = False) -> dict[str, str]:
        resolver_calls.append(force_refresh)
        return {"api_key": "header.payload.sig", "base_url": "https://chatgpt.com/backend-api/codex"}

    bridge = HindsightCodexBridge(credential_resolver=resolver, http_client_factory=FakeClient)
    result = bridge.chat_completions(
        {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]}
    )

    assert resolver_calls == [False]
    assert FakeClient.calls[0]["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert FakeClient.calls[0]["method"] == "POST"
    assert FakeClient.calls[0]["headers"]["Authorization"] == "Bearer header.payload.sig"
    assert FakeClient.calls[0]["headers"]["originator"] == "codex_cli_rs"
    assert FakeClient.calls[0]["headers"]["Accept"] == "text/event-stream"
    assert FakeClient.calls[0]["json"]["input"] == [{"role": "user", "content": "hello"}]
    assert FakeClient.calls[0]["json"]["stream"] is True
    assert "temperature" not in FakeClient.calls[0]["json"]
    assert "max_output_tokens" not in FakeClient.calls[0]["json"]
    assert result["object"] == "chat.completion"
    assert result["id"] == "resp_123"
    assert result["choices"][0]["message"] == {"role": "assistant", "content": "stored"}
    assert result["usage"] == {"input_tokens": 1, "output_tokens": 1}


def test_bridge_returns_openai_tool_calls_from_codex_function_calls() -> None:
    FakeClient.calls = []
    FakeClient.responses = [
        FakeResponse(
            200,
            {
                "id": "resp_tool",
                "created_at": 1779000001,
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_123",
                        "call_id": "call_123",
                        "name": "recall_memory",
                        "arguments": '{"query":"hindsight"}',
                    }
                ],
            },
        )
    ]

    def resolver(*, force_refresh: bool = False) -> dict[str, str]:
        return {"api_key": "header.payload.sig", "base_url": "https://chatgpt.com/backend-api/codex"}

    bridge = HindsightCodexBridge(credential_resolver=resolver, http_client_factory=FakeClient)
    result = bridge.chat_completions(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "use tools"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "recall_memory",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }
            ],
        }
    )

    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_123|fc_123",
                "type": "function",
                "function": {"name": "recall_memory", "arguments": '{"query":"hindsight"}'},
            }
        ],
    }


def test_bridge_collects_streamed_tool_call_items_when_completed_output_is_empty() -> None:
    FakeClient.calls = []
    FakeClient.responses = [
        FakeResponse(
            200,
            {
                "__events__": [
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "id": "fc_streamed",
                            "type": "function_call",
                            "status": "in_progress",
                            "arguments": "",
                            "call_id": "call_streamed",
                            "name": "recall",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "fc_streamed",
                        "arguments": '{"query":"hindsight","max_tokens":1000}',
                    },
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "id": "fc_streamed",
                            "type": "function_call",
                            "status": "completed",
                            "arguments": '{"query":"hindsight","max_tokens":1000}',
                            "call_id": "call_streamed",
                            "name": "recall",
                        },
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_streamed_tool",
                            "created_at": 1779000002,
                            "status": "completed",
                            "output": [],
                        },
                    },
                ]
            },
        )
    ]

    def resolver(*, force_refresh: bool = False) -> dict[str, str]:
        return {"api_key": "header.payload.sig", "base_url": "https://chatgpt.com/backend-api/codex"}

    bridge = HindsightCodexBridge(credential_resolver=resolver, http_client_factory=FakeClient)
    result = bridge.chat_completions(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "use tools"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "recall",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "recall"}},
        }
    )

    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"] == [
        {
            "id": "call_streamed|fc_streamed",
            "type": "function",
            "function": {"name": "recall", "arguments": '{"query":"hindsight","max_tokens":1000}'},
        }
    ]


def test_bridge_refreshes_once_on_401() -> None:
    FakeClient.calls = []
    FakeClient.responses = [
        FakeResponse(401, {"error": "expired"}),
        FakeResponse(200, {"id": "resp_ok", "output_text": "ok"}),
    ]
    resolver_calls: list[bool] = []

    def resolver(*, force_refresh: bool = False) -> dict[str, str]:
        resolver_calls.append(force_refresh)
        return {"api_key": f"token-{force_refresh}", "base_url": "https://chatgpt.com/backend-api/codex"}

    bridge = HindsightCodexBridge(credential_resolver=resolver, http_client_factory=FakeClient)
    result = bridge.chat_completions({"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]})

    assert resolver_calls == [False, True]
    assert len(FakeClient.calls) == 2
    assert result["choices"][0]["message"]["content"] == "ok"


def test_bridge_reports_credential_resolver_failure_without_token_dump() -> None:
    def resolver(*, force_refresh: bool = False) -> dict[str, str]:
        raise RuntimeError("refresh_token=SECRET access_token=SECRET")

    bridge = HindsightCodexBridge(credential_resolver=resolver, http_client_factory=FakeClient)
    with pytest.raises(BridgeError) as exc:
        bridge.chat_completions({"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]})

    assert exc.value.status_code == 503
    assert exc.value.code == "codex_credentials_unavailable"
    assert "SECRET" not in str(exc.value)
    assert "RuntimeError" in str(exc.value)


def test_embeddings_response_is_openai_shaped_and_deterministic() -> None:
    first = _to_embeddings_response({"model": "text-embedding-3-small", "input": ["alpha", "alpha"]})
    second = _to_embeddings_response({"model": "text-embedding-3-small", "input": "alpha"})

    assert first["object"] == "list"
    assert first["data"][0]["object"] == "embedding"
    assert len(first["data"][0]["embedding"]) == 1536
    assert first["data"][0]["embedding"] == first["data"][1]["embedding"]
    assert first["data"][0]["embedding"] == second["data"][0]["embedding"]


def test_http_handler_health_and_chat_completion() -> None:
    FakeClient.calls = []
    FakeClient.responses = [FakeResponse(200, {"id": "resp_http", "output_text": "pong"})]

    def resolver(*, force_refresh: bool = False) -> dict[str, str]:
        return {"api_key": "header.payload.sig", "base_url": "https://chatgpt.com/backend-api/codex"}

    bridge = HindsightCodexBridge(credential_resolver=resolver, http_client_factory=FakeClient)
    server = HindsightCodexBridgeServer(("127.0.0.1", 0), bridge, shared_secret="local-secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/health", timeout=5) as resp:
            assert json.loads(resp.read().decode("utf-8")) == {"status": "ok"}

        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps({"model": "gpt-5.5", "messages": [{"role": "user", "content": "ping"}]}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer local-secret"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["choices"][0]["message"]["content"] == "pong"

        req = urllib.request.Request(
            f"{base}/v1/embeddings",
            data=json.dumps({"model": "text-embedding-3-small", "input": "ping"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer local-secret"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            emb_body = json.loads(resp.read().decode("utf-8"))
        assert emb_body["object"] == "list"
        assert len(emb_body["data"][0]["embedding"]) == 1536
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
