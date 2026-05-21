from __future__ import annotations

from agent.repo_policy_closeout import (
    HUMAN_CLOSEOUT_SECTIONS,
    RUNTIME_TOOLING_EXTRA_SECTIONS,
    REQUIRED_LEDGER_SECTIONS,
    STANDARD_CLOSEOUT_SECTIONS,
    missing_policy_check_is_incomplete,
    render_closeout_template,
    validate_closeout_sections,
)


def test_standard_template_contains_all_required_sections() -> None:
    template = render_closeout_template()

    assert validate_closeout_sections(template).ok is True
    for section in STANDARD_CLOSEOUT_SECTIONS:
        assert section in template


def test_standard_template_leads_with_human_operational_story_before_ledger() -> None:
    template = render_closeout_template()

    first_policy_index = template.index("Policy check")
    for section in HUMAN_CLOSEOUT_SECTIONS:
        assert section in template
        assert template.index(section) < first_policy_index
    for section in REQUIRED_LEDGER_SECTIONS:
        assert section in template


def test_runtime_tooling_template_contains_restart_live_queue_sections() -> None:
    template = render_closeout_template(runtime_tooling=True)

    validation = validate_closeout_sections(template, runtime_tooling=True)
    assert validation.ok is True
    for section in RUNTIME_TOOLING_EXTRA_SECTIONS:
        assert section in template


def test_missing_policy_check_is_incomplete_closeout() -> None:
    incomplete = """Green 완료
- tests passed

Yellow 대기
- none

Red 필요
- none

검증
- pytest

Git 상태
- clean

Live 상태
- not live
"""

    validation = validate_closeout_sections(incomplete)

    assert validation.ok is False
    assert "Policy check" in validation.missing_sections
    assert missing_policy_check_is_incomplete([incomplete]) is True


def test_runtime_tooling_closeout_missing_queue_fields_is_incomplete() -> None:
    standard_only = render_closeout_template()

    validation = validate_closeout_sections(standard_only, runtime_tooling=True)

    assert validation.ok is False
    assert set(RUNTIME_TOOLING_EXTRA_SECTIONS).issubset(set(validation.missing_sections))
