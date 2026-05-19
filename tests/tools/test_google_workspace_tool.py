import base64
import json
import sqlite3
import stat
import subprocess

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


@pytest.mark.parametrize("scope", ["gmail.modify", "https://mail.google.com/"])
def test_forbidden_active_scope_config_fails_closed(monkeypatch, scope):
    profiles = {
        "bad": {
            **BASE_PROFILES["whystarve"],
            "gmail": {"enabled": True, "allowed_mailboxes": ["me"], "scopes": ["gmail.readonly", scope]},
        }
    }
    install_profiles(monkeypatch, profiles)
    monkeypatch.setattr(gw, "_build_google_service", lambda *a, **k: pytest.fail("client/BWS should not be reached"))
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


def _profile(auth_ref="bws:GOOGLE_WORKSPACE_TEST_AUTHORIZED_USER_JSON", gmail_enabled=True):
    return gw.WorkspaceProfile(
        name="testprofile",
        display_name="Test Profile",
        auth_ref=auth_ref,
        mode="readonly",
        drive={"enabled": True, "allowed_folders": ["folder-ok"], "scopes": [gw.READONLY_DRIVE_SCOPE]},
        gmail={"enabled": gmail_enabled, "allowed_mailboxes": ["me"], "scopes": [gw.READONLY_GMAIL_SCOPE]} if gmail_enabled else {},
    )


def _authorized_user_json(**extra):
    data = {
        "type": "authorized_user",
        "client_id": "client-id.apps.googleusercontent.com",
        "client_secret": "super-secret-client-secret",
        "refresh_token": "super-secret-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    data.update(extra)
    return data


def test_env_and_file_auth_refs_still_resolve_unchanged(monkeypatch, tmp_path):
    credential = tmp_path / "google.json"
    monkeypatch.setenv("WS_GOOGLE_AUTH_JSON", str(credential))
    assert gw._resolve_secret_path("env:WS_GOOGLE_AUTH_JSON") == credential
    assert gw._resolve_secret_path(f"file:{credential}") == credential


@pytest.mark.parametrize("auth_ref", ["bws:", "bws:   ", "bws:../secret", "bws:bad;secret", "bws:bad\nsecret"])
def test_invalid_bws_auth_ref_fails_closed(auth_ref):
    with pytest.raises(gw.GoogleWorkspaceError) as exc:
        gw._resolve_secret_path(auth_ref, _profile(auth_ref))
    assert str(exc.value) == "auth_bws_ref_invalid"


def test_missing_bws_binary_fails_closed(monkeypatch):
    monkeypatch.setattr(gw, "_workspace_config", lambda: {})
    monkeypatch.setattr(gw.shutil, "which", lambda name: None)
    with pytest.raises(gw.GoogleWorkspaceError) as exc:
        gw._resolve_secret_path("bws:GOOGLE_WORKSPACE_TEST_AUTHORIZED_USER_JSON", _profile())
    assert str(exc.value) == "auth_bws_binary_missing"


def test_bws_subprocess_uses_argv_without_shell_and_materializes_0600(monkeypatch, tmp_path):
    profile = _profile()
    credential = _authorized_user_json(scopes=[gw.READONLY_DRIVE_SCOPE, gw.READONLY_GMAIL_SCOPE])
    calls = []

    class Proc:
        returncode = 0
        stdout = json.dumps({"id": "secret-id", "key": "GOOGLE_WORKSPACE_TEST_AUTHORIZED_USER_JSON", "value": json.dumps(credential), "revisionDate": "2026-05-19T00:00:00Z"})
        stderr = "client_secret=should-not-leak"

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Proc()

    monkeypatch.setattr(gw, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gw, "_bws_binary", lambda: "/usr/bin/bws")
    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    path = gw._resolve_secret_path("bws:GOOGLE_WORKSPACE_TEST_AUTHORIZED_USER_JSON", profile)
    assert calls == [(["/usr/bin/bws", "secret", "get", "GOOGLE_WORKSPACE_TEST_AUTHORIZED_USER_JSON", "--output", "json"], {"capture_output": True, "text": True, "timeout": gw.BWS_TIMEOUT_SECONDS, "check": False, "shell": False})]
    assert path.parent == tmp_path / "secrets" / "google-workspace" / "testprofile"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["refresh_token"] == "super-secret-refresh-token"
    meta = path.with_name(path.name.replace(".json", ".meta.json"))
    assert stat.S_IMODE(meta.stat().st_mode) == 0o600
    assert "GOOGLE_WORKSPACE_TEST_AUTHORIZED_USER_JSON" not in meta.read_text()


@pytest.mark.parametrize(
    ("proc", "error"),
    [
        (subprocess.TimeoutExpired(cmd=["bws"], timeout=1), "auth_bws_unavailable"),
        ({"returncode": 1, "stdout": "", "stderr": "client_secret=raw-secret"}, "auth_bws_unavailable"),
        ({"returncode": 0, "stdout": "not-json", "stderr": "refresh_token=raw-secret"}, "auth_bws_unavailable"),
    ],
)
def test_bws_subprocess_failures_are_redacted(monkeypatch, proc, error):
    def fake_run(*args, **kwargs):
        if isinstance(proc, Exception):
            raise proc
        return type("Proc", (), proc)()

    monkeypatch.setattr(gw, "_bws_binary", lambda: "/usr/bin/bws")
    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    with pytest.raises(gw.GoogleWorkspaceError) as exc:
        gw._resolve_secret_path("bws:GOOGLE_WORKSPACE_TEST_AUTHORIZED_USER_JSON", _profile())
    assert str(exc.value) == error
    assert "raw-secret" not in str(exc.value)


def test_bws_missing_secret_fails_closed_after_metadata_lookup(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[2] == "get":
            return type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "not found with raw-secret"})()
        return type("Proc", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    monkeypatch.setattr(gw, "_bws_binary", lambda: "/usr/bin/bws")
    monkeypatch.setattr(gw.subprocess, "run", fake_run)
    with pytest.raises(gw.GoogleWorkspaceError) as exc:
        gw._resolve_secret_path("bws:GOOGLE_WORKSPACE_MISSING_JSON", _profile())
    assert str(exc.value) == "auth_bws_secret_missing"
    assert calls == [
        ["/usr/bin/bws", "secret", "get", "GOOGLE_WORKSPACE_MISSING_JSON", "--output", "json"],
        ["/usr/bin/bws", "secret", "list", "--output", "json"],
    ]


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("encrypted:placeholder", "auth_bws_value_not_json"),
        ("not-json", "auth_bws_value_not_json"),
        (json.dumps({"type": "authorized_user", "client_id": "only"}), "auth_bws_credential_incomplete"),
        (json.dumps({**_authorized_user_json(), "scopes": ["https://mail.google.com/"]}), "auth_bws_metadata_mismatch"),
    ],
)
def test_bws_malformed_or_forbidden_values_fail_closed(monkeypatch, value, error):
    monkeypatch.setattr(gw, "_get_bws_secret", lambda selector: {"id": "secret-id", "value": value})
    with pytest.raises(gw.GoogleWorkspaceError) as exc:
        gw._resolve_secret_path("bws:GOOGLE_WORKSPACE_TEST_AUTHORIZED_USER_JSON", _profile())
    assert str(exc.value) == error


