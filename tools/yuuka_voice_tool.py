#!/usr/bin/env python3
"""Curated Hayase Yuuka voice clip selector and downloader."""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import quote

from hermes_constants import display_hermes_home, get_hermes_home
from tools.registry import registry

logger = logging.getLogger(__name__)

_RECENT_CLIP_HISTORY: dict[str, deque[str]] = {}
_RECENT_CLIP_HISTORY_LOCK = Lock()
_RECENT_CLIP_WINDOW = 3
_RECENT_CLIP_BUFFER = 5
_MAX_CLIP_BYTES = 5 * 1024 * 1024
_ALLOWED_CONTENT_TYPES = {"application/octet-stream", "application/ogg"}

_ALLOWED_VARIANTS = {"base", "pajama", "sportswear"}
_ALLOWED_CLIP_CLASSES = {"semantic_extension", "mood_coloring"}
_ALLOWED_SITUATIONS = {
    "greet",
    "work_start",
    "work_pressure",
    "late",
    "warning",
    "tease",
    "rest",
    "tired",
    "health",
    "praise",
    "success",
    "assist",
    "fun",
    "planning",
}
_ALLOWED_MOODS = {
    "warm",
    "strict",
    "managerial",
    "playful",
    "shy",
    "tired",
    "confident",
    "flustered",
}


def _tool_dirs() -> dict[str, Path]:
    base = get_hermes_home() / "media_cache" / "yuuka-voice"
    return {
        "base": base,
        "clips": base / "clips",
    }



def _ensure_tool_dirs() -> dict[str, Path]:
    paths = _tool_dirs()
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths



def _bundled_catalog_path() -> Path:
    return Path(__file__).resolve().parent / "references" / "yuuka_voice_catalog_seed.json"



def _catalog_override_path() -> Path:
    return get_hermes_home() / "voice_catalogs" / "yuuka" / "catalog.json"



def _load_catalog() -> list[dict[str, Any]]:
    sources = [_catalog_override_path(), _bundled_catalog_path()]
    for path in sources:
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Yuuka voice catalog is not a list: {path}")
        return data
    raise FileNotFoundError(
        f"No Yuuka voice catalog found. Expected {display_hermes_home()}/voice_catalogs/yuuka/catalog.json or bundled seed data."
    )



def _normalize_tag_list(values: list[str] | None, *, allowed: set[str]) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        item = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not item:
            continue
        if item not in allowed:
            raise ValueError(f"Unsupported tag '{value}'. Allowed: {sorted(allowed)}")
        if item not in normalized:
            normalized.append(item)
    return normalized



def _normalize_variant(value: str | None) -> str:
    variant = str(value or "auto").strip().lower()
    if variant == "auto":
        return variant
    if variant not in _ALLOWED_VARIANTS:
        raise ValueError(f"Unsupported variant '{value}'. Allowed: auto, {sorted(_ALLOWED_VARIANTS)}")
    return variant



def _normalize_clip_class(value: str | None) -> str:
    clip_class = str(value or "auto").strip().lower()
    if clip_class == "auto":
        return clip_class
    if clip_class not in _ALLOWED_CLIP_CLASSES:
        raise ValueError(
            f"Unsupported clip_class '{value}'. Allowed: auto, {sorted(_ALLOWED_CLIP_CLASSES)}"
        )
    return clip_class



