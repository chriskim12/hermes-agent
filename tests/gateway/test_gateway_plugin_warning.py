import logging
import sys
import types

from gateway import run as gateway_run


def test_gateway_plugin_discovery_failure_is_warning(caplog, monkeypatch):
    plugin_mod = types.ModuleType("hermes_cli.plugins")

    def boom():
        raise RuntimeError("plugin registry offline")

    plugin_mod.discover_plugins = boom
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugin_mod)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        gateway_run._discover_gateway_plugins()

    warning_records = [
        record
        for record in caplog.records
        if record.name == "gateway.run"
        and record.levelno == logging.WARNING
        and record.getMessage() == "plugin discovery failed at gateway startup"
    ]
    assert len(warning_records) == 1
    assert warning_records[0].exc_info is not None
    assert "plugin registry offline" in caplog.text


def test_gateway_plugin_discovery_success_has_no_warning(caplog, monkeypatch):
    plugin_mod = types.ModuleType("hermes_cli.plugins")
    called = {"count": 0}

    def discover_plugins():
        called["count"] += 1

    plugin_mod.discover_plugins = discover_plugins
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugin_mod)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        gateway_run._discover_gateway_plugins()

    assert called == {"count": 1}
    assert "plugin discovery failed at gateway startup" not in caplog.text
