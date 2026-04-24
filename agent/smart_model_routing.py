"""Compatibility shim for optional per-turn smart model routing.

Some local gateway carry still imports ``agent.smart_model_routing`` while the
upstream feature was removed.  Keep the gateway fail-open: when no optional
routing module/config is present, use the already-resolved primary runtime.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _primary_route(primary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model": primary.get("model"),
        "runtime": {
            "api_key": primary.get("api_key"),
            "base_url": primary.get("base_url"),
            "provider": primary.get("provider"),
            "api_mode": primary.get("api_mode"),
            "command": primary.get("command"),
            "args": list(primary.get("args") or []),
            "credential_pool": primary.get("credential_pool"),
        },
        "label": None,
        "signature": (
            primary.get("model"),
            primary.get("provider"),
            primary.get("base_url"),
            primary.get("api_mode"),
            primary.get("command"),
            tuple(primary.get("args") or ()),
        ),
    }


def resolve_turn_route(
    user_message: str,
    routing_config: Optional[Dict[str, Any]],
    primary: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the primary route when smart routing is unavailable/disabled.

    The local config currently has no ``smart_model_routing`` block, so this
    deliberately avoids inventing routing behavior.  It only preserves the API
    shape expected by ``gateway.run.GatewayRunner._resolve_turn_agent_config``.
    """
    return _primary_route(primary)
