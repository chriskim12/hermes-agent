from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

HUMAN_CLOSEOUT_SECTIONS: tuple[str, ...] = (
    "결론",
    "실제 반영",
    "아직 안 한 것",
    "다음 판단",
)

REQUIRED_LEDGER_SECTIONS: tuple[str, ...] = (
    "Policy check",
    "Green 완료",
    "Yellow 대기",
    "Red 필요",
    "검증",
    "Git 상태",
    "Live 상태",
)

STANDARD_CLOSEOUT_SECTIONS: tuple[str, ...] = HUMAN_CLOSEOUT_SECTIONS + REQUIRED_LEDGER_SECTIONS

RUNTIME_TOOLING_EXTRA_SECTIONS: tuple[str, ...] = (
    "Gateway restart 필요",
    "Live runtime 반영됨",
    "대기열 포함됨",
)

STANDARD_CLOSEOUT_TEMPLATE = """결론
- <한 줄로: 이 작업이 실제로 어디까지 반영됐는지>

실제 반영
- <작업 결과가 어느 branch/worktree/commit/policy/checker까지 반영됐는지>

아직 안 한 것
- <push/PR/merge/release/deploy/live apply/restart/env-secret/customer-visible mutation 중 하지 않은 것>

다음 판단
- <Chris가 판단해야 할 것 또는 다음 카드/게이트>

Policy check
- <repo-policy checker result, policy path, pass/fail_closed/drift reason>

Green 완료
- <completed local/green work; keep this as ledger, not the headline>

Yellow 대기
- <queued release/live/restart/review items, or none>

Red 필요
- <actions still requiring explicit approval, or none crossed>

검증
- <tests/static checks/proofs>

Git 상태
- <branch/worktree/commit/dirty state/push status>

Live 상태
- <deployed/live/runtime/customer-visible state>
"""

RUNTIME_TOOLING_CLOSEOUT_TEMPLATE = STANDARD_CLOSEOUT_TEMPLATE + """
Gateway restart 필요
- <yes/no and why; do not restart unless explicitly approved>

Live runtime 반영됨
- <yes/no with runtime proof if applied>

대기열 포함됨
- <yes/no; queue entry id/details if restart/live apply is pending>
"""


@dataclass(frozen=True)
class CloseoutSectionValidation:
    ok: bool
    missing_sections: tuple[str, ...]
    required_sections: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "missing_sections": list(self.missing_sections),
            "required_sections": list(self.required_sections),
        }


def required_closeout_sections(*, runtime_tooling: bool = False) -> tuple[str, ...]:
    if runtime_tooling:
        return STANDARD_CLOSEOUT_SECTIONS + RUNTIME_TOOLING_EXTRA_SECTIONS
    return STANDARD_CLOSEOUT_SECTIONS


def validate_closeout_sections(text: str, *, runtime_tooling: bool = False) -> CloseoutSectionValidation:
    required = required_closeout_sections(runtime_tooling=runtime_tooling)
    missing = tuple(section for section in required if section not in text)
    return CloseoutSectionValidation(ok=not missing, missing_sections=missing, required_sections=required)


def render_closeout_template(*, runtime_tooling: bool = False) -> str:
    return RUNTIME_TOOLING_CLOSEOUT_TEMPLATE if runtime_tooling else STANDARD_CLOSEOUT_TEMPLATE


def missing_policy_check_is_incomplete(texts: Iterable[str]) -> bool:
    return any("Policy check" not in text for text in texts)