def _normalize_download_flag(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    raise ValueError("download must be a boolean")



def _query_tokens(query: str | None) -> list[str]:
    parts = re.split(r"[^\w]+", (query or "").lower())
    tokens: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if len(part) < 2 or part in seen:
            continue
        seen.add(part)
        tokens.append(part)
    return tokens



def _history_scope_key(task_id: str | None) -> str | None:
    scope = str(task_id or "").strip()
    return scope or None



def _recent_clip_ids_for_scope(task_id: str | None, *, limit: int = _RECENT_CLIP_WINDOW) -> list[str]:
    scope = _history_scope_key(task_id)
    if not scope:
        return []
    with _RECENT_CLIP_HISTORY_LOCK:
        recent = list(_RECENT_CLIP_HISTORY.get(scope, ()))
    if limit <= 0:
        return recent
    return recent[-limit:]



def _record_recent_clip(task_id: str | None, clip_id: str | None) -> None:
    scope = _history_scope_key(task_id)
    normalized_clip_id = str(clip_id or "").strip()
    if not scope or not normalized_clip_id:
        return
    with _RECENT_CLIP_HISTORY_LOCK:
        history = _RECENT_CLIP_HISTORY.setdefault(scope, deque(maxlen=_RECENT_CLIP_BUFFER))
        history.append(normalized_clip_id)



def _recency_adjustment(entry_id: str, recent_clip_ids: list[str]) -> tuple[int, list[str]]:
    if not recent_clip_ids:
        return 0, []

    recent_tail = [item for item in recent_clip_ids if item]
    if not recent_tail:
        return 0, []
    if recent_tail[-1] == entry_id:
        return -10_000, ["recent:immediate_repeat_block"]

    penalties_by_distance = {
        2: 8,
        3: 5,
    }
    for distance, clip_id in enumerate(reversed(recent_tail), start=1):
        if distance == 1 or clip_id != entry_id:
            continue
        penalty = penalties_by_distance.get(distance, 3)
        return -penalty, [f"recent:penalty_{distance}"]

    return 0, []



def _entry_search_blob(entry: dict[str, Any]) -> str:
    bits = [
        entry.get("id", ""),
        entry.get("variant", ""),
        entry.get("clip_class", ""),
        " ".join(entry.get("situations", [])),
        " ".join(entry.get("moods", [])),
        entry.get("paraphrase_en", ""),
        entry.get("text_jp", ""),
    ]
    return " ".join(bit for bit in bits if bit).lower()



def _score_entry(
    entry: dict[str, Any],
    *,
    variant: str,
    clip_class: str,
    situations: list[str],
    moods: list[str],
    query: str | None,
    recent_clip_ids: list[str],
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    entry_variant = str(entry.get("variant", "")).strip().lower()
    entry_class = str(entry.get("clip_class", "")).strip().lower()
    entry_situations = {str(item).strip().lower() for item in entry.get("situations", [])}
    entry_moods = {str(item).strip().lower() for item in entry.get("moods", [])}

    if variant != "auto":
        if entry_variant == variant:
            score += 2
            reasons.append(f"variant:{variant}")
        else:
            score -= 1

    if clip_class != "auto":
        if entry_class == clip_class:
            score += 4
            reasons.append(f"class:{clip_class}")
        else:
            score -= 1

    if situations:
        overlap = [tag for tag in situations if tag in entry_situations]
        if overlap:
            score += 5 * len(overlap)
            reasons.extend(f"situation:{tag}" for tag in overlap)
        else:
            score -= 3

    if moods:
        overlap = [tag for tag in moods if tag in entry_moods]
        if overlap:
            score += 2 * len(overlap)
            reasons.extend(f"mood:{tag}" for tag in overlap)
        else:
            score -= 1

    blob = _entry_search_blob(entry)
    for token in _query_tokens(query):
        if token in blob:
            score += 1
            reasons.append(f"query:{token}")

    recency_delta, recency_reasons = _recency_adjustment(str(entry.get("id", "")), recent_clip_ids)
    score += recency_delta
    reasons.extend(recency_reasons)

    return score, reasons



def select_yuuka_voice_entry(
    *,
    variant: str = "auto",
    clip_class: str = "auto",
    situations: list[str] | None = None,
    moods: list[str] | None = None,
    query: str | None = None,
    top_k: int = 3,
    recent_clip_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    variant = _normalize_variant(variant)
    clip_class = _normalize_clip_class(clip_class)
    situations = _normalize_tag_list(situations, allowed=_ALLOWED_SITUATIONS)
    moods = _normalize_tag_list(moods, allowed=_ALLOWED_MOODS)
    top_k = max(1, min(int(top_k), 5))
    recent_clip_ids = [str(item or "").strip() for item in (recent_clip_ids or []) if str(item or "").strip()]

    if not situations and not moods and not query:
        return None

    scored: list[tuple[int, dict[str, Any], list[str]]] = []
    for entry in _load_catalog():
        score, reasons = _score_entry(
            entry,
            variant=variant,
            clip_class=clip_class,
            situations=situations,
            moods=moods,
            query=query,
            recent_clip_ids=recent_clip_ids,
        )
        scored.append((score, entry, reasons))

    if not scored:
        return None

    scored.sort(key=lambda row: (row[0], row[1].get("id", "")), reverse=True)
    best_score, best_entry, best_reasons = scored[0]
    if best_score <= 0:
        return None

    alternatives = []
    for alt_score, alt_entry, alt_reasons in scored[1:top_k]:
        if alt_score <= 0:
            continue
        alternatives.append(
            {
                "id": alt_entry.get("id"),
                "variant": alt_entry.get("variant"),
                "file_name": alt_entry.get("file_name"),
                "clip_class": alt_entry.get("clip_class"),
                "paraphrase_en": alt_entry.get("paraphrase_en"),
                "score": alt_score,
                "reasons": alt_reasons,
            }
        )

    selected = dict(best_entry)
    selected["score"] = best_score
    selected["reasons"] = best_reasons
    selected["alternatives"] = alternatives
    return selected



def _build_redirect_url(file_name: str) -> str:
    return f"https://bluearchive.wiki/wiki/Special:Redirect/file/{quote(file_name, safe='')}"



def _cache_path_for(file_name: str) -> Path:
    _ensure_tool_dirs()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name)
    return _tool_dirs()["clips"] / safe_name



def _download_clip(file_name: str) -> Path:
    target_path = _cache_path_for(file_name)
    if target_path.is_file() and target_path.stat().st_size > 0:
        return target_path

    url = _build_redirect_url(file_name)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)"})
    with urllib.request.urlopen(request, timeout=60) as response:
        content_type_header = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        if content_type_header and not (
            content_type_header.startswith("audio/") or content_type_header in _ALLOWED_CONTENT_TYPES
        ):
            raise ValueError(f"Unexpected content type for {file_name}: {content_type_header}")

        content_length_header = str(response.headers.get("Content-Length", "")).strip()
        if content_length_header:
            try:
                content_length = int(content_length_header)
            except ValueError:
                content_length = None
            else:
                if content_length > _MAX_CLIP_BYTES:
                    raise ValueError(f"Clip is too large to cache safely: {content_length} bytes")

        chunks: list[bytes] = []
        total_size = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > _MAX_CLIP_BYTES:
                raise ValueError(f"Clip is too large to cache safely: {total_size} bytes")
            chunks.append(chunk)

    data = b"".join(chunks)
    if not data:
        raise ValueError(f"Downloaded empty clip for {file_name}")
    if not data.startswith(b"OggS"):
        raise ValueError(f"Downloaded clip is not a valid Ogg audio file: {file_name}")

    target_path.write_bytes(data)
    return target_path



def yuuka_voice_reply_tool(
    *,
    variant: str = "auto",
    clip_class: str = "auto",
    situations: list[str] | None = None,
    moods: list[str] | None = None,
    query: str | None = None,
    download: bool = True,
    top_k: int = 3,
    task_id: str | None = None,
) -> str:
    try:
        recent_clip_ids = _recent_clip_ids_for_scope(task_id)
        selected = select_yuuka_voice_entry(
            variant=variant,
            clip_class=clip_class,
            situations=situations,
            moods=moods,
            query=query,
            top_k=top_k,
            recent_clip_ids=recent_clip_ids,
        )
        if not selected:
            return json.dumps(
                {
                    "success": False,
                    "error": "no_matching_clip",
                    "detail": "No Yuuka voice clip matched the requested context strongly enough.",
                },
                ensure_ascii=False,
            )

        file_path = None
        media_tag = None
        if download:
            downloaded = _download_clip(str(selected["file_name"]))
            file_path = str(downloaded)
            media_tag = f"MEDIA:{file_path}"

        _record_recent_clip(task_id, str(selected.get("id", "")))

        return json.dumps(
            {
                "success": True,
                "clip": {
                    "id": selected.get("id"),
                    "variant": selected.get("variant"),
                    "clip_class": selected.get("clip_class"),
                    "file_name": selected.get("file_name"),
                    "paraphrase_en": selected.get("paraphrase_en"),
                    "text_jp": selected.get("text_jp"),
                    "situations": selected.get("situations", []),
                    "moods": selected.get("moods", []),
                    "score": selected.get("score"),
                    "reasons": selected.get("reasons", []),
                },
                "alternatives": selected.get("alternatives", []),
                "source_url": _build_redirect_url(str(selected["file_name"])),
                "file_path": file_path,
                "media_tag": media_tag,
                "cached_under": str(_tool_dirs()["base"]),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.warning("Yuuka voice tool failed: %s", exc)
        return json.dumps(
            {
                "success": False,
                "error": "yuuka_voice_failed",
                "detail": str(exc),
            },
            ensure_ascii=False,
        )


YUUKA_VOICE_SCHEMA = {
    "name": "yuuka_voice_reply",
    "description": (
        "Choose and cache a semantically aligned Hayase Yuuka in-game voice clip from a curated v1 catalog. "
        "Best for Discord/Telegram character reactions when the clip should extend the current reply rather than replace it. "
        "Use short structured tags (situations, moods, clip_class) instead of pasting long conversation history. "
        f"Downloaded clips are cached under {display_hermes_home()}/media_cache/yuuka-voice/."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "variant": {
                "type": "string",
                "enum": ["auto", "base", "pajama", "sportswear"],
                "description": "Preferred Yuuka variant. Use auto when not important.",
            },
            "clip_class": {
                "type": "string",
                "enum": ["auto", "semantic_extension", "mood_coloring"],
                "description": "semantic_extension = meaningfully extends the answer; mood_coloring = broad mood line that still fits context.",
            },
            "situations": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": sorted(_ALLOWED_SITUATIONS),
                },
                "description": "Short situation tags for the current reply, e.g. greet, work_pressure, late, rest, tease.",
            },
            "moods": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": sorted(_ALLOWED_MOODS),
                },
                "description": "Short speaker-tone tags for the current reply, e.g. strict, warm, managerial, playful, shy.",
            },
            "query": {
                "type": "string",
                "description": "Optional short English hint for matching/paraphrase lookup, not long chat history.",
            },
            "download": {
                "type": "boolean",
                "description": "When true, download/cache the selected clip locally and return MEDIA:path.",
                "default": True,
            },
            "top_k": {
                "type": "integer",
                "description": "How many top candidates to consider/return (1-5).",
                "default": 3,
                "minimum": 1,
                "maximum": 5,
            },
        },
    },
}


registry.register(
    name="yuuka_voice_reply",
    toolset="yuuka_voice",
    schema=YUUKA_VOICE_SCHEMA,
    handler=lambda args, **kw: yuuka_voice_reply_tool(
        variant=args.get("variant", "auto"),
        clip_class=args.get("clip_class", "auto"),
        situations=args.get("situations"),
        moods=args.get("moods"),
        query=args.get("query"),
        download=_normalize_download_flag(args.get("download", True)),
        top_k=args.get("top_k", 3),
        task_id=kw.get("task_id"),
    ),
    emoji="🎙️",
)
