"""Turn a completed GENERAL mission's task results into a hand-off action."""
from __future__ import annotations

from typing import Awaitable, Callable

import structlog

from state import database as db
from state.models import Task

log = structlog.get_logger().bind(component="research_concierge")

ACTION_HUMAN_APPROVAL = "human_approval_request"
EVENT_RESEARCH_SYNTHESIZED = "RESEARCH_SYNTHESIZED"
SOURCE_ORCH = "orchestrator"

_PROMPT = (
    "You are NERVE's research concierge. Given the goal and the raw findings "
    "from web searches, write a concise recommendation (max 6 lines) naming the "
    "best option(s) with prices and the exact links to proceed. Findings:\n"
    "GOAL: {goal}\nFINDINGS:\n{findings}\n"
)

GenerateFn = Callable[[str], Awaitable[str]]


async def synthesize_and_handoff(mission_id: str, *, generate: GenerateFn | None = None) -> None:
    """Synthesize completed-task findings and create a hand-off approval action.

    Never raises: any failure is logged so the mission can still resolve.

    Args:
        mission_id: The GENERAL mission whose tasks have completed.
        generate: Async text generator (defaults to the planner's Gemini caller).
    """
    try:
        await _run(mission_id, generate or _default_generate())
    except Exception as exc:  # noqa: BLE001 — synthesis must not crash the loop
        log.warning("synthesis_failed", mission_id=mission_id, error=str(exc))


async def _run(mission_id: str, generate: GenerateFn) -> None:
    """Gather findings, reason once, and create the approval action."""
    state = await db.get_mission_state(mission_id)
    if state is None:
        return
    findings = _collect_findings(state.tasks)
    if not findings:
        log.info("synthesis_no_findings", mission_id=mission_id)
        return
    prompt = _PROMPT.format(goal=state.mission.goal, findings=findings)
    recommendation = await generate(prompt)
    await _create_handoff(mission_id, recommendation)


def _collect_findings(tasks: list[Task]) -> str:
    """Concatenate completed web-search task results into a prompt-ready string."""
    lines: list[str] = []
    for task in tasks:
        if task.status != "completed" or not task.result:
            continue
        answer = task.result.get("answer") if isinstance(task.result, dict) else None
        if answer:
            lines.append(f"- {task.description}: {answer}")
    return "\n".join(lines)


async def _create_handoff(mission_id: str, recommendation: str) -> None:
    """Create the human_approval_request action, emit the event, ping Telegram."""
    action = await db.create_action(
        mission_id, ACTION_HUMAN_APPROVAL, {"recommendation": recommendation}
    )
    await db.emit_event(
        mission_id, EVENT_RESEARCH_SYNTHESIZED, {"action_id": action.action_id}, SOURCE_ORCH
    )
    await _notify_telegram(action.action_id, mission_id, recommendation)
    log.info("synthesis_handoff_created", mission_id=mission_id, action_id=action.action_id)


async def _notify_telegram(action_id: str, mission_id: str, recommendation: str) -> None:
    """Best-effort Telegram hand-off ping; never raises (bot must not crash the app)."""
    try:
        from notifications.telegram_bot import telegram_notifier

        await telegram_notifier.send_approval_request(
            action_id, ACTION_HUMAN_APPROVAL, recommendation, mission_id
        )
    except Exception as exc:  # noqa: BLE001 — notifications are best-effort
        log.warning("synthesis_telegram_failed", error=str(exc))


def _default_generate() -> GenerateFn:
    """Return the planner's public Gemini text-generation seam."""
    from orchestrator.planner import gemini_generate

    return gemini_generate
