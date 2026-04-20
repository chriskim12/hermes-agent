#!/usr/bin/env python3
"""YouTube audio download and MP3 conversion tool."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from hermes_constants import display_hermes_home, get_hermes_home
from tools.registry import registry

SUPPORTED_BITRATES = {"192k", "256k", "320k"}
_SUPPORTED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def _tool_dirs() -> dict[str, Path]:
    base = get_hermes_home() / "media_cache" / "youtube-audio"
    return {
        "base": base,
        "incoming": base / "incoming",
        "processed": base / "processed",
        "failed": base / "failed",
    }


def _ensure_tool_dirs() -> dict[str, Path]:
    paths = _tool_dirs()
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _mutagen_available() -> bool:
    try:
        import mutagen  # noqa: F401
        return True
    except ImportError:
        return False


def check_youtube_audio_requirements() -> bool:
    return all(
        [
            shutil.which("yt-dlp") is not None,
            shutil.which("ffmpeg") is not None,
            _mutagen_available(),
        ]
    )


def is_supported_youtube_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower()
    return host in _SUPPORTED_HOSTS


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if host == "youtu.be":
        video_id = parsed.path.strip("/")
        return video_id or None
    if host in _SUPPORTED_HOSTS:
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if video_id:
            return video_id
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed"}:
            return parts[1]
    return None


def cleanup_youtube_title(title: str) -> str:
    cleaned = (title or "").strip()
    patterns = [
        r"\s*\((official\s+video|official\s+music\s+video|lyrics?|audio|visualizer|hd|4k)\)\s*",
        r"\s*\[(official\s+video|official\s+music\s+video|lyrics?|audio|visualizer|hd|4k)\]\s*",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")
    return cleaned or (title or "").strip()


def infer_artist_title(title: str) -> dict[str, Any]:
    cleaned = cleanup_youtube_title(title)
    for separator in (" - ", " – ", " — "):
        if separator in cleaned:
            artist, song_title = cleaned.split(separator, 1)
            artist = artist.strip()
            song_title = song_title.strip()
            if artist and song_title:
                return {
                    "artist": artist,
                    "title": song_title,
                    "artist_inferred": True,
                }
    return {
        "artist": None,
        "title": cleaned,
        "artist_inferred": False,
    }


def sanitize_filename(value: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', (value or '').strip())
    sanitized = re.sub(r'_+', '_', sanitized).strip(' ._')
    return sanitized or "youtube_audio"


def _error_payload(error: str, detail: str, **extra: Any) -> str:
    payload = {"success": False, "error": error, "detail": detail}
    payload.update(extra)
    return json.dumps(payload)


def _extract_media_info(path: Path) -> dict[str, Any]:
    from mutagen import File as MutagenFile

    warnings: list[str] = []
    audio = MutagenFile(path, easy=True)
    if audio is None:
        warnings.append("mutagen could not parse output metadata; using filename/title inference")
        inferred = infer_artist_title(path.stem)
        return {
            "title": inferred["title"],
            "artist": inferred["artist"],
            "artist_inferred": inferred["artist_inferred"],
            "warnings": warnings,
        }

    tags = getattr(audio, "tags", {}) or {}
    title = None
    artist = None
    if hasattr(tags, "get"):
        title_values = tags.get("title") or []
        artist_values = tags.get("artist") or []
        title = title_values[0] if title_values else None
        artist = artist_values[0] if artist_values else None

    title = cleanup_youtube_title(title or path.stem)
    inferred = infer_artist_title(title)
    artist_inferred = False
    if not artist:
        artist = inferred["artist"]
        artist_inferred = bool(artist)
        if artist:
            warnings.append("artist inferred from title")

    return {
        "title": inferred["title"] if artist_inferred else title,
        "artist": artist,
        "artist_inferred": artist_inferred,
        "warnings": warnings,
    }


def _fetch_youtube_metadata(url: str, video_id: str) -> dict[str, Any]:
    result = subprocess.run(
        ["yt-dlp", "--dump-single-json", "--no-playlist", url],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        return {
            "id": video_id,
            "warnings": [
                (result.stderr or result.stdout or "yt-dlp metadata lookup failed").strip()
            ],
        }
    try:
        payload = json.loads(result.stdout or "{}")
        if not isinstance(payload, dict):
            raise ValueError("metadata payload was not an object")
        payload.setdefault("id", video_id)
        return payload
    except Exception as exc:
        return {"id": video_id, "warnings": [f"metadata lookup parse failed: {exc}"]}


def _year_from_upload_date(upload_date: Any) -> str | None:
    if not upload_date:
        return None
    text = str(upload_date).strip()
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    return None


def _write_id3_tags(path: Path, metadata: dict[str, Any]) -> None:
    from mutagen.id3 import ID3, COMM, TALB, TDRC, TIT2, TPE1

    tags = ID3()
    title = (metadata.get("title") or "").strip()
    artist = (metadata.get("artist") or "").strip()
    album = (metadata.get("album") or "").strip()
    source_url = (metadata.get("source_url") or "").strip()
    year = (metadata.get("year") or "").strip()

    if title:
        tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.add(TPE1(encoding=3, text=artist))
    if album:
        tags.add(TALB(encoding=3, text=album))
    if year:
        tags.add(TDRC(encoding=3, text=year))
    if source_url:
        tags.add(COMM(encoding=3, lang="eng", desc="source_url", text=source_url))

    tags.save(path)


def youtube_to_mp3(url: str, preferred_bitrate: str = "320k", task_id: str | None = None) -> str:
    del task_id
    if not is_supported_youtube_url(url):
        return _error_payload("unsupported_url", "Only YouTube URLs are supported.", source_url=url)

    if preferred_bitrate not in SUPPORTED_BITRATES:
        return _error_payload(
            "invalid_bitrate",
            f"preferred_bitrate must be one of {sorted(SUPPORTED_BITRATES)}.",
            supported_bitrates=sorted(SUPPORTED_BITRATES),
            source_url=url,
        )

    if not check_youtube_audio_requirements():
        missing = []
        if shutil.which("yt-dlp") is None:
            missing.append("yt-dlp")
        if shutil.which("ffmpeg") is None:
            missing.append("ffmpeg")
        if not _mutagen_available():
            missing.append("mutagen")
        return _error_payload(
            "missing_dependency",
            f"Missing required dependency/dependencies: {', '.join(missing)}.",
            source_url=url,
        )

    video_id = extract_video_id(url)
    if not video_id:
        return _error_payload("invalid_url", "Could not extract a YouTube video ID.", source_url=url)

    paths = _ensure_tool_dirs()
    incoming_path = paths["incoming"] / f"{video_id}.source"
    processed_path = paths["processed"] / f"{video_id}-{preferred_bitrate}.mp3"
    failed_path = paths["failed"] / f"{video_id}-{preferred_bitrate}.source"
    youtube_metadata = _fetch_youtube_metadata(url, video_id)
    metadata_warnings = list(youtube_metadata.get("warnings") or [])
    metadata_title = cleanup_youtube_title(str(youtube_metadata.get("title") or "").strip())
    metadata_artist = (
        str(youtube_metadata.get("uploader") or youtube_metadata.get("channel") or "").strip() or None
    )
    metadata_year = _year_from_upload_date(youtube_metadata.get("upload_date"))

    try:
        download_result = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "-f",
                "bestaudio/best",
                "-o",
                str(incoming_path),
                url,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if download_result.returncode != 0:
            return _error_payload(
                "download_failed",
                (download_result.stderr or download_result.stdout or "yt-dlp failed").strip(),
                source_url=url,
                video_id=video_id,
            )

        conversion_result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(incoming_path),
                "-vn",
                "-acodec",
                "libmp3lame",
                "-b:a",
                preferred_bitrate,
                str(processed_path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if conversion_result.returncode != 0:
            if incoming_path.exists():
                shutil.move(str(incoming_path), failed_path)
            return _error_payload(
                "conversion_failed",
                (conversion_result.stderr or conversion_result.stdout or "ffmpeg failed").strip(),
                source_url=url,
                video_id=video_id,
            )

        if incoming_path.exists():
            incoming_path.unlink()

        if not processed_path.exists():
            return _error_payload(
                "missing_output",
                "Conversion completed but no MP3 file was produced.",
                source_url=url,
                video_id=video_id,
            )

        media_info = _extract_media_info(processed_path)
        warnings = metadata_warnings + list(media_info.get("warnings", []))
        raw_title = media_info.get("title")
        derived_title = None
        if raw_title:
            normalized_raw_title = cleanup_youtube_title(str(raw_title).strip())
            if normalized_raw_title != processed_path.stem:
                derived_title = normalized_raw_title
        if not derived_title and metadata_title:
            inferred_from_metadata = infer_artist_title(metadata_title)
            derived_title = inferred_from_metadata["title"] or metadata_title
        title = derived_title or processed_path.stem
        artist = media_info.get("artist") or metadata_artist
        artist_inferred = bool(media_info.get("artist_inferred", False))
        if not media_info.get("artist") and metadata_artist:
            artist_inferred = True
            warnings.append("artist inferred from uploader/channel metadata")

        _write_id3_tags(
            processed_path,
            {
                "title": title,
                "artist": artist,
                "source_url": url,
                "year": metadata_year,
            },
        )

        return json.dumps(
            {
                "success": True,
                "file_path": str(processed_path),
                "title": title,
                "artist": artist,
                "artist_inferred": artist_inferred,
                "source_url": url,
                "video_id": video_id,
                "warnings": warnings,
            }
        )
    except subprocess.TimeoutExpired as exc:
        if incoming_path.exists():
            shutil.move(str(incoming_path), failed_path)
        return _error_payload(
            "timeout",
            f"Process timed out after {exc.timeout} seconds.",
            source_url=url,
            video_id=video_id,
        )
    except Exception as exc:
        if incoming_path.exists() and not failed_path.exists():
            shutil.move(str(incoming_path), failed_path)
        return _error_payload(
            "unexpected_error",
            str(exc),
            source_url=url,
            video_id=video_id,
        )


registry.register(
    name="youtube_to_mp3",
    toolset="youtube_audio",
    schema={
        "name": "youtube_to_mp3",
        "description": (
            "Download audio from a supported YouTube URL, convert it to MP3 with ffmpeg, "
            f"and store it under {display_hermes_home()}/media_cache/youtube-audio/."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "A YouTube URL from youtube.com, music.youtube.com, m.youtube.com, or youtu.be.",
                },
                "preferred_bitrate": {
                    "type": "string",
                    "enum": sorted(SUPPORTED_BITRATES),
                    "description": "Preferred MP3 bitrate.",
                    "default": "320k",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    handler=lambda args, **kwargs: youtube_to_mp3(
        url=args.get("url", ""),
        preferred_bitrate=args.get("preferred_bitrate", "320k"),
        task_id=kwargs.get("task_id"),
    ),
    check_fn=check_youtube_audio_requirements,
    description="Download a YouTube audio track and convert it to MP3.",
    emoji="🎵",
)
