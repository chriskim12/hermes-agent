"""Contract/policy/readiness tests for the insane-search Hermes adapter."""

from __future__ import annotations
import inspect


from pathlib import Path

from plugins.web.insane_search.adapter import map_fetch_result, resolve_public_url
from plugins.web.insane_search.contract import RetrievalResult
from plugins.web.insane_search.policy import evaluate_public_url
from plugins.web.insane_search.readiness import check_readiness
from plugins.web.insane_search.browser_adapter import browser_escalation_needed


def test_contract_mapping_serializes_required_result_fields() -> None:
    upstream = {
        "ok": True,
        "content": "hello public web",
        "final_url": "https://example.com/final",
        "verdict": "strong_ok",
        "profile_used": "unknown_challenge",
        "summary": "ok",
        "planned_attempts": 2,
        "executed_attempts": 1,
        "grid_exhausted": False,
        "trace": [
            {
                "phase": "grid",
                "executor": "curl_cffi",
                "url": "https://example.com/",
                "url_transform": "original",
                "impersonate": "safari",
                "referer": "self_root",
                "status": 200,
                "body_size": 16,
                "verdict": "strong_ok",
                "reasons": ["selector:h1"],
                "elapsed_s": 0.01,
            }
        ],
    }

    result = map_fetch_result("https://example.com/", upstream)
    payload = result.to_dict()

    assert isinstance(result, RetrievalResult)
    assert payload["success"] is True
    assert payload["url"] == "https://example.com/"
    assert payload["final_url"] == "https://example.com/final"
    assert payload["source_type"] == "insane_fallback"
    assert payload["route_used"] == "grid:curl_cffi:original"
    assert payload["verdict"] == "strong_ok"
    assert payload["confidence"] == 0.95
    assert payload["public_boundary"] == {
        "public_only": True,
        "private_url": False,
        "credentials_used": False,
        "auth_or_paywall": False,
        "stop_reason": "",
    }
    assert payload["trace"][0]["executor"] == "curl_cffi"
    assert payload["untried_routes"] == []
    assert payload["browser_escalation"] == {
        "needed": False,
        "reason": "",
        "allowed_actions": [],
        "candidate_public_urls": [],
    }
    assert payload["metadata"]["profile_used"] == "unknown_challenge"


def test_policy_stops_secret_and_private_urls_before_engine_call() -> None:
    calls: list[str] = []

    def fake_fetch(url: str, **kwargs: object) -> dict[str, object]:
        calls.append(url)
        return {
            "ok": True,
            "verdict": "strong_ok",
            "content": "ok",
            "final_url": url,
            "trace": [],
        }

    secret = resolve_public_url("https://example.com/?token=secret", engine_fetch=fake_fetch)
    userinfo = resolve_public_url("https://user:pass@example.com/", engine_fetch=fake_fetch)
    private = resolve_public_url("http://127.0.0.1/admin", engine_fetch=fake_fetch)

    assert secret.to_dict()["source_type"] == "policy_stop"
    assert secret.to_dict()["verdict"] == "secret_url_blocked"
    assert userinfo.to_dict()["verdict"] == "secret_url_blocked"
    assert private.to_dict()["verdict"] == "ssrf_blocked"
    assert private.to_dict()["public_boundary"]["private_url"] is True
    assert calls == []
    assert "allow_private_urls" not in inspect.signature(resolve_public_url).parameters


def test_policy_blocks_noncanonical_private_ipv4_forms_before_engine_call() -> None:
    calls: list[str] = []

    def fake_fetch(url: str, **kwargs: object) -> dict[str, object]:
        calls.append(url)
        return {"ok": True, "verdict": "strong_ok", "final_url": url, "trace": []}

    for url in (
        "http://2130706433/",
        "http://127.1/",
        "http://0177.0.0.1/",
        "http://0x7f.0.0.1/",
    ):
        result = resolve_public_url(url, engine_fetch=fake_fetch).to_dict()
        assert result["verdict"] == "ssrf_blocked", url
        assert result["public_boundary"]["private_url"] is True

    assert calls == []


def test_policy_marks_auth_paywall_paths_terminal() -> None:
    decision = evaluate_public_url("https://example.com/login?next=/article")

    assert decision.allowed is False
    assert decision.verdict.value == "auth_required"
    assert decision.to_boundary().auth_or_paywall is True

def test_final_url_policy_is_rechecked_after_engine_redirect() -> None:
    upstream = {
        "ok": True,
        "content": "internal metadata",
        "final_url": "http://169.254.169.254/latest/meta-data/",
        "verdict": "strong_ok",
        "trace": [{"phase": "grid", "executor": "curl_cffi", "verdict": "strong_ok"}],
    }

    result = map_fetch_result("https://example.com/redirect", upstream).to_dict()

    assert result["success"] is False
    assert result["source_type"] == "policy_stop"
    assert result["route_used"] == "policy:final_url"
    assert result["verdict"] == "ssrf_blocked"
    assert result["public_boundary"]["private_url"] is True
    assert result["metadata"]["policy_reason"] == "private_or_local_host"


