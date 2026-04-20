#!/usr/bin/env python3
"""YouTube audio download and MP3 conversion tool."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
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
_AGENT_BROWSER_COOKIE_DOMAINS = (
    ".youtube.com",
    "youtube.com",
    ".google.com",
    "google.com",
    "accounts.google.com",
)
_YOUTUBE_AUTH_CHALLENGE_MARKERS = (
    "sign in to confirm you're not a bot",
    "sign in to confirm you’re not a bot",
)


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


def _youtube_cookie_file() -> Path | None:
    candidate = get_hermes_home() / "secrets" / "youtube-cookies.txt"
    try:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    except OSError:
        return None
    return None


def _agent_browser_profile_path() -> Path:
    return get_hermes_home() / "integrations" / "youtube-agent-browser" / "profile"


def _agent_browser_command() -> str | None:
    command = shutil.which("agent-browser")
    if command:
        return command
    local_bin = get_hermes_home() / "hermes-agent" / "node_modules" / ".bin" / "agent-browser"
    if local_bin.is_file():
        return str(local_bin)
    return None


def _is_google_or_youtube_cookie_domain(domain: str) -> bool:
    normalized = (domain or "").strip().lower().lstrip(".")
    if not normalized:
        return False

    for candidate in _AGENT_BROWSER_COOKIE_DOMAINS:
        base = candidate.lower().lstrip(".")
        if normalized == base or normalized.endswith(f".{base}"):
            return True
    return False


def _netscape_bool(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def _cookie_to_netscape_line(cookie: dict[str, Any]) -> str | None:
    domain = str(cookie.get("domain") or "").strip()
    name = str(cookie.get("name") or "").strip()
    value = str(cookie.get("value") or "")
    if not domain or not name or not _is_google_or_youtube_cookie_domain(domain):
        return None

    path = str(cookie.get("path") or "/")
    secure = bool(cookie.get("secure", False))
    expires_raw = cookie.get("expires", 0)
    try:
        expires = int(expires_raw)
    except (TypeError, ValueError):
        expires = 0
    include_subdomains = domain.startswith(".")
    return "\t".join(
        [
            domain,
            _netscape_bool(include_subdomains),
            path,
            _netscape_bool(secure),
            str(max(expires, 0)),
            name,
            value,
        ]
    )


def _refresh_youtube_cookies_from_agent_browser() -> dict[str, Any]:
    agent_browser = _agent_browser_command()
    profile_path = _agent_browser_profile_path()
    if not agent_browser:
        return {
            "success": False,
            "error": "missing_agent_browser",
            "detail": "agent-browser is not installed.",
            "profile_path": str(profile_path),
            "source": "agent_browser",
        }

    profile_path.mkdir(parents=True, exist_ok=True)
    open_result = subprocess.run(
        [agent_browser, "--profile", str(profile_path), "open", "https://www.youtube.com", "--json"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if open_result.returncode != 0:
        return {
            "success": False,
            "error": "agent_browser_open_failed",
            "detail": (open_result.stderr or open_result.stdout or "agent-browser open failed").strip(),
            "profile_path": str(profile_path),
            "source": "agent_browser",
        }

    cookie_result = subprocess.run(
        [agent_browser, "--profile", str(profile_path), "cookies", "--json"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if cookie_result.returncode != 0:
        return {
            "success": False,
            "error": "agent_browser_cookie_read_failed",
            "detail": (cookie_result.stderr or cookie_result.stdout or "agent-browser cookies failed").strip(),
            "profile_path": str(profile_path),
            "source": "agent_browser",
        }

    try:
        payload = json.loads(cookie_result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {
            "success": False,
            "error": "agent_browser_cookie_parse_failed",
            "detail": str(exc),
            "profile_path": str(profile_path),
            "source": "agent_browser",
        }

    cookies = payload.get("data", {}).get("cookies", []) if isinstance(payload, dict) else []
    lines = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        line = _cookie_to_netscape_line(cookie)
        if line:
            lines.append(line)

    if not lines:
        return {
            "success": False,
            "error": "no_youtube_cookies_found",
            "detail": "No Google/YouTube cookies were available in the agent-browser profile.",
            "profile_path": str(profile_path),
            "source": "agent_browser",
        }

    cookie_path = get_hermes_home() / "secrets" / "youtube-cookies.txt"
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_text(
        "# Netscape HTTP Cookie File\n" + "\n".join(sorted(lines)) + "\n",
        encoding="utf-8",
    )
    cookie_path.chmod(0o600)
    return {
        "success": True,
        "cookie_count": len(lines),
        "cookie_path": str(cookie_path),
        "profile_path": str(profile_path),
        "source": "agent_browser",
    }


def _is_youtube_auth_challenge(detail: str) -> bool:
    lowered = (detail or "").strip().lower()
    return any(marker in lowered for marker in _YOUTUBE_AUTH_CHALLENGE_MARKERS)


def _yt_dlp_base_command() -> list[str]:
    command = ["yt-dlp"]
    cookie_file = _youtube_cookie_file()
    if cookie_file is not None:
        command.extend(["--cookies", str(cookie_file)])
    if shutil.which("node") is not None:
        command.extend(["--js-runtimes", "node"])
    return command


def _google_drive_folder_config_path() -> Path:
    return get_hermes_home() / "integrations" / "youtube-audio-google-drive-folder.txt"


def _google_drive_folder_id() -> str | None:
    candidate = _google_drive_folder_config_path()
    try:
        folder_id = candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return folder_id or None


def _google_workspace_bridge_path() -> Path | None:
    candidate = get_hermes_home() / "skills" / "productivity" / "google-workspace" / "scripts" / "gws_bridge.py"
    return candidate if candidate.is_file() else None


def _upload_file_to_google_drive(path: Path) -> dict[str, Any] | None:
    folder_id = _google_drive_folder_id()
    if not folder_id:
        return None

    bridge_path = _google_workspace_bridge_path()
    if bridge_path is None:
        return {
            "success": False,
            "folder_id": folder_id,
            "error": "missing_google_workspace_bridge",
            "detail": "Google Workspace bridge script is not installed.",
        }

    token_path = get_hermes_home() / "google_token.json"
    if not token_path.is_file():
        return {
            "success": False,
            "folder_id": folder_id,
            "error": "missing_google_auth",
            "detail": f"No Google token found at {token_path}.",
        }

    upload_result = subprocess.run(
        [
            sys.executable,
            str(bridge_path),
            "drive",
            "+upload",
            str(path),
            "--parent",
            folder_id,
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if upload_result.returncode != 0:
        return {
            "success": False,
            "folder_id": folder_id,
            "error": "drive_upload_failed",
            "detail": (upload_result.stderr or upload_result.stdout or "gws drive upload failed").strip(),
        }

    raw_output = (upload_result.stdout or "").strip()
    payload: dict[str, Any] = {}
    if raw_output:
        try:
            parsed = json.loads(raw_output)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}

    result: dict[str, Any] = {
        "success": True,
        "folder_id": folder_id,
    }
    file_id = payload.get("id") or payload.get("fileId") or payload.get("file_id")
    name = payload.get("name") or payload.get("filename")
    web_view_link = payload.get("webViewLink") or payload.get("web_view_link") or payload.get("webLink")
    if file_id:
        result["file_id"] = file_id
    if name:
        result["name"] = name
    if web_view_link:
        result["web_view_link"] = web_view_link
    if payload:
        result["raw"] = payload
    return result


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
        _yt_dlp_base_command() + ["--dump-single-json", "--no-playlist", url],
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
        auth_refresh: dict[str, Any] | None = None
        download_command = _yt_dlp_base_command() + [
            "--no-playlist",
            "-f",
            "bestaudio/best",
            "-o",
            str(incoming_path),
            url,
        ]
        download_result = subprocess.run(
            download_command,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if download_result.returncode != 0:
            download_error = (download_result.stderr or download_result.stdout or "yt-dlp failed").strip()
            if _is_youtube_auth_challenge(download_error):
                auth_refresh = _refresh_youtube_cookies_from_agent_browser()
                if auth_refresh.get("success"):
                    download_result = subprocess.run(
                        _yt_dlp_base_command()
                        + [
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
                    download_error = (download_result.stderr or download_result.stdout or "yt-dlp failed").strip()
            if download_result.returncode != 0:
                return _error_payload(
                    "download_failed",
                    download_error,
                    source_url=url,
                    video_id=video_id,
                    auth_refresh=auth_refresh,
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

        drive_upload = _upload_file_to_google_drive(processed_path)
        if drive_upload and not drive_upload.get("success"):
            warnings.append(
                f"google drive upload failed: {drive_upload.get('detail') or drive_upload.get('error') or 'unknown error'}"
            )

        payload = {
            "success": True,
            "file_path": str(processed_path),
            "title": title,
            "artist": artist,
            "artist_inferred": artist_inferred,
            "source_url": url,
            "video_id": video_id,
            "warnings": warnings,
        }
        if auth_refresh is not None:
            payload["auth_refresh"] = auth_refresh
        if drive_upload is not None:
            payload["drive_upload"] = drive_upload

        return json.dumps(payload)
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
