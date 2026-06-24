"""Vendor import checks for the staged insane-search integration."""

from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "plugins" / "web" / "insane_search"
VENDOR_DIR = PLUGIN_DIR / "vendor"
ENGINE_DIR = VENDOR_DIR / "insane_search_engine"


def test_vendor_import_preserves_upstream_engine_files() -> None:
    assert (ENGINE_DIR / "fetch_chain.py").is_file()
    assert (ENGINE_DIR / "executor.py").is_file()
    assert (ENGINE_DIR / "phase0.py").is_file()
    assert (ENGINE_DIR / "tests" / "test_smoke.py").is_file()
    assert (ENGINE_DIR / "templates" / "playwright_real_chrome.js").is_file()


def test_vendor_license_and_sync_ledger_are_present() -> None:
    license_text = (VENDOR_DIR / "LICENSE").read_text(encoding="utf-8")
    upstream_text = (VENDOR_DIR / "UPSTREAM.md").read_text(encoding="utf-8")

    assert "MIT License" in license_text
    assert "49306346b59aa89b5e96d98e1104da0890deed72" in upstream_text
    assert "skills/insane-search/engine/" in upstream_text
    assert "plugins/web/insane_search/vendor/insane_search_engine/" in upstream_text


def test_vendor_slice_excludes_claude_wrapper_and_setup_hooks() -> None:
    assert not (PLUGIN_DIR / ".claude-plugin").exists()
    assert not (PLUGIN_DIR / "setup").exists()
    assert not (PLUGIN_DIR / "skills").exists()

    upstream_text = (VENDOR_DIR / "UPSTREAM.md").read_text(encoding="utf-8")
    assert ".claude-plugin/" in upstream_text
    assert "setup/" in upstream_text
    assert "auto-install/setup behavior remains excluded" in upstream_text


def test_inert_plugin_registers_no_live_provider() -> None:
    module = importlib.import_module("plugins.web.insane_search")

    class RecordingContext:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def register_web_search_provider(self, provider: object) -> None:
            self.calls.append("register_web_search_provider")

        def register_tool(self, *args: object, **kwargs: object) -> None:
            self.calls.append("register_tool")

    ctx = RecordingContext()
    module.register(ctx)
    assert ctx.calls == []
