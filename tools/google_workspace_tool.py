"""Profile-scoped Google Workspace tools for Drive/Gmail retrieval.

This module intentionally exposes a generic ``google_workspace`` surface instead
of project-specific tools such as ``whystarve_drive_search``.  Individual
projects are profiles in configuration; the tool code stays shared.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from tools.registry import registry

READONLY_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
READONLY_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
FORBIDDEN_GMAIL_SCOPES = {"gmail.send", "gmail.modify", "gmail.compose"}
FORBIDDEN_DRIVE_SCOPES = {"drive", "drive.file", "drive.appdata"}
_ALLOWED_GOOGLE_WORKSPACE_SCOPES = {READONLY_DRIVE_SCOPE, READONLY_GMAIL_SCOPE}
_PROFILE_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_.@:+/=-]+$")
_BWS_REF_RE = re.compile(r"^[a-zA-Z0-9_.@:+/=-]+$")
BWS_TIMEOUT_SECONDS = 15


class GoogleWorkspaceError(RuntimeError):
    """Fail-closed connector error returned to the model as structured JSON."""


@dataclass(frozen=True)
class WorkspaceProfile:
    name: str
    display_name: str
    auth_ref: str
    mode: str
    drive: dict[str, Any]
    gmail: dict[str, Any]


def _json_ok(data: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def _json_error(error: str, *, reason: str, **extra: Any) -> str:
    payload = {"ok": False, "error": error, "reason": reason}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _load_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _workspace_config() -> dict[str, Any]:
    cfg = _load_config()
    section = cfg.get("google_workspace") or cfg.get("google", {}).get("workspace") or {}
    return section if isinstance(section, dict) else {}


def _profiles_config() -> dict[str, Any]:
    profiles = _workspace_config().get("profiles") or {}
    return profiles if isinstance(profiles, dict) else {}


def check_google_workspace_requirements() -> bool:
    """Keep the toolset available; individual operations fail closed by policy.

    Hermes should be able to list configured profiles even before Google client
    libraries or credentials are installed.  Read/search handlers do their own
    prerequisite checks and return precise errors.
    """
    return True


def _require_profile(profile: str | None) -> WorkspaceProfile:
    if not profile or not str(profile).strip():
        raise GoogleWorkspaceError("profile_required")
    name = str(profile).strip()
    if not _PROFILE_RE.fullmatch(name):
        raise GoogleWorkspaceError("invalid_profile_name")
    profiles = _profiles_config()
    raw = profiles.get(name)
    if not isinstance(raw, dict):
        raise GoogleWorkspaceError("unknown_profile")
    mode = str(raw.get("mode") or "readonly").strip().lower()
    if mode != "readonly":
        raise GoogleWorkspaceError("profile_not_readonly")
    auth_ref = str(raw.get("auth_ref") or "").strip()
    if not auth_ref:
        raise GoogleWorkspaceError("profile_auth_ref_missing")
    return WorkspaceProfile(
        name=name,
        display_name=str(raw.get("display_name") or name),
        auth_ref=auth_ref,
        mode=mode,
        drive=raw.get("drive") if isinstance(raw.get("drive"), dict) else {},
        gmail=raw.get("gmail") if isinstance(raw.get("gmail"), dict) else {},
    )


def _bool_enabled(surface: dict[str, Any]) -> bool:
    return bool(surface.get("enabled", False))


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value if str(v).strip()]
    return []


def _validate_safe_identifier(value: str, field: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise GoogleWorkspaceError(f"invalid_{field}")


def _require_drive_policy(profile: WorkspaceProfile, *, folder: str | None = None) -> None:
    if not _bool_enabled(profile.drive):
        raise GoogleWorkspaceError("drive_disabled")
    allowed_drives = _as_list(profile.drive.get("allowed_shared_drives"))
    allowed_folders = _as_list(profile.drive.get("allowed_folders"))
    if not allowed_drives and not allowed_folders:
        raise GoogleWorkspaceError("drive_allowlist_required")
    scopes = {str(s).lower().removeprefix("https://www.googleapis.com/auth/") for s in _as_list(profile.drive.get("scopes"))}
    if scopes & FORBIDDEN_DRIVE_SCOPES:
        raise GoogleWorkspaceError("drive_forbidden_scope_configured")
    if scopes and not scopes <= {"drive.readonly"}:
        raise GoogleWorkspaceError("drive_forbidden_scope_configured")
    if folder:
        if folder not in allowed_folders:
            raise GoogleWorkspaceError("drive_folder_forbidden")
        _validate_safe_identifier(folder.replace(" ", "_"), "folder")


def _require_gmail_policy(profile: WorkspaceProfile, *, mailbox: str | None = None) -> None:
    if not _bool_enabled(profile.gmail):
        raise GoogleWorkspaceError("gmail_disabled")
    allowed_mailboxes = _as_list(profile.gmail.get("allowed_mailboxes"))
    if not allowed_mailboxes:
        raise GoogleWorkspaceError("gmail_mailbox_allowlist_required")
    scopes = {str(s).lower().removeprefix("https://www.googleapis.com/auth/") for s in _as_list(profile.gmail.get("scopes"))}
    if scopes & FORBIDDEN_GMAIL_SCOPES:
        raise GoogleWorkspaceError("gmail_forbidden_scope_configured")
    if scopes and not scopes <= {"gmail.readonly"}:
        raise GoogleWorkspaceError("gmail_forbidden_scope_configured")
    if mailbox:
        if mailbox not in allowed_mailboxes:
            raise GoogleWorkspaceError("gmail_mailbox_forbidden")
        _validate_safe_identifier(mailbox, "mailbox")


def _escape_drive_query(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _drive_list_call(service: Any, *, q: str, limit: int, drive_id: str | None = None) -> Any:
    params = {
        "q": q,
        "pageSize": max(1, min(int(limit or 10), 50)),
        "fields": "files(id,name,mimeType,webViewLink,modifiedTime,driveId,parents)",
        "includeItemsFromAllDrives": True,
        "supportsAllDrives": True,
    }
    if drive_id:
        params.update({"corpora": "drive", "driveId": drive_id})
    else:
        params.update({"corpora": "user"})
    return service.files().list(**params).execute()


def _allowed_drive_ids(service: Any, profile: WorkspaceProfile) -> set[str]:
    ids = {str(v) for v in _as_list(profile.drive.get("allowed_shared_drive_ids"))}
    for entry in _as_list(profile.drive.get("allowed_shared_drives")):
        name = _escape_drive_query(entry)
        try:
            result = service.drives().list(q=f"name = '{name}'", fields="drives(id,name)").execute()
        except Exception as exc:
            raise GoogleWorkspaceError("drive_allowlist_resolution_failed") from exc
        for drive in result.get("drives", []) or []:
            if drive.get("name") == entry and drive.get("id"):
                ids.add(str(drive["id"]))
    return ids


def _require_drive_item_allowed(service: Any, profile: WorkspaceProfile, meta: dict[str, Any]) -> None:
    allowed_folders = set(_as_list(profile.drive.get("allowed_folders")))
    parents = {str(p) for p in meta.get("parents", []) or []}
    if parents & allowed_folders:
        return
    drive_id = str(meta.get("driveId") or "")
    if drive_id and drive_id in _allowed_drive_ids(service, profile):
        return
    raise GoogleWorkspaceError("drive_item_outside_allowlist")


def _validate_bws_ref(secret_ref: str) -> str:
    ref = str(secret_ref or "").strip()
    if not ref:
        raise GoogleWorkspaceError("auth_bws_ref_invalid")
    if any(ch in ref for ch in ("\x00", "\n", "\r")) or ".." in ref or not _BWS_REF_RE.fullmatch(ref):
        raise GoogleWorkspaceError("auth_bws_ref_invalid")
    return ref


def _bws_binary() -> str:
    raw = (_workspace_config().get("bws") or {}).get("path") if isinstance(_workspace_config().get("bws"), dict) else None
    path = str(raw).strip() if raw else shutil.which("bws")
    if not path:
        raise GoogleWorkspaceError("auth_bws_binary_missing")
    resolved = Path(path).expanduser()
    if not resolved.is_absolute() or not resolved.exists() or not os.access(resolved, os.X_OK):
        raise GoogleWorkspaceError("auth_bws_binary_missing")
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise GoogleWorkspaceError("auth_bws_binary_missing") from exc
    if mode & stat.S_IWOTH:
        raise GoogleWorkspaceError("auth_bws_binary_missing")
    return str(resolved)


def _run_bws_json(args: list[str]) -> Any:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=BWS_TIMEOUT_SECONDS, check=False, shell=False)
    except subprocess.TimeoutExpired as exc:
        raise GoogleWorkspaceError("auth_bws_unavailable") from exc
    except OSError as exc:
        raise GoogleWorkspaceError("auth_bws_unavailable") from exc
    if proc.returncode != 0:
        raise GoogleWorkspaceError("auth_bws_unavailable")
    try:
        return json.loads(proc.stdout or "")
    except Exception as exc:
        raise GoogleWorkspaceError("auth_bws_unavailable") from exc


def _extract_bws_secret_value(secret: Any) -> str:
    if not isinstance(secret, dict):
        raise GoogleWorkspaceError("auth_bws_value_empty")
    candidates = [secret.get("value")]
    data = secret.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("value"), data.get("text"), data.get("secret")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise GoogleWorkspaceError("auth_bws_value_empty")


def _bws_secret_matches(secret: Any, selector: str) -> bool:
    if not isinstance(secret, dict):
        return False
    for key in ("id", "key", "name"):
        if str(secret.get(key) or "") == selector:
            return True
    data = secret.get("data")
    if isinstance(data, dict):
        return any(str(data.get(key) or "") == selector for key in ("id", "key", "name"))
    return False


def _get_bws_secret(selector: str) -> dict[str, Any]:
    bws = _bws_binary()
    try:
        secret = _run_bws_json([bws, "secret", "get", selector, "--output", "json"])
        if isinstance(secret, dict):
            return secret
    except GoogleWorkspaceError:
        pass
    secrets = _run_bws_json([bws, "secret", "list", "--output", "json"])
    if not isinstance(secrets, list):
        raise GoogleWorkspaceError("auth_bws_unavailable")
    matches = [item for item in secrets if _bws_secret_matches(item, selector)]
    if not matches:
        raise GoogleWorkspaceError("auth_bws_secret_missing")
    if len(matches) > 1:
        raise GoogleWorkspaceError("auth_bws_secret_ambiguous")
    secret_id = str(matches[0].get("id") or (matches[0].get("data") or {}).get("id") or "")
    if not secret_id:
        raise GoogleWorkspaceError("auth_bws_secret_missing")
    secret = _run_bws_json([bws, "secret", "get", secret_id, "--output", "json"])
    if not isinstance(secret, dict):
        raise GoogleWorkspaceError("auth_bws_unavailable")
    return secret


def _configured_scopes(profile: WorkspaceProfile) -> set[str]:
    scopes = set(_as_list(profile.drive.get("scopes"))) | set(_as_list(profile.gmail.get("scopes")))
    scopes = {s if s.startswith("https://") else f"https://www.googleapis.com/auth/{s}" for s in scopes}
    return scopes or _ALLOWED_GOOGLE_WORKSPACE_SCOPES


def _normalize_credential_scopes(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {s for s in value.replace(",", " ").split() if s}
    if isinstance(value, Iterable):
        return {str(s) for s in value if str(s).strip()}
    return set()


def _validate_google_credential_json(data: Any, profile: WorkspaceProfile) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GoogleWorkspaceError("auth_bws_value_not_json")
    cred_type = data.get("type")
    scopes = _normalize_credential_scopes(data.get("scopes") or data.get("scope"))
    if scopes and not scopes <= (_configured_scopes(profile) & _ALLOWED_GOOGLE_WORKSPACE_SCOPES):
        raise GoogleWorkspaceError("auth_bws_metadata_mismatch")
    if cred_type == "authorized_user":
        required = {"client_id", "client_secret", "refresh_token", "token_uri"}
    elif cred_type == "service_account":
        if _bool_enabled(profile.gmail):
            raise GoogleWorkspaceError("auth_bws_credential_forbidden_for_gmail")
        required = {"project_id", "private_key", "client_email", "token_uri"}
    else:
        raise GoogleWorkspaceError("auth_bws_credential_incomplete")
    if any(not str(data.get(key) or "").strip() for key in required):
        raise GoogleWorkspaceError("auth_bws_credential_incomplete")
    return data


def _materialize_bws_credential(profile: WorkspaceProfile, selector: str, credential: dict[str, Any], metadata: dict[str, Any]) -> Path:
    directory = get_hermes_home() / "secrets" / "google-workspace" / profile.name
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        digest = hashlib.sha256(f"{profile.name}\0{selector}".encode("utf-8")).hexdigest()[:16]
        path = directory / f"credential-{digest}.json"
        meta_path = directory / f"credential-{digest}.meta.json"
        body = json.dumps(credential, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        marker = hashlib.sha256(body.encode("utf-8")).hexdigest()
        old_umask = os.umask(0o177)
        try:
            tmp_path = directory / f".{path.name}.tmp-{os.getpid()}"
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
            os.chmod(path, 0o600)
            meta = {
                "profile": profile.name,
                "scheme": "bws",
                "selector_sha256": hashlib.sha256(selector.encode("utf-8")).hexdigest(),
                "credential_sha256": marker,
                "revision": metadata.get("revisionDate") or metadata.get("updatedAt") or metadata.get("revision") or None,
                "cache_policy": "stable projection; rewritten on each resolution and usable only with mode 0600 under a 0700 directory",
            }
            meta_tmp = directory / f".{meta_path.name}.tmp-{os.getpid()}"
            fd = os.open(meta_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(meta, ensure_ascii=False, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(meta_tmp, meta_path)
            os.chmod(meta_path, 0o600)
        finally:
            os.umask(old_umask)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise GoogleWorkspaceError("auth_materialization_insecure")
        return path
    except GoogleWorkspaceError:
        raise
    except Exception as exc:
        raise GoogleWorkspaceError("auth_materialization_failed") from exc


def _resolve_bws_auth_ref(secret_ref: str, profile: WorkspaceProfile) -> Path:
    selector = _validate_bws_ref(secret_ref)
    secret = _get_bws_secret(selector)
    value = _extract_bws_secret_value(secret)
    if value.startswith("encrypted:"):
        raise GoogleWorkspaceError("auth_bws_value_not_json")
    try:
        data = json.loads(value)
    except Exception as exc:
        raise GoogleWorkspaceError("auth_bws_value_not_json") from exc
    credential = _validate_google_credential_json(data, profile)
    return _materialize_bws_credential(profile, selector, credential, secret)


def _resolve_secret_path(auth_ref: str, profile: WorkspaceProfile | None = None) -> Path:
    """Resolve a non-secret auth reference to a local credential/token file path."""
    ref = auth_ref.strip()
    if ref.startswith("bws:"):
        if profile is None:
            raise GoogleWorkspaceError("auth_bws_ref_invalid")
        return _resolve_bws_auth_ref(ref.split(":", 1)[1], profile)
    if ref.startswith("env:"):
        env_name = ref.split(":", 1)[1].strip()
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise GoogleWorkspaceError("auth_env_missing")
        return Path(value).expanduser()
    if ref.startswith("file:"):
        return Path(ref.split(":", 1)[1].strip()).expanduser()
    if ref.startswith("secure-store:"):
        env_name = "GOOGLE_WORKSPACE_AUTH_" + ref.split(":", 1)[1].upper().replace("-", "_")
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise GoogleWorkspaceError("secure_store_ref_unresolved")
        return Path(value).expanduser()
    return Path(ref).expanduser()


def _google_credentials(profile: WorkspaceProfile, scopes: list[str]) -> Any:
    path = _resolve_secret_path(profile.auth_ref, profile)
    if not path.exists():
        raise GoogleWorkspaceError("auth_file_missing")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("type") == "service_account":
                from google.oauth2 import service_account

                return service_account.Credentials.from_service_account_file(str(path), scopes=scopes)
            from google.oauth2.credentials import Credentials

            return Credentials.from_authorized_user_file(str(path), scopes=scopes)
        raise GoogleWorkspaceError("auth_file_must_be_json")
    except GoogleWorkspaceError:
        raise
    except ImportError as exc:
        raise GoogleWorkspaceError("google_auth_libraries_missing") from exc
    except Exception as exc:
        raise GoogleWorkspaceError("auth_load_failed") from exc


def _build_google_service(api: str, version: str, profile: WorkspaceProfile, scopes: list[str]) -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleWorkspaceError("google_api_client_missing") from exc
    credentials = _google_credentials(profile, scopes)
    return build(api, version, credentials=credentials, cache_discovery=False)


def _cache_path() -> Path:
    raw = _workspace_config().get("cache") or {}
    configured = raw.get("path") if isinstance(raw, dict) else None
    if configured:
        return Path(str(configured)).expanduser()
    return get_hermes_home() / "google-workspace" / "cache.sqlite"


def _cache_conn() -> sqlite3.Connection:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS drive_files ("
        "profile TEXT NOT NULL, file_id TEXT NOT NULL, name TEXT, mime_type TEXT, web_url TEXT, "
        "modified_time TEXT, folder_path TEXT, text_excerpt TEXT, indexed_at INTEGER NOT NULL, "
        "PRIMARY KEY(profile, file_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gmail_messages ("
        "profile TEXT NOT NULL, mailbox TEXT NOT NULL, message_id TEXT NOT NULL, thread_id TEXT, "
        "sender TEXT, recipients TEXT, subject TEXT, date TEXT, snippet TEXT, body_text TEXT, "
        "indexed_at INTEGER NOT NULL, PRIMARY KEY(profile, mailbox, message_id))"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS drive_files_fts USING fts5(profile UNINDEXED, file_id UNINDEXED, name, text_excerpt)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS gmail_messages_fts USING fts5(profile UNINDEXED, mailbox UNINDEXED, message_id UNINDEXED, subject, snippet, body_text)"
    )
    return conn


def _audit(operation: str, profile: str, surface: str, **details: Any) -> None:
    audit_cfg = _workspace_config().get("audit") or {}
    if isinstance(audit_cfg, dict) and audit_cfg.get("log_reads", True) is False:
        return
    configured_path = audit_cfg.get("path") if isinstance(audit_cfg, dict) else None
    if configured_path:
        path = Path(str(configured_path)).expanduser()
    else:
        path = get_hermes_home() / "google-workspace" / "audit.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_details = {k: _mask(v) for k, v in details.items()}
    entry = {
        "ts": int(time.time()),
        "profile": profile,
        "surface": surface,
        "operation": operation,
        "details": safe_details,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _mask(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if len(text) <= 8:
        return text
    return f"{text[:4]}…{text[-4:]}"


def _decode_gmail_data(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _gmail_body_text(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    text = _decode_gmail_data(body.get("data"))
    parts = payload.get("parts") if isinstance(payload.get("parts"), list) else []
    for part in parts:
        text_part = _gmail_body_text(part)
        if text_part:
            text += ("\n" if text else "") + text_part
    return text


def google_workspace_profiles() -> str:
    profiles = []
    for name, raw in sorted(_profiles_config().items()):
        if not isinstance(raw, dict):
            continue
        profiles.append(
            {
                "name": name,
                "display_name": raw.get("display_name") or name,
                "mode": raw.get("mode") or "readonly",
                "drive_enabled": bool((raw.get("drive") or {}).get("enabled")) if isinstance(raw.get("drive"), dict) else False,
                "gmail_enabled": bool((raw.get("gmail") or {}).get("enabled")) if isinstance(raw.get("gmail"), dict) else False,
            }
        )
    return _json_ok({"profiles": profiles})


def google_drive_search(profile: str | None, query: str, folder: str | None = None, limit: int = 10) -> str:
    try:
        p = _require_profile(profile)
        _require_drive_policy(p, folder=folder)
        service = _build_google_service("drive", "v3", p, [READONLY_DRIVE_SCOPE])
        q = "trashed = false"
        if query:
            query_text = str(query)
            if query_text.strip().startswith("modifiedTime >"):
                q += f" and {query_text.strip()}"
            else:
                escaped = _escape_drive_query(query_text)
                q += f" and fullText contains '{escaped}'"
        if folder:
            q += f" and '{_escape_drive_query(folder)}' in parents"
        max_results = max(1, min(int(limit or 10), 50))
        drive_ids = _allowed_drive_ids(service, p) if not folder else set()
        allowed_drives = _as_list(p.drive.get("allowed_shared_drives")) + _as_list(p.drive.get("allowed_shared_drive_ids"))
        allowed_folders = _as_list(p.drive.get("allowed_folders"))
        files: list[dict[str, Any]] = []
        if drive_ids:
            for drive_id in sorted(drive_ids):
                result = _drive_list_call(service, q=q, limit=max_results, drive_id=drive_id)
                files.extend(result.get("files", []) or [])
                if len(files) >= max_results:
                    files = files[:max_results]
                    break
        elif folder:
            result = _drive_list_call(service, q=q, limit=max_results)
            files = result.get("files", []) or []
        else:
            raise GoogleWorkspaceError("drive_allowlist_resolution_empty")
        now = int(time.time())
        with _cache_conn() as conn:
            for item in files:
                conn.execute(
                    "INSERT OR REPLACE INTO drive_files(profile,file_id,name,mime_type,web_url,modified_time,folder_path,text_excerpt,indexed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (p.name, item.get("id"), item.get("name"), item.get("mimeType"), item.get("webViewLink"), item.get("modifiedTime"), folder, None, now),
                )
        _audit("search", p.name, "drive", query=query, folder=folder, count=len(files))
        return _json_ok({"profile": p.name, "files": files})
    except GoogleWorkspaceError as exc:
        return _json_error(str(exc), reason="policy_or_prerequisite_failure", profile=profile)


def google_drive_read(profile: str | None, file_id: str) -> str:
    try:
        p = _require_profile(profile)
        _require_drive_policy(p)
        _validate_safe_identifier(file_id, "file_id")
        service = _build_google_service("drive", "v3", p, [READONLY_DRIVE_SCOPE])
        meta = service.files().get(fileId=file_id, fields="id,name,mimeType,webViewLink,modifiedTime,driveId,parents", supportsAllDrives=True).execute()
        _require_drive_item_allowed(service, p, meta)
        mime_type = meta.get("mimeType") or ""
        text = ""
        if mime_type.startswith("application/vnd.google-apps."):
            export_mime = "text/plain"
            if mime_type == "application/vnd.google-apps.spreadsheet":
                export_mime = "text/csv"
            elif mime_type == "application/vnd.google-apps.presentation":
                export_mime = "text/plain"
            elif mime_type == "application/vnd.google-apps.document":
                export_mime = "text/plain"
            content = service.files().export(fileId=file_id, mimeType=export_mime).execute()
            text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
        else:
            content = service.files().get_media(fileId=file_id).execute()
            if isinstance(content, bytes):
                text = content.decode("utf-8", errors="replace")
            else:
                text = str(content)
        now = int(time.time())
        with _cache_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO drive_files(profile,file_id,name,mime_type,web_url,modified_time,folder_path,text_excerpt,indexed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (p.name, file_id, meta.get("name"), mime_type, meta.get("webViewLink"), meta.get("modifiedTime"), None, text[:4000], now),
            )
        _audit("read", p.name, "drive", file_id=file_id)
        return _json_ok({"profile": p.name, "metadata": meta, "text": text})
    except GoogleWorkspaceError as exc:
        return _json_error(str(exc), reason="policy_or_prerequisite_failure", profile=profile, file_id=_mask(file_id))
    except Exception as exc:
        return _json_error("drive_read_failed", reason="google_api_failure", profile=profile, file_id=_mask(file_id), detail=type(exc).__name__)


def google_drive_recent(profile: str | None, days: int = 7, folder: str | None = None, limit: int = 10) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days or 7)))
    query = f"modifiedTime > '{cutoff.isoformat().replace('+00:00', 'Z')}'"
    return google_drive_search(profile=profile, query=query, folder=folder, limit=limit)


def google_mail_search(profile: str | None, query: str, mailbox: str, after: str | None = None, before: str | None = None, limit: int = 10) -> str:
    try:
        p = _require_profile(profile)
        _require_gmail_policy(p, mailbox=mailbox)
        q = str(query or "")
        if after:
            q += f" after:{after}"
        if before:
            q += f" before:{before}"
        service = _build_google_service("gmail", "v1", p, [READONLY_GMAIL_SCOPE])
        result = service.users().messages().list(userId=mailbox, q=q.strip(), maxResults=max(1, min(int(limit or 10), 50))).execute()
        messages = result.get("messages", [])
        _audit("search", p.name, "gmail", mailbox=mailbox, query=query, count=len(messages))
        return _json_ok({"profile": p.name, "mailbox": mailbox, "messages": messages})
    except GoogleWorkspaceError as exc:
        return _json_error(str(exc), reason="policy_or_prerequisite_failure", profile=profile, mailbox=_mask(mailbox))


def google_mail_read(profile: str | None, message_id: str, mailbox: str) -> str:
    try:
        p = _require_profile(profile)
        _require_gmail_policy(p, mailbox=mailbox)
        _validate_safe_identifier(message_id, "message_id")
        service = _build_google_service("gmail", "v1", p, [READONLY_GMAIL_SCOPE])
        msg = service.users().messages().get(userId=mailbox, id=message_id, format="full").execute()
        headers = {h.get("name", "").lower(): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])}
        body_text = _gmail_body_text(msg.get("payload"))
        now = int(time.time())
        with _cache_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gmail_messages(profile,mailbox,message_id,thread_id,sender,recipients,subject,date,snippet,body_text,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (p.name, mailbox, message_id, msg.get("threadId"), headers.get("from"), headers.get("to"), headers.get("subject"), headers.get("date"), msg.get("snippet"), body_text[:4000], now),
            )
        _audit("read", p.name, "gmail", mailbox=mailbox, message_id=message_id)
        return _json_ok({"profile": p.name, "mailbox": mailbox, "message": msg, "body_text": body_text})
    except GoogleWorkspaceError as exc:
        return _json_error(str(exc), reason="policy_or_prerequisite_failure", profile=profile, mailbox=_mask(mailbox), message_id=_mask(message_id))


def google_mail_thread(profile: str | None, thread_id: str, mailbox: str) -> str:
    try:
        p = _require_profile(profile)
        _require_gmail_policy(p, mailbox=mailbox)
        _validate_safe_identifier(thread_id, "thread_id")
        service = _build_google_service("gmail", "v1", p, [READONLY_GMAIL_SCOPE])
        thread = service.users().threads().get(userId=mailbox, id=thread_id, format="full").execute()
        _audit("read_thread", p.name, "gmail", mailbox=mailbox, thread_id=thread_id)
        return _json_ok({"profile": p.name, "mailbox": mailbox, "thread": thread})
    except GoogleWorkspaceError as exc:
        return _json_error(str(exc), reason="policy_or_prerequisite_failure", profile=profile, mailbox=_mask(mailbox), thread_id=_mask(thread_id))


SCHEMAS = {
    "google_workspace_profiles": {
        "name": "google_workspace_profiles",
        "description": "List configured Google Workspace profiles and enabled surfaces.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "google_drive_search": {
        "name": "google_drive_search",
        "description": "Search Google Drive for a configured read-only profile. Fails closed on missing profile or allowlist violations.",
        "parameters": {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Configured Google Workspace profile, e.g. whystarve."},
                "query": {"type": "string", "description": "Drive full-text query."},
                "folder": {"type": "string", "description": "Optional allowed folder name."},
                "limit": {"type": "integer", "description": "Maximum results, capped at 50."},
            },
            "required": ["profile", "query"],
        },
    },
    "google_drive_read": {
        "name": "google_drive_read",
        "description": "Read/export one Google Drive file for a configured read-only profile.",
        "parameters": {"type": "object", "properties": {"profile": {"type": "string"}, "file_id": {"type": "string"}}, "required": ["profile", "file_id"]},
    },
    "google_drive_recent": {
        "name": "google_drive_recent",
        "description": "List recent Google Drive files for a configured read-only profile.",
        "parameters": {"type": "object", "properties": {"profile": {"type": "string"}, "days": {"type": "integer"}, "folder": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["profile"]},
    },
    "google_mail_search": {
        "name": "google_mail_search",
        "description": "Search Gmail messages in an allowed mailbox for a configured read-only profile.",
        "parameters": {"type": "object", "properties": {"profile": {"type": "string"}, "query": {"type": "string"}, "mailbox": {"type": "string"}, "after": {"type": "string"}, "before": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["profile", "query", "mailbox"]},
    },
    "google_mail_read": {
        "name": "google_mail_read",
        "description": "Read one Gmail message in an allowed mailbox for a configured read-only profile.",
        "parameters": {"type": "object", "properties": {"profile": {"type": "string"}, "message_id": {"type": "string"}, "mailbox": {"type": "string"}}, "required": ["profile", "message_id", "mailbox"]},
    },
    "google_mail_thread": {
        "name": "google_mail_thread",
        "description": "Read one Gmail thread in an allowed mailbox for a configured read-only profile.",
        "parameters": {"type": "object", "properties": {"profile": {"type": "string"}, "thread_id": {"type": "string"}, "mailbox": {"type": "string"}}, "required": ["profile", "thread_id", "mailbox"]},
    },
}


def _handler(fn: Any) -> Any:
    def inner(args: dict[str, Any], **_: Any) -> str:
        return fn(**args)

    return inner


registry.register(
    name="google_workspace_profiles",
    toolset="google_workspace",
    schema=SCHEMAS["google_workspace_profiles"],
    handler=_handler(google_workspace_profiles),
    check_fn=check_google_workspace_requirements,
    description=SCHEMAS["google_workspace_profiles"]["description"],
    emoji="🗂️",
    max_result_size_chars=30000,
)
registry.register(
    name="google_drive_search",
    toolset="google_workspace",
    schema=SCHEMAS["google_drive_search"],
    handler=_handler(google_drive_search),
    check_fn=check_google_workspace_requirements,
    description=SCHEMAS["google_drive_search"]["description"],
    emoji="🗂️",
    max_result_size_chars=30000,
)
registry.register(
    name="google_drive_read",
    toolset="google_workspace",
    schema=SCHEMAS["google_drive_read"],
    handler=_handler(google_drive_read),
    check_fn=check_google_workspace_requirements,
    description=SCHEMAS["google_drive_read"]["description"],
    emoji="🗂️",
    max_result_size_chars=30000,
)
registry.register(
    name="google_drive_recent",
    toolset="google_workspace",
    schema=SCHEMAS["google_drive_recent"],
    handler=_handler(google_drive_recent),
    check_fn=check_google_workspace_requirements,
    description=SCHEMAS["google_drive_recent"]["description"],
    emoji="🗂️",
    max_result_size_chars=30000,
)
registry.register(
    name="google_mail_search",
    toolset="google_workspace",
    schema=SCHEMAS["google_mail_search"],
    handler=_handler(google_mail_search),
    check_fn=check_google_workspace_requirements,
    description=SCHEMAS["google_mail_search"]["description"],
    emoji="🗂️",
    max_result_size_chars=30000,
)
registry.register(
    name="google_mail_read",
    toolset="google_workspace",
    schema=SCHEMAS["google_mail_read"],
    handler=_handler(google_mail_read),
    check_fn=check_google_workspace_requirements,
    description=SCHEMAS["google_mail_read"]["description"],
    emoji="🗂️",
    max_result_size_chars=30000,
)
registry.register(
    name="google_mail_thread",
    toolset="google_workspace",
    schema=SCHEMAS["google_mail_thread"],
    handler=_handler(google_mail_thread),
    check_fn=check_google_workspace_requirements,
    description=SCHEMAS["google_mail_thread"]["description"],
    emoji="🗂️",
    max_result_size_chars=30000,
)
