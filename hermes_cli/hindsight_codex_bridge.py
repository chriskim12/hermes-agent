"""OpenAI-compatible bridge for Hindsight backed by Hermes Codex OAuth.

This module gives Hindsight a narrow OpenAI-compatible loopback endpoint while
keeping ChatGPT/Codex OAuth tokens inside Hermes's existing credential resolver.
It intentionally starts with the smallest surface Hindsight needs:
non-streaming ``POST /v1/chat/completions``.

Run manually while testing:

    python -m hermes_cli.hindsight_codex_bridge --host 127.0.0.1 --port 8787

Then point Hindsight at:

    HINDSIGHT_API_LLM_PROVIDER=openai
    HINDSIGHT_API_LLM_BASE_URL=http://127.0.0.1:8787/v1
    HINDSIGHT_API_LLM_API_KEY=<local shared secret or dummy>

Live Hermes config/provider switching remains a separate approval gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

import httpx

from agent.auxiliary_client import _codex_cloudflare_headers
from agent.codex_responses_adapter import _chat_messages_to_responses_input, _responses_tools
from hermes_cli.auth import resolve_codex_runtime_credentials

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8787
_DEFAULT_TIMEOUT_SECONDS = 120.0
_ENV_MODEL_OVERRIDE = "HERMES_HINDSIGHT_CODEX_MODEL"
_ENV_LOCAL_SHARED_SECRET = "HERMES_HINDSIGHT_CODEX_BRIDGE_KEY"


class BridgeError(RuntimeError):
    """HTTP-shaped bridge failure without leaking token material."""

    def __init__(self, message: str, *, status_code: int = 500, code: str = "bridge_error") -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = code


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    return str(value)


def _extract_output_text(response_payload: Mapping[str, Any]) -> str:
    """Extract visible assistant text from an OpenAI Responses-style payload."""
    direct = response_payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct

    chunks: list[str] = []
    output = response_payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping):
                continue
            # Responses message item: {type: message, role: assistant, content:[...]}
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, Mapping):
                        continue
                    ptype = str(part.get("type") or "")
                    if ptype in {"output_text", "text"}:
                        text = part.get("text")
                        if isinstance(text, str):
                            chunks.append(text)
            # Some compatible servers flatten text onto the item.
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)

    if chunks:
        return "".join(chunks)

    # Defensive compatibility for chat-completion-shaped upstream mocks/proxies.
    choices = response_payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            msg = first.get("message")
            if isinstance(msg, Mapping):
                return _as_text(msg.get("content"))
            return _as_text(first.get("text"))

    return ""


def _build_responses_payload(chat_payload: Mapping[str, Any]) -> Dict[str, Any]:
    messages = chat_payload.get("messages")
    if not isinstance(messages, list):
        raise BridgeError("chat.completions payload must include a messages array", status_code=400, code="invalid_request")

    if chat_payload.get("stream"):
        raise BridgeError("stream=true is not supported by the Hindsight Codex bridge MVP", status_code=400, code="stream_not_supported")

    requested_model = str(chat_payload.get("model") or "").strip()
    model = os.getenv(_ENV_MODEL_OVERRIDE, "").strip() or requested_model
    if not model:
        raise BridgeError(
            f"model is required unless {_ENV_MODEL_OVERRIDE} is set",
            status_code=400,
            code="model_required",
        )

    system_chunks = [
        _as_text(msg.get("content"))
        for msg in messages
        if isinstance(msg, Mapping) and msg.get("role") == "system"
    ]
    non_system = [
        dict(msg)
        for msg in messages
        if isinstance(msg, Mapping) and msg.get("role") != "system"
    ]

    payload: Dict[str, Any] = {
        "model": model,
        "input": _chat_messages_to_responses_input(non_system),
        "store": False,
    }

    instructions = "\n\n".join(chunk for chunk in system_chunks if chunk).strip()
    payload["instructions"] = instructions or "You are a concise, helpful assistant."

    if "temperature" in chat_payload:
        payload["temperature"] = chat_payload["temperature"]
    if "top_p" in chat_payload:
        payload["top_p"] = chat_payload["top_p"]

    max_output_tokens = chat_payload.get("max_output_tokens")
    if max_output_tokens is None:
        max_output_tokens = chat_payload.get("max_completion_tokens", chat_payload.get("max_tokens"))
    if max_output_tokens is not None:
        payload["max_output_tokens"] = max_output_tokens

    tools = _responses_tools(chat_payload.get("tools"))
    if tools:
        payload["tools"] = tools
        tool_choice = chat_payload.get("tool_choice")
        if tool_choice is not None:
            payload["tool_choice"] = _responses_tool_choice(tool_choice)

    return payload


def _responses_tool_choice(tool_choice: Any) -> Any:
    """Convert Chat Completions tool_choice into Codex Responses shape."""
    if tool_choice in (None, "auto", "none", "required"):
        return tool_choice
    if isinstance(tool_choice, Mapping):
        if tool_choice.get("type") == "function":
            fn = tool_choice.get("function")
            name = fn.get("name") if isinstance(fn, Mapping) else tool_choice.get("name")
            if isinstance(name, str) and name.strip():
                return {"type": "function", "name": name.strip()}
    return tool_choice


def _extract_tool_calls(upstream_payload: Mapping[str, Any]) -> list[Dict[str, Any]]:
    tool_calls: list[Dict[str, Any]] = []
    output = upstream_payload.get("output")
    if not isinstance(output, list):
        return tool_calls
    for index, item in enumerate(output):
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type") or "")
        if item_type not in {"function_call", "custom_tool_call"}:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments = item.get("arguments") if item_type == "function_call" else item.get("input")
        if isinstance(arguments, Mapping):
            arguments = json.dumps(arguments, ensure_ascii=False)
        elif not isinstance(arguments, str):
            arguments = "{}" if arguments is None else str(arguments)
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_{uuid.uuid5(uuid.NAMESPACE_URL, f'{name}:{arguments}:{index}').hex[:12]}"
        call_id = call_id.strip()
        response_item_id = item.get("id")
        tool_id = call_id
        if isinstance(response_item_id, str) and response_item_id.strip():
            tool_id = f"{call_id}|{response_item_id.strip()}"
        tool_calls.append({
            "id": tool_id,
            "type": "function",
            "function": {"name": name.strip(), "arguments": arguments.strip() or "{}"},
        })
    return tool_calls


def _to_chat_completion_response(
    *,
    upstream_payload: Mapping[str, Any],
    requested_model: str,
) -> Dict[str, Any]:
    created = upstream_payload.get("created_at") or upstream_payload.get("created") or int(time.time())
    try:
        created_int = int(float(created))
    except Exception:
        created_int = int(time.time())

    upstream_id = upstream_payload.get("id")
    if not isinstance(upstream_id, str) or not upstream_id:
        upstream_id = f"chatcmpl-hindsight-codex-{uuid.uuid4().hex[:24]}"

    text = _extract_output_text(upstream_payload)
    tool_calls = _extract_tool_calls(upstream_payload)
    finish_reason = "tool_calls" if tool_calls else "stop"
    status = str(upstream_payload.get("status") or "").lower()
    if status and status not in {"completed", "succeeded", "success"}:
        finish_reason = status

    message: Dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}

    response: Dict[str, Any] = {
        "id": upstream_id,
        "object": "chat.completion",
        "created": created_int,
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }
    usage = upstream_payload.get("usage")
    if isinstance(usage, Mapping):
        response["usage"] = dict(usage)
    return response


def _embedding_vector(text: str, *, dimensions: int = 1536) -> list[float]:
    """Deterministic OpenAI-shaped embedding for local Hindsight smoke/tests.

    Codex OAuth does not expose a public embeddings endpoint. Hindsight still
    needs embeddings during startup/retain/recall, so the bridge provides a
    narrow deterministic vector endpoint for local smoke and chunk retrieval.
    This is not a semantic embedding model and must not be treated as quality
    proof for production recall.
    """
    seed = hashlib.sha256(text.encode("utf-8", errors="ignore")).digest()
    values: list[float] = []
    counter = 0
    while len(values) < dimensions:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
        for idx in range(0, len(block), 4):
            raw = int.from_bytes(block[idx : idx + 4], "big", signed=False)
            values.append((raw / 2147483647.5) - 1.0)
            if len(values) >= dimensions:
                break
    # Normalize so cosine distance remains well-behaved.
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]


def _to_embeddings_response(payload: Mapping[str, Any]) -> Dict[str, Any]:
    model = str(payload.get("model") or "text-embedding-3-small")
    raw_input = payload.get("input", "")
    inputs = raw_input if isinstance(raw_input, list) else [raw_input]
    data = []
    for idx, item in enumerate(inputs):
        text = _as_text(item)
        data.append({"object": "embedding", "index": idx, "embedding": _embedding_vector(text)})
    return {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


class HindsightCodexBridge:
    """Translate OpenAI chat completions into Codex Responses calls."""

    def __init__(
        self,
        *,
        credential_resolver: Callable[..., Mapping[str, Any]] = resolve_codex_runtime_credentials,
        http_client_factory: Callable[..., httpx.Client] = httpx.Client,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._http_client_factory = http_client_factory
        self._timeout_seconds = float(timeout_seconds)

    def chat_completions(self, chat_payload: Mapping[str, Any]) -> Dict[str, Any]:
        responses_payload = _build_responses_payload(chat_payload)
        upstream_payload = self._post_responses_with_refresh(responses_payload)
        return _to_chat_completion_response(
            upstream_payload=upstream_payload,
            requested_model=str(chat_payload.get("model") or responses_payload.get("model") or ""),
        )

    def _post_responses_with_refresh(self, responses_payload: Mapping[str, Any]) -> Dict[str, Any]:
        last_status: Optional[int] = None
        last_payload: Optional[Dict[str, Any]] = None
        for force_refresh in (False, True):
            try:
                creds = self._credential_resolver(force_refresh=force_refresh)
            except Exception as exc:
                raise BridgeError(
                    f"Codex OAuth credentials are unavailable: {exc.__class__.__name__}",
                    status_code=503,
                    code="codex_credentials_unavailable",
                ) from exc
            api_key = str(creds.get("api_key") or "").strip()
            base_url = str(creds.get("base_url") or "").strip().rstrip("/")
            if not api_key or not base_url:
                raise BridgeError("Codex OAuth credentials are unavailable", status_code=503, code="codex_credentials_unavailable")

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                **_codex_cloudflare_headers(api_key),
            }
            timeout = httpx.Timeout(max(5.0, self._timeout_seconds))
            with self._http_client_factory(timeout=timeout, headers=headers) as client:
                status_code, payload = self._stream_codex_response(
                    client,
                    f"{base_url}/responses",
                    self._codex_backend_payload(responses_payload),
                )

            last_status = status_code
            last_payload = payload
            if status_code not in (401, 403):
                break
            if force_refresh:
                break

        if last_status is None:
            raise BridgeError("Codex request was not attempted", status_code=500, code="codex_request_missing")
        if last_status >= 400:
            raise BridgeError(
                f"Codex backend returned HTTP {last_status}",
                status_code=502 if last_status >= 500 else last_status,
                code="codex_backend_error",
            )
        if not isinstance(last_payload, Mapping):
            raise BridgeError("Codex backend returned a non-object JSON payload", status_code=502, code="codex_invalid_json_shape")
        return dict(last_payload)

    @staticmethod
    def _codex_backend_payload(responses_payload: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(responses_payload)
        # chatgpt.com/backend-api/codex is Responses-shaped but not identical to
        # the public OpenAI Responses endpoint. It currently requires streaming
        # and rejects these public-API knobs, so the bridge accepts them from
        # OpenAI-compatible clients but strips them before hitting Codex.
        payload["stream"] = True
        for key in ("temperature", "top_p", "max_output_tokens"):
            payload.pop(key, None)
        return payload

    @staticmethod
    def _stream_codex_response(client: httpx.Client, url: str, payload: Mapping[str, Any]) -> tuple[int, Dict[str, Any]]:
        text_chunks: list[str] = []
        completed_payload: Optional[Dict[str, Any]] = None
        response_id: Optional[str] = None
        created_at: Optional[Any] = None
        model: Optional[str] = None
        usage: Optional[Mapping[str, Any]] = None
        output_items: list[Dict[str, Any]] = []
        output_items_by_id: dict[str, Dict[str, Any]] = {}

        with client.stream("POST", url, json=dict(payload)) as response:
            status_code = int(response.status_code)
            if status_code >= 400:
                return status_code, {"error": "codex_backend_error"}
            for raw_line in response.iter_lines():
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data = raw_line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except Exception:
                    continue
                if not isinstance(event, Mapping):
                    continue
                event_type = str(event.get("type") or "")
                if event_type in {"response.output_item.added", "response.output_item.done"}:
                    item = event.get("item")
                    if isinstance(item, Mapping):
                        item_dict = dict(item)
                        item_id = item_dict.get("id")
                        if isinstance(item_id, str) and item_id.strip():
                            # The final `.done` event carries completed function-call
                            # arguments; overwrite the earlier `.added` skeleton.
                            output_items_by_id[item_id] = item_dict
                        else:
                            output_items.append(item_dict)
                    continue
                if event_type == "response.function_call_arguments.done":
                    item_id = event.get("item_id")
                    arguments = event.get("arguments")
                    if isinstance(item_id, str) and item_id in output_items_by_id and isinstance(arguments, str):
                        output_items_by_id[item_id]["arguments"] = arguments
                        output_items_by_id[item_id]["status"] = "completed"
                    continue
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        text_chunks.append(delta)
                    continue
                if event_type == "response.output_text.done":
                    done_text = event.get("text")
                    if isinstance(done_text, str):
                        text_chunks = [done_text]
                    continue
                response_obj = event.get("response")
                if isinstance(response_obj, Mapping):
                    if isinstance(response_obj.get("id"), str):
                        response_id = response_obj.get("id")
                    created_at = response_obj.get("created_at", created_at)
                    if isinstance(response_obj.get("model"), str):
                        model = response_obj.get("model")
                    if isinstance(response_obj.get("usage"), Mapping):
                        usage = response_obj.get("usage")
                    if event_type == "response.completed":
                        completed_payload = dict(response_obj)

        if completed_payload is None:
            completed_payload = {
                "id": response_id or f"resp_hindsight_codex_{uuid.uuid4().hex[:24]}",
                "created_at": created_at or int(time.time()),
                "status": "completed",
                "model": model or str(payload.get("model") or ""),
            }
        collected_output = output_items + list(output_items_by_id.values())
        if collected_output and not completed_payload.get("output"):
            completed_payload["output"] = collected_output
        if text_chunks or "output_text" not in completed_payload:
            completed_payload["output_text"] = "".join(text_chunks)
        if usage and "usage" not in completed_payload:
            completed_payload["usage"] = dict(usage)
        return 200, completed_payload


def _openai_error(message: str, *, status_code: int, code: str) -> Dict[str, Any]:
    return {"error": {"message": message, "type": "hindsight_codex_bridge_error", "code": code, "status_code": status_code}}


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "HermesHindsightCodexBridge/0.1"

    @property
    def bridge(self) -> HindsightCodexBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    @property
    def shared_secret(self) -> str:
        return self.server.shared_secret  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") in {"/health", "/v1/health"}:
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, _openai_error("not found", status_code=404, code="not_found"))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = self.path.rstrip("/")
        if path not in {"/v1/chat/completions", "/v1/embeddings"}:
            self._send_json(404, _openai_error("not found", status_code=404, code="not_found"))
            return
        if self.shared_secret:
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {self.shared_secret}"
            if auth != expected:
                self._send_json(401, _openai_error("invalid local bridge token", status_code=401, code="unauthorized"))
                return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if not isinstance(payload, Mapping):
                raise BridgeError("request body must be a JSON object", status_code=400, code="invalid_json")
            if path == "/v1/embeddings":
                response = _to_embeddings_response(payload)
            else:
                response = self.bridge.chat_completions(payload)
            self._send_json(200, response)
        except BridgeError as exc:
            self._send_json(exc.status_code, _openai_error(str(exc), status_code=exc.status_code, code=exc.code))
        except json.JSONDecodeError:
            self._send_json(400, _openai_error("invalid JSON", status_code=400, code="invalid_json"))
        except Exception as exc:  # defensive: never dump tokens/headers
            self._send_json(500, _openai_error(f"bridge failure: {exc.__class__.__name__}", status_code=500, code="bridge_failure"))

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.getenv("HERMES_HINDSIGHT_CODEX_BRIDGE_LOG", "").lower() in {"1", "true", "yes"}:
            super().log_message(fmt, *args)

    def _send_json(self, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HindsightCodexBridgeServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], bridge: HindsightCodexBridge, shared_secret: str = "") -> None:
        super().__init__(server_address, _BridgeHandler)
        self.bridge = bridge
        self.shared_secret = shared_secret


def serve(*, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT, shared_secret: str = "") -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing to bind Hindsight Codex bridge to a non-loopback host")
    bridge = HindsightCodexBridge()
    server = HindsightCodexBridgeServer((host, int(port)), bridge, shared_secret=shared_secret)
    print(f"Hindsight Codex bridge listening on http://{host}:{port}/v1", flush=True)
    server.serve_forever()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Hindsight Codex OAuth OpenAI-compatible bridge")
    parser.add_argument("--host", default=os.getenv("HERMES_HINDSIGHT_CODEX_BRIDGE_HOST", _DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_HINDSIGHT_CODEX_BRIDGE_PORT", str(_DEFAULT_PORT))))
    parser.add_argument("--shared-secret", default=os.getenv(_ENV_LOCAL_SHARED_SECRET, ""))
    args = parser.parse_args(list(argv) if argv is not None else None)
    serve(host=args.host, port=args.port, shared_secret=args.shared_secret)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
