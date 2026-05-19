"""Focused regression tests for execute_code cwd safety behavior."""

import sys
import types

import tools.code_execution_tool as code_execution_tool


def test_resolve_child_cwd_non_project_uses_staging_dir(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    assert code_execution_tool._resolve_child_cwd("strict", str(staging_dir)) == str(staging_dir)


def test_resolve_child_cwd_project_prefers_valid_terminal_cwd(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    terminal_cwd = tmp_path / "terminal-cwd"
    terminal_cwd.mkdir()

    monkeypatch.setenv("TERMINAL_CWD", str(terminal_cwd))

    assert code_execution_tool._resolve_child_cwd("project", str(staging_dir)) == str(terminal_cwd)


def test_resolve_child_cwd_project_uses_safe_getcwd_after_invalid_terminal_cwd(
    monkeypatch, tmp_path
):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    current_cwd = tmp_path / "current-cwd"
    current_cwd.mkdir()
    fake_terminal_tool = types.SimpleNamespace(_safe_getcwd=lambda: str(current_cwd))

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "missing-terminal-cwd"))
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", fake_terminal_tool)

    assert code_execution_tool._resolve_child_cwd("project", str(staging_dir)) == str(current_cwd)


def test_resolve_child_cwd_project_falls_back_to_staging_when_cwd_deleted(
    monkeypatch, tmp_path
):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    fake_terminal_tool = types.SimpleNamespace(_safe_getcwd=lambda: None)

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "missing-terminal-cwd"))
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", fake_terminal_tool)

    assert code_execution_tool._resolve_child_cwd("project", str(staging_dir)) == str(staging_dir)


def test_resolve_child_cwd_project_falls_back_to_staging_when_safe_getcwd_raises(
    monkeypatch, tmp_path
):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    def _raise_deleted_cwd():
        raise FileNotFoundError("cwd deleted")

    fake_terminal_tool = types.SimpleNamespace(_safe_getcwd=_raise_deleted_cwd)

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "missing-terminal-cwd"))
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", fake_terminal_tool)

    assert code_execution_tool._resolve_child_cwd("project", str(staging_dir)) == str(staging_dir)