def test_mapper_rechecks_original_url_policy_for_direct_callers() -> None:
    upstream = {
        "ok": True,
        "content": "should not surface",
        "final_url": "https://example.com/",
        "verdict": "strong_ok",
        "trace": [{"phase": "grid", "executor": "curl_cffi", "verdict": "strong_ok"}],
    }

    result = map_fetch_result("https://example.com/?token=secret", upstream).to_dict()

    assert result["success"] is False
    assert result["source_type"] == "policy_stop"
    assert result["route_used"] == "policy:input_url"
    assert result["verdict"] == "secret_url_blocked"
    assert result["content"] == ""

def test_browser_escalation_mapping_is_structured_not_executed() -> None:
    upstream = {
        "ok": False,
        "content": "",
        "final_url": "https://example.com/protected",
        "verdict": "challenge",
        "must_invoke_playwright_mcp": True,
        "untried_routes": ["playwright_mcp"],
        "trace": [{"phase": "grid", "executor": "curl_cffi", "verdict": "challenge"}],
    }

    result = map_fetch_result("https://example.com/protected", upstream).to_dict()

    assert result["success"] is False
    assert result["browser_escalation"] == {
        "needed": True,
        "reason": "upstream_mcp_required",
        "allowed_actions": ["browser_navigate", "browser_network_requests", "browser_snapshot"],
        "candidate_public_urls": ["https://example.com/protected"],
    }


def test_browser_adapter_normalizes_structured_escalation_only() -> None:
    result = map_fetch_result(
        "https://example.com/protected",
        {
            "ok": False,
            "final_url": "https://example.com/protected",
            "verdict": "challenge",
            "must_invoke_playwright_mcp": True,
            "trace": [],
        },
    )

    payload = browser_escalation_needed(result)

    assert payload == {
        "needed": True,
        "reason": "upstream_mcp_required",
        "allowed_actions": ["browser_navigate", "browser_network_requests", "browser_snapshot"],
        "candidate_public_urls": ["https://example.com/protected"],
    }


def test_terminal_policy_stops_do_not_escalate_to_browser() -> None:
    terminal_urls = [
        "https://example.com/?token=secret",
        "http://127.0.0.1/",
        "https://example.com/login",
    ]

    for url in terminal_urls:
        result = resolve_public_url(url, engine_fetch=lambda *_args, **_kwargs: None)
        assert result.to_dict()["source_type"] == "policy_stop"
        assert browser_escalation_needed(result) == {
            "needed": False,
            "reason": "",
            "allowed_actions": [],
            "candidate_public_urls": [],
        }


def test_browser_adapter_filters_raw_dict_actions_and_urls() -> None:
    payload = browser_escalation_needed(
        {
            "browser_escalation": {
                "needed": True,
                "reason": "raw",
                "allowed_actions": ["browser_navigate", "credential_login"],
                "candidate_public_urls": [
                    "https://example.com/public",
                    "http://127.0.0.1/private",
                ],
            }
        }
    )

    assert payload == {
        "needed": True,
        "reason": "raw",
        "allowed_actions": ["browser_navigate"],
        "candidate_public_urls": ["https://example.com/public"],
    }

def test_resolver_disables_local_playwright_fallback_by_default() -> None:
    captured: dict[str, object] = {}

    def fake_fetch(url: str, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": False, "verdict": "challenge", "final_url": url, "trace": []}

    resolve_public_url("https://example.com/challenge", engine_fetch=fake_fetch, enable_playwright=True)

    assert captured["enable_playwright"] is False

def test_readiness_is_profile_aware_and_non_mutating(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    import plugins.web.insane_search.readiness as readiness

    seen: list[str] = []

    def fake_find_spec(name: str) -> object | None:
        seen.append(name)
        return object() if name == "bs4" else None

    monkeypatch.setattr(readiness, "find_spec", fake_find_spec)
    report = check_readiness()
    payload = report.to_dict()

    assert payload["ready"] is True
    assert payload["missing"] == []
    assert payload["optional_missing"] == ["curl_cffi", "playwright", "yaml"]
    assert payload["learned_store"] == str(tmp_path / ".hermes" / "web" / "insane_search" / "learned_routes.json")
    assert payload["diagnostics"]["non_mutating"] is True
    assert payload["diagnostics"]["network"] is False
    assert payload["diagnostics"]["installer"] is False
    assert seen == ["curl_cffi", "bs4", "playwright", "yaml"]
