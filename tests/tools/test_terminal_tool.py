"""Focused regression tests for terminal tool safety behavior."""

import json

import tools.terminal_tool as terminal_tool


def setup_function():
    terminal_tool._reset_cached_sudo_passwords()


def teardown_function():
    terminal_tool._reset_cached_sudo_passwords()


def test_safe_getcwd_returns_process_cwd_when_available(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal_tool.os, "getcwd", lambda: str(tmp_path))

    assert terminal_tool._safe_getcwd() == str(tmp_path)


def test_searching_for_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "rg --line-number --no-heading --with-filename 'sudo' . | head -n 20"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_printf_literal_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "printf '%s\\n' sudo"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_non_command_argument_named_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "grep -n sudo README.md"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_actual_sudo_command_uses_configured_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo apt install -y ripgrep")

    assert transformed == "sudo -S -p '' apt install -y ripgrep"
    assert sudo_stdin == "testpass\n"


def test_actual_sudo_after_leading_env_assignment_is_rewritten(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("DEBUG=1 sudo whoami")

    assert transformed == "DEBUG=1 sudo -S -p '' whoami"
    assert sudo_stdin == "testpass\n"


def test_explicit_empty_sudo_password_tries_empty_without_prompt(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("interactive sudo prompt should not run for explicit empty password")

    monkeypatch.setattr(terminal_tool, "_prompt_for_sudo_password", _fail_prompt)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo true")

    assert transformed == "sudo -S -p '' true"
    assert sudo_stdin == "\n"


def test_cached_sudo_password_is_used_when_env_is_unset(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool._set_cached_sudo_password("cached-pass")

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("echo ok && sudo whoami")

    assert transformed == "echo ok && sudo -S -p '' whoami"
    assert sudo_stdin == "cached-pass\n"


def test_cached_sudo_password_isolated_by_session_key(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-a")
    terminal_tool._set_cached_sudo_password("alpha-pass")

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-b")
    assert terminal_tool._get_cached_sudo_password() == ""

    monkeypatch.setenv("HERMES_SESSION_KEY", "session-a")
    assert terminal_tool._get_cached_sudo_password() == "alpha-pass"


def test_passwordless_sudo_skips_interactive_prompt_and_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError(
            "interactive sudo prompt should not run when sudo -n already works"
        )

    monkeypatch.setattr(terminal_tool, "_prompt_for_sudo_password", _fail_prompt)
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: True, raising=False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo whoami")

    assert transformed == "sudo whoami"
    assert sudo_stdin is None


def test_passwordless_sudo_probe_rechecks_local_terminal(monkeypatch):
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Result(0 if len(calls) == 1 else 1)

    monkeypatch.setattr(terminal_tool.subprocess, "run", fake_run)

    assert terminal_tool._sudo_nopasswd_works() is True
    assert terminal_tool._sudo_nopasswd_works() is False
    assert len(calls) == 2
    assert calls[0][0] == ["sudo", "-n", "true"]
    assert calls[1][0] == ["sudo", "-n", "true"]


def test_passwordless_sudo_probe_is_disabled_for_nonlocal_terminal_env(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")

    def _fail_run(*_args, **_kwargs):
        raise AssertionError("host sudo probe must not run for non-local terminal envs")

    monkeypatch.setattr(terminal_tool.subprocess, "run", _fail_run)

    assert terminal_tool._sudo_nopasswd_works() is False


def test_validate_workdir_allows_windows_drive_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project") is None
    assert terminal_tool._validate_workdir("C:/Users/Alice/project") is None


def test_validate_workdir_allows_windows_unc_paths():
    assert terminal_tool._validate_workdir(r"\\server\share\project") is None


def test_validate_workdir_blocks_shell_metacharacters_in_windows_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project; rm -rf /")
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project$(whoami)")
    assert terminal_tool._validate_workdir("C:\\Users\\Alice\\project\nwhoami")


def test_resolve_start_cwd_uses_requested_workdir_when_process_cwd_deleted(monkeypatch, tmp_path):
    requested_workdir = tmp_path / "requested"
    requested_workdir.mkdir()
    missing_terminal_cwd = tmp_path / "missing-terminal-cwd"

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(missing_terminal_cwd))
    monkeypatch.setattr(
        terminal_tool.os,
        "getcwd",
        lambda: (_ for _ in ()).throw(FileNotFoundError("cwd deleted")),
    )

    assert terminal_tool._resolve_start_cwd(str(requested_workdir)) == str(requested_workdir)
    config = terminal_tool._get_env_config(requested_workdir=str(requested_workdir))

    assert config["cwd"] == str(requested_workdir)


def test_get_env_config_ignores_missing_terminal_cwd_without_crashing(monkeypatch, tmp_path):
    fallback_home = tmp_path / "home"
    fallback_home.mkdir()
    missing_terminal_cwd = tmp_path / "deleted-session-cwd"

    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(missing_terminal_cwd))
    monkeypatch.setenv("HOME", str(fallback_home))
    monkeypatch.setattr(
        terminal_tool.os,
        "getcwd",
        lambda: (_ for _ in ()).throw(FileNotFoundError("cwd deleted")),
    )

    config = terminal_tool._get_env_config()

    assert config["cwd"] == str(fallback_home)
    assert config["cwd"] != str(missing_terminal_cwd)


def test_docker_mount_cwd_ignores_missing_terminal_cwd(monkeypatch, tmp_path):
    requested_workdir = tmp_path / "requested"
    requested_workdir.mkdir()
    missing_terminal_cwd = tmp_path / "deleted-session-cwd"

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
    monkeypatch.setenv("TERMINAL_CWD", str(missing_terminal_cwd))
    monkeypatch.setattr(
        terminal_tool.os,
        "getcwd",
        lambda: (_ for _ in ()).throw(FileNotFoundError("cwd deleted")),
    )

    config = terminal_tool._get_env_config(requested_workdir=str(requested_workdir))

    assert config["cwd"] == "/workspace"
    assert config["host_cwd"] == str(requested_workdir)


def test_resolve_start_cwd_falls_back_to_tmp_when_cwd_and_home_are_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "missing-terminal-cwd"))
    monkeypatch.setenv("HOME", str(tmp_path / "missing-home"))
    monkeypatch.setattr(
        terminal_tool.os,
        "getcwd",
        lambda: (_ for _ in ()).throw(FileNotFoundError("cwd deleted")),
    )

    assert terminal_tool._resolve_start_cwd(str(tmp_path / "missing-requested")) == "/tmp"


def test_terminal_tool_surfaces_pending_approval_as_state_not_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config_for_call",
        lambda requested_workdir=None: {
            "env_type": "local",
            "cwd": str(tmp_path),
            "timeout": 180,
            "local_persistent": False,
            "host_cwd": None,
        },
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_create_environment", lambda **kwargs: object())
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type: {
            "approved": False,
            "status": "pending_approval",
            "approval_pending": True,
            "command": command,
            "description": "dangerous command",
            "pattern_key": "rm_rf",
            "pattern_keys": ["rm_rf", "sudo"],
        },
    )

    result = json.loads(terminal_tool.terminal_tool("rm -rf /tmp/example"))

    assert result["status"] == "pending_approval"
    assert result["approval_pending"] is True
    assert result["output"] == ""
    assert result["error"] == ""
    assert result["exit_code"] == -1
    assert result["command"] == "rm -rf /tmp/example"
    assert result["description"] == "dangerous command"
    assert result["pattern_key"] == "rm_rf"
    assert result["pattern_keys"] == ["rm_rf", "sudo"]
