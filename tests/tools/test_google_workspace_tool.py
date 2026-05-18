import base64
import json
import sqlite3

import pytest

from tools import google_workspace_tool as gw


BASE_PROFILES = {
    "whystarve": {
        "display_name": "Whystarve",
        "auth_ref": "env:WS_GOOGLE_AUTH_JSON",
        "mode": "readonly",
        "drive": {
            "enabled": True,
            "allowed_shared_drives": ["Whystarve"],
            "allowed_folders": ["folder-ok"],
        },
        "gmail": {
            "enabled": True,
            "allowed_mailboxes": ["me"],
            "forbidden_scopes": ["gmail.send", "gmail.modify", "gmail.compose"],
        },
    },
    "dummy": {
        "display_name": "Dummy",
        "auth_ref": "file:/tmp/dummy-google.json",
        "mode": "readonly",
        "drive": {"enabled": True, "allowed_folders": ["dummy-folder"]},
        "gmail": {"enabled": True, "allowed_mailboxes": ["dummy@example.com"]},
    },
}


def install_profiles(monkeypatch, profiles=None):
    monkeypatch.setattr(gw, "_profiles_config", lambda: profiles or BASE_PROFILES)


def test_profiles_lists_generic_profile_surface(monkeypatch):
    install_profiles(monkeypatch)
    payload = json.loads(gw.google_workspace_profiles())
    assert payload["ok"] is True
    assert [p["name"] for p in payload["profiles"]] == ["dummy", "whystarve"]
    assert payload["profiles"][1]["drive_enabled"] is True
    assert payload["profiles"][1]["gmail_enabled"] is True


@pytest.mark.parametrize(
    ("profile", "error"),
    [(None, "profile_required"), ("", "profile_required"), ("../bad", "invalid_profile_name"), ("missing", "unknown_profile")],
)
def test_unknown_or_invalid_profile_fails_closed(monkeypatch, profile, error):
    install_profiles(monkeypatch)
    payload = json.loads(gw.google_drive_search(profile, "roadmap"))
    assert payload["ok"] is False
    assert payload["error"] == error


def test_gmail_mailbox_allowlist_fails_closed_before_client(monkeypatch):
    install_profiles(monkeypatch)
    monkeypatch.setattr(gw, "_build_google_service", lambda *a, **k: pytest.fail("client should not be built"))
    payload = json.loads(gw.google_mail_search("whystarve", "invoice", mailbox="other@example.com"))
    assert payload["ok"] is False
    assert payload["error"] == "gmail_mailbox_forbidden"


def test_drive_folder_allowlist_fails_closed_before_client(monkeypatch):
    install_profiles(monkeypatch)
    monkeypatch.setattr(gw, "_build_google_service", lambda *a, **k: pytest.fail("client should not be built"))
    payload = json.loads(gw.google_drive_search("whystarve", "deck", folder="other-folder"))
    assert payload["ok"] is False
    assert payload["error"] == "drive_folder_forbidden"


def test_forbidden_active_scope_config_fails_closed(monkeypatch):
    profiles = {
        "bad": {
            **BASE_PROFILES["whystarve"],
            "gmail": {"enabled": True, "allowed_mailboxes": ["me"], "scopes": ["gmail.readonly", "gmail.modify"]},
        }
    }
    install_profiles(monkeypatch, profiles)
    payload = json.loads(gw.google_mail_search("bad", "x", mailbox="me"))
    assert payload["ok"] is False
    assert payload["error"] == "gmail_forbidden_scope_configured"


def test_cache_schema_is_profile_namespaced(tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "_workspace_config", lambda: {"cache": {"path": str(tmp_path / "gw.sqlite")}})
    with gw._cache_conn() as conn:
        drive_pk = conn.execute("PRAGMA table_info(drive_files)").fetchall()
        gmail_pk = conn.execute("PRAGMA table_info(gmail_messages)").fetchall()
        drive_pk_cols = [row[1] for row in drive_pk if row[5] > 0]
        gmail_pk_cols = [row[1] for row in gmail_pk if row[5] > 0]
    assert drive_pk_cols == ["profile", "file_id"]
    assert gmail_pk_cols == ["profile", "mailbox", "message_id"]


def test_gmail_body_decoding_extracts_nested_plain_text():
    encoded = base64.urlsafe_b64encode("hello 선생님".encode()).decode().rstrip("=")
    payload = {"parts": [{"mimeType": "text/plain", "body": {"data": encoded}}]}
    assert gw._gmail_body_text(payload) == "hello 선생님"


class _Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Files:
    def get(self, **kwargs):
        return _Exec({"id": kwargs["fileId"], "name": "Doc", "mimeType": "text/plain", "parents": ["folder-ok"]})

    def get_media(self, **kwargs):
        return _Exec(b"drive text")


class _FakeDriveService:
    def files(self):
        return _Files()


def test_drive_read_checks_item_allowlist_and_caches(monkeypatch, tmp_path):
    install_profiles(monkeypatch)
    monkeypatch.setattr(gw, "_workspace_config", lambda: {"cache": {"path": str(tmp_path / "gw.sqlite")}})
    monkeypatch.setattr(gw, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_build_google_service", lambda *a, **k: _FakeDriveService())
    payload = json.loads(gw.google_drive_read("whystarve", "file123"))
    assert payload["ok"] is True
    assert payload["text"] == "drive text"
    with sqlite3.connect(tmp_path / "gw.sqlite") as conn:
        rows = conn.execute("select profile, file_id, text_excerpt from drive_files").fetchall()
    assert rows == [("whystarve", "file123", "drive text")]


def test_google_workspace_toolset_is_static_and_resolves():
    from toolsets import TOOLSETS, resolve_toolset

    assert "google_workspace" in TOOLSETS
    tools = set(resolve_toolset("google_workspace"))
    assert {
        "google_workspace_profiles",
        "google_drive_search",
        "google_drive_read",
        "google_drive_recent",
        "google_mail_search",
        "google_mail_read",
        "google_mail_thread",
    } <= tools
