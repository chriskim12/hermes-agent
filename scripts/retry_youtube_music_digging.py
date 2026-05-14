#!/usr/bin/env python3
"""Retry queued YouTube music digging requests.

Intended for Hermes cron. The script stays silent when there is no work so a
no_agent cron can use stdout as the delivery body without noisy heartbeats.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.youtube_music_digging_tool import retry_pending_music_digs  # noqa: E402


def main() -> int:
    raw = retry_pending_music_digs(limit=5)
    result = json.loads(raw)
    processed = int(result.get("processed") or 0)
    if processed <= 0:
        return 0

    lines = [f"대기 중이던 음악 링크 {processed}개 재처리했어요."]
    for item in result.get("results", []):
        status = item.get("status") or "failed"
        qid = item.get("queue_id")
        if item.get("success") and status in {"uploaded", "duplicate"}:
            title = item.get("title") or ""
            artist = item.get("artist") or ""
            name = item.get("filename") or ""
            drive = (item.get("drive") or item.get("existing") or {}).get("webViewLink") or ""
            label = f"{artist} - {title}".strip(" -") or name or item.get("source", "")
            if status == "duplicate":
                lines.append(f"- #{qid}: 이미 있음 — {label}" + (f" / {drive}" if drive else ""))
            else:
                lines.append(f"- #{qid}: 완료 — {label}" + (f" / {drive}" if drive else ""))
        else:
            stage = item.get("stage") or "unknown"
            error = item.get("error") or item.get("detail") or "failed"
            lines.append(f"- #{qid}: 아직 실패 — {stage}: {error}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
