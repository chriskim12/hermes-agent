#!/usr/bin/env python3
"""Inventory-backed repo drift/apply gate entrypoint."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cron.repo_update_gate import main


if __name__ == "__main__":
    raise SystemExit(main())