def test_bws_service_account_rejected_for_gmail_profile(monkeypatch):
    service_account = {
        "type": "service_account",
        "project_id": "proj",
        "private_key": "fixture-private-key-value",
        "client_email": "svc@example.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    monkeypatch.setattr(gw, "_get_bws_secret", lambda selector: {"id": "secret-id", "value": json.dumps(service_account)})
    with pytest.raises(gw.GoogleWorkspaceError) as exc:
        gw._resolve_secret_path("bws:GOOGLE_WORKSPACE_TEST_SERVICE_ACCOUNT_JSON", _profile())
    assert str(exc.value) == "auth_bws_credential_forbidden_for_gmail"


class _Exec:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Files:
    def list(self, **kwargs):
        return _Exec({"files": [{"id": "fallback", "name": "Should Not Leak"}]})

    def get(self, **kwargs):
        return _Exec({"id": kwargs["fileId"], "name": "Doc", "mimeType": "text/plain", "parents": ["folder-ok"]})

    def get_media(self, **kwargs):
        return _Exec(b"drive text")

    def export(self, **kwargs):
        return _Exec(b"sheet text")


class _SpreadsheetFiles(_Files):
    def get(self, **kwargs):
        return _Exec({"id": kwargs["fileId"], "name": "Sheet", "mimeType": "application/vnd.google-apps.spreadsheet", "parents": ["folder-ok"]})

    def export(self, **kwargs):
        assert kwargs["mimeType"] == "text/csv"
        return _Exec(b"a,b\n1,2")


class _Drives:
    def list(self, **kwargs):
        return _Exec({"drives": []})


class _FakeDriveService:
    def __init__(self, files=None):
        self._files = files or _Files()

    def files(self):
        return self._files

    def drives(self):
        return _Drives()


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


def test_drive_read_exports_spreadsheets_as_csv(monkeypatch, tmp_path):
    install_profiles(monkeypatch)
    monkeypatch.setattr(gw, "_workspace_config", lambda: {"cache": {"path": str(tmp_path / "gw.sqlite")}})
    monkeypatch.setattr(gw, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_build_google_service", lambda *a, **k: _FakeDriveService(_SpreadsheetFiles()))
    payload = json.loads(gw.google_drive_read("whystarve", "sheet123"))
    assert payload["ok"] is True
    assert payload["text"] == "a,b\n1,2"


def test_drive_search_fails_closed_when_named_shared_drive_resolves_empty(monkeypatch):
    install_profiles(monkeypatch)
    monkeypatch.setattr(gw, "_build_google_service", lambda *a, **k: _FakeDriveService())
    payload = json.loads(gw.google_drive_search("whystarve", "deck"))
    assert payload["ok"] is False
    assert payload["error"] == "drive_allowlist_resolution_empty"


def test_audit_honors_configured_path_and_masks_details(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit" / "google-workspace.log"
    monkeypatch.setattr(gw, "_workspace_config", lambda: {"audit": {"path": str(audit_path)}})
    gw._audit("search", "whystarve", "drive", query="sensitive-query-value")
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert payload["profile"] == "whystarve"
    assert payload["surface"] == "drive"
    assert payload["operation"] == "search"
    assert payload["details"] == {"query": "sens…alue"}


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
