"""Reusable Kanban intake domain logic.

This module deliberately contains no Discord adapter code and does not create,
claim, dispatch, or otherwise mutate Kanban tasks.  It turns a parsed intake
request into one of a small set of structured outcomes that a gateway, CLI, or
agent tool can render consistently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping, Sequence


class IntakeOutcome(str, Enum):
    SUCCESS = "success"
    AMBIGUOUS_INPUT = "ambiguous_input"
    BLOCKED_POLICY = "blocked_policy"
    KANBAN_UNAVAILABLE = "kanban_unavailable"
    APPROVAL_REQUIRED = "approval_required"


class IntakeLifecycle(str, Enum):
    INTERVIEW = "interview"
    DRAFT = "draft"
    ADMIT = "admit"


PROJECT_NAMESPACES = {
    "bo": "BO",
    "dc": "DC",
    "ws": "WS",
    "rs": "RS",
}

APPROVAL_REQUIRED_FOR = [
    "worker dispatch",
    "repo mutation",
    "PR creation/update/merge",
    "secret/env mutation",
    "provider/customer-visible action",
    "gateway command registration",
    "gateway reload or restart",
]

_BOUNDARY_LINES = [
    "Invoking this Discord intake command is not execution approval.",
    "Live Discord slash-command registration and Hermes gateway reload/restart require separate human approval.",
]

_EXECUTOR_ONLY_RE = re.compile(
    r"^\s*(run|launch|start|use)\s+(codex|claude|opencode|omx|agent|worker)\s*$",
    re.IGNORECASE,
)
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LIVE_ACTION_RE = re.compile(
    r"\b("
    r"restart|reload|dispatch|execute|run worker|spawn worker|"
    r"push|merge|open pr|create pr|update pr|pull request|"
    r"secret|secrets|env|credential|provider|customer|billing|"
    r"register (?:the )?(?:discord )?(?:slash )?command|live rollout"
    r")\b",
    re.IGNORECASE,
)
_GATEWAY_RE = re.compile(r"\b(gateway|discord|slash command|command registration)\b", re.IGNORECASE)


@dataclass(frozen=True)
class IntakeRequest:
    goal: str
    project: str
    tenant: str | None = None
    context: str | None = None
    non_goals: Sequence[str] = field(default_factory=list)
    acceptance_criteria: Sequence[str] = field(default_factory=list)
    side_effect_boundary: str | None = None
    open_questions: Sequence[str] = field(default_factory=list)
    suggested_breakdown: Sequence[str] = field(default_factory=list)
    initial_routing: str = "proposed_only"
    source: str = "parsed_intake_request"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IntakeRequest":
        def strings(name: str) -> list[str]:
            raw = data.get(name) or []
            if isinstance(raw, str):
                return [raw.strip()] if raw.strip() else []
            if not isinstance(raw, Sequence):
                return []
            return [str(item).strip() for item in raw if str(item).strip()]

        return cls(
            goal=str(data.get("goal") or ""),
            project=str(data.get("project") or ""),
            tenant=(str(data["tenant"]).strip() if data.get("tenant") is not None else None),
            context=(str(data["context"]).strip() if data.get("context") else None),
            non_goals=strings("non_goals"),
            acceptance_criteria=strings("acceptance_criteria"),
            side_effect_boundary=(
                str(data["side_effect_boundary"]).strip()
                if data.get("side_effect_boundary")
                else None
            ),
            open_questions=strings("open_questions"),
            suggested_breakdown=strings("suggested_breakdown"),
            initial_routing=str(data.get("initial_routing") or "proposed_only"),
            source=str(data.get("source") or "parsed_intake_request"),
        )


@dataclass(frozen=True)
class IntakeResult:
    outcome: IntakeOutcome
    reply: str
    reasons: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    draft: dict[str, Any] | None = None
    kanban_admission_handoff: dict[str, Any] | None = None
    dispatch_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.outcome in {IntakeOutcome.SUCCESS, IntakeOutcome.APPROVAL_REQUIRED},
            "outcome": self.outcome.value,
            "reply": self.reply,
            "reasons": self.reasons,
            "questions": self.questions,
            "draft": self.draft,
            "kanban_admission_handoff": self.kanban_admission_handoff,
            "dispatch_allowed": self.dispatch_allowed,
        }


def _normalize_project(project: str) -> tuple[str | None, str | None]:
    value = (project or "").strip().lower()
    namespace = PROJECT_NAMESPACES.get(value)
    if not namespace:
        return None, "project는 bo, dc, ws, rs 중 하나여야 합니다."
    return namespace, None


def _normalize_tenant(tenant: str | None) -> tuple[str | None, str | None]:
    if tenant is None or str(tenant).strip() == "":
        return None, None
    raw = str(tenant).strip()
    lowered = raw.lower()
    if raw != lowered:
        return None, "tenant must already be lowercase to avoid collision-prone normalization."
    if "/" in raw or "\\" in raw or "@" in raw or "://" in raw:
        return None, "tenant contains forbidden path, mention, or URL characters."
    if not _TENANT_RE.fullmatch(raw):
        return None, "tenant must match [a-z0-9][a-z0-9._-]{0,63}."
    return raw, None


def _normalize_lifecycle(value: IntakeLifecycle | str) -> IntakeLifecycle:
    if isinstance(value, IntakeLifecycle):
        return value
    try:
        return IntakeLifecycle(str(value).strip().lower())
    except ValueError:
        return IntakeLifecycle.INTERVIEW


def _policy_block_reasons(request: IntakeRequest) -> list[str]:
    text = "\n".join(
        part
        for part in [
            request.goal,
            request.context,
            request.side_effect_boundary,
            "\n".join(request.acceptance_criteria),
            "\n".join(request.suggested_breakdown),
        ]
        if part
    )
    actionable_segments = []
    for segment in re.split(r"[\n.;]+", text):
        stripped = segment.strip()
        if not stripped or not _LIVE_ACTION_RE.search(stripped):
            continue
        if re.search(
            r"\b(no|not|never|without|forbid(?:den)?|prohibit(?:ed)?|금지|별도 승인|admission[- ]only)\b",
            stripped,
            re.IGNORECASE,
        ):
            continue
        actionable_segments.append(stripped)

    if not actionable_segments:
        return []

    reasons = ["intake request includes live execution or mutation language"]
    if any(_GATEWAY_RE.search(segment) for segment in actionable_segments):
        reasons.append("gateway/Discord command rollout requires separate approval")
    return reasons


def _validation_reasons(request: IntakeRequest) -> list[str]:
    reasons: list[str] = []
    goal = (request.goal or "").strip()
    if len(goal) < 12:
        reasons.append("goal must be at least 12 characters and describe an outcome")
    if len(goal) > 2000:
        reasons.append("goal must be at most 2000 characters")
    if _EXECUTOR_ONLY_RE.fullmatch(goal):
        reasons.append("goal is only an executor command, not an outcome")
    _, project_error = _normalize_project(request.project)
    if project_error:
        reasons.append(project_error)
    _, tenant_error = _normalize_tenant(request.tenant)
    if tenant_error:
        reasons.append(tenant_error)
    return reasons


def _clarifying_questions(request: IntakeRequest, reasons: Sequence[str]) -> list[str]:
    questions: list[str] = []
    if any("goal" in reason for reason in reasons):
        questions.append("원하는 결과를 한 문장으로 더 구체화해 주세요. 무엇이 완료되면 성공인가요?")
    if any("project" in reason for reason in reasons):
        questions.append("project는 bo, dc, ws, rs 중 어디인가요?")
    if any("tenant" in reason for reason in reasons):
        questions.append("tenant를 소문자 slug 형식으로 다시 적어 주세요. 예: ops-core")
    if not request.acceptance_criteria:
        questions.append("완료 판정 기준을 1~3개만 적어 주세요.")
    if not request.side_effect_boundary:
        questions.append("이 intake에서 금지할 side effect 범위가 있나요? 예: repo/PR/gateway/secrets/customer action 금지")
    return questions[:3]


def _build_draft(request: IntakeRequest, namespace: str, tenant: str | None) -> dict[str, Any]:
    open_questions = [str(q).strip() for q in request.open_questions if str(q).strip()]
    return {
        "goal": request.goal.strip(),
        "project": request.project.strip().lower(),
        "namespace": namespace,
        "tenant": tenant,
        "context": request.context,
        "non_goals": list(request.non_goals),
        "acceptance_criteria": list(request.acceptance_criteria),
        "side_effect_boundary": request.side_effect_boundary
        or "Admission only. No worker dispatch, repo/PR mutation, secrets/env mutation, provider/customer action, Discord registration, or gateway reload/restart.",
        "approval_required_for": list(APPROVAL_REQUIRED_FOR),
        "open_questions": open_questions,
        "suggested_breakdown": list(request.suggested_breakdown),
        "initial_routing": "proposed_only",
        "executor_dispatch": "forbidden_during_admission",
        "source": request.source,
    }


def _render_draft(draft: Mapping[str, Any]) -> str:
    criteria = draft.get("acceptance_criteria") or []
    criteria_text = "\n".join(f"- {item}" for item in criteria) or "- TBD after clarification"
    non_goals = draft.get("non_goals") or ["No worker dispatch during admission"]
    non_goals_text = "\n".join(f"- {item}" for item in non_goals)
    return (
        f"# Seed Contract Draft ({draft['namespace']})\n\n"
        f"Goal: {draft['goal']}\n\n"
        f"Acceptance criteria:\n{criteria_text}\n\n"
        f"Non-goals / boundaries:\n{non_goals_text}\n"
        f"- executor_dispatch={draft['executor_dispatch']}\n"
        f"- initial_routing={draft['initial_routing']}\n\n"
        + "\n".join(_BOUNDARY_LINES)
    )


def _build_handoff(draft: Mapping[str, Any]) -> dict[str, Any]:
    body = _render_draft(draft)
    return {
        "operation": "kanban_admission_request",
        "title": f"[{draft['namespace']}] Intake admission: {draft['goal'][:80]}",
        "body": body,
        "namespace": draft["namespace"],
        "tenant": "kanban",
        "assignee": None,
        "status": "triage",
        "triage": True,
        "dispatch_allowed": False,
        "metadata": {
            "project": draft["project"],
            "namespace": draft["namespace"],
            "tenant": draft.get("tenant"),
            "initial_routing": "proposed_only",
            "executor_dispatch": "forbidden_during_admission",
            "approval_required_for": list(APPROVAL_REQUIRED_FOR),
        },
    }


def evaluate_intake_request(
    request: IntakeRequest | Mapping[str, Any],
    *,
    lifecycle: IntakeLifecycle | str = IntakeLifecycle.INTERVIEW,
    kanban_available: bool = True,
    approve_admission: bool = False,
) -> IntakeResult:
    """Evaluate a parsed intake request without performing side effects.

    `approve_admission=True` means approval to emit an admission handoff request,
    not approval to dispatch an executor. The returned handoff is data only; the
    caller must still decide whether/how to write a Kanban admission record.
    """
    req = request if isinstance(request, IntakeRequest) else IntakeRequest.from_mapping(request)
    normalized_lifecycle = _normalize_lifecycle(lifecycle)

    policy_reasons = _policy_block_reasons(req)
    if policy_reasons:
        return IntakeResult(
            outcome=IntakeOutcome.BLOCKED_POLICY,
            reply=(
                "Intake는 실행 승인이 아닙니다. 요청 내용은 admission card로만 정리할 수 있고, "
                "dispatch/PR/secrets/env/gateway restart/live rollout은 별도 승인 후에만 진행됩니다."
            ),
            reasons=policy_reasons,
        )

    validation_reasons = _validation_reasons(req)
    needs_more_detail = not req.acceptance_criteria and normalized_lifecycle is IntakeLifecycle.INTERVIEW
    if validation_reasons or needs_more_detail:
        questions = _clarifying_questions(req, validation_reasons)
        return IntakeResult(
            outcome=IntakeOutcome.AMBIGUOUS_INPUT,
            reply=(
                "카드로 만들기 전에 확정해야 합니다.\n"
                + "\n".join(f"{idx}. {q}" for idx, q in enumerate(questions, start=1))
                + "\n아직 Kanban admission card는 만들지 않았습니다. 카드는 만들지 않았습니다."
            ),
            reasons=validation_reasons,
            questions=questions,
        )

    namespace, _ = _normalize_project(req.project)
    tenant, _ = _normalize_tenant(req.tenant)
    assert namespace is not None
    draft = _build_draft(req, namespace, tenant)

    if normalized_lifecycle is not IntakeLifecycle.ADMIT or not approve_admission:
        return IntakeResult(
            outcome=IntakeOutcome.APPROVAL_REQUIRED,
            reply=(
                "Seed Contract draft is ready, but Kanban admission handoff requires explicit approval. "
                "No card was created and no executor dispatch is allowed."
            ),
            draft=draft,
        )

    if not kanban_available:
        return IntakeResult(
            outcome=IntakeOutcome.KANBAN_UNAVAILABLE,
            reply="Kanban is unavailable, so no admission handoff was produced and no card was created.",
            draft=draft,
            reasons=["kanban unavailable"],
        )

    return IntakeResult(
        outcome=IntakeOutcome.SUCCESS,
        reply=(
            "접수했어요. Kanban admission handoff를 만들었습니다. "
            "중요: 이건 실행 승인이 아니라 결정 게이트입니다."
        ),
        draft=draft,
        kanban_admission_handoff=_build_handoff(draft),
    )
