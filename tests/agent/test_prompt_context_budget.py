import agent.prompt_builder as prompt_builder
from agent.prompt_builder import (
    build_skills_system_prompt,
    should_load_context_files_for_session,
)


def test_gateway_without_explicit_terminal_cwd_skips_project_context():
    assert should_load_context_files_for_session(
        platform="discord",
        terminal_cwd=None,
        mode="explicit",
    ) is False


def test_gateway_with_explicit_terminal_cwd_keeps_project_context():
    assert should_load_context_files_for_session(
        platform="discord",
        terminal_cwd="/workspace/project",
        mode="explicit",
    ) is True


def test_gateway_hermes_install_cwd_skips_project_context():
    assert should_load_context_files_for_session(
        platform="discord",
        terminal_cwd=str(prompt_builder.Path(__file__).resolve().parents[2]),
        mode="explicit",
    ) is False


def test_cli_keeps_project_context_without_terminal_cwd():
    assert should_load_context_files_for_session(
        platform="cli",
        terminal_cwd=None,
        mode="explicit",
    ) is True


def test_compact_skills_prompt_preserves_tool_contract_without_full_catalog(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    skill_dir = hermes_home / "skills" / "devops" / "sample-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sample-skill\ndescription: A very detailed skill description that should not appear in compact mode.\n---\n\n# Body\n",
        encoding="utf-8",
    )
    (hermes_home / "skills" / "devops" / "DESCRIPTION.md").write_text(
        "---\ndescription: Devops category description.\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    prompt_builder._SKILLS_PROMPT_CACHE.clear()

    prompt = build_skills_system_prompt(mode="compact")

    assert "## Skills (mandatory)" in prompt
    assert "skills_list" in prompt
    assert "skill_view" in prompt
    assert "<available_skill_categories>" in prompt
    assert "devops: Devops category description." in prompt
    assert "<available_skills>" not in prompt
    assert "sample-skill" not in prompt
    assert "very detailed skill description" not in prompt
