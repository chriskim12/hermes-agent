"""Cheap, non-mutating readiness checks for insane-search integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    missing: list[str] = field(default_factory=list)
    optional_missing: list[str] = field(default_factory=list)
    learned_store: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "missing": list(self.missing),
            "optional_missing": list(self.optional_missing),
            "learned_store": self.learned_store,
            "diagnostics": dict(self.diagnostics),
        }


def default_learned_store_path() -> Path:
    return get_hermes_home() / "web" / "insane_search" / "learned_routes.json"


def check_readiness(*, learned_store: str | Path | None = None) -> ReadinessReport:
    """Return dependency readiness without importing network clients or installing anything."""
    required: tuple[str, ...] = ()
    optional = ("curl_cffi", "bs4", "playwright", "yaml")
    missing = [name for name in required if find_spec(name) is None]
    optional_missing = [name for name in optional if find_spec(name) is None]
    store = Path(learned_store) if learned_store else default_learned_store_path()
    return ReadinessReport(
        ready=not missing,
        missing=missing,
        optional_missing=optional_missing,
        learned_store=str(store),
        diagnostics={
            "non_mutating": True,
            "network": False,
            "installer": False,
            "vendor_package": "plugins.web.insane_search.vendor.insane_search_engine",
        },
    )
