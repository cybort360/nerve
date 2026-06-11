"""Action routes: human approval / rejection of pending actions.

Approval is the human gate required before any action executes (CLAUDE.md
invariant 2). Approving a rollback dispatches an ExecutionAgent — which itself
re-verifies the approved Action before calling the MCP tool.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from auth.dependencies import current_user
from config import settings
from notifications.telegram_bot import telegram_notifier
from routes.schemas import ApproveRequest, RejectRequest
from state import database as db
from state.models import Action, User

log = structlog.get_logger()
router = APIRouter(prefix="/actions", tags=["actions"])

SOURCE_USER = "user"
EVENT_ACTION_APPROVED = "ACTION_APPROVED"
EVENT_ACTION_REJECTED = "ACTION_REJECTED"
ACTION_GITLAB_ROLLBACK = "gitlab_rollback"
PENDING = "pending"
NOTIFY_LEVEL_SUCCESS = "success"
NOTIFY_LEVEL_WARNING = "warning"

_BACKGROUND: set[asyncio.Task] = set()


@router.post("/{action_id}/approve", response_model=Action)
async def approve_action(
    action_id: str,
    body: ApproveRequest,
    request: Request,
    user: User = Depends(current_user),
) -> Action:
    """Approve a pending action and dispatch its execution.

    Args:
        action_id: Action to approve.
        body: Who approved it.
        request: FastAPI request (for the orchestrator on app.state).
        user: Authenticated user (injected by dependency).

    Returns:
        The updated :class:`Action`.

    Raises:
        HTTPException: 404 if not found or mission not owned, 409 if not pending.
    """
    action = await _require_pending(action_id)
    if await db.get_owned_mission(action.mission_id, user.user_id) is None:
        raise HTTPException(status_code=404, detail="action not found")
    updated = await db.update_action_status(action_id, "approved", approved_by=body.approved_by)
    await db.emit_event(
        action.mission_id,
        EVENT_ACTION_APPROVED,
        {"action_id": action_id, "approved_by": body.approved_by},
        SOURCE_USER,
    )
    await _maybe_trigger_execution(request.app, updated)
    await telegram_notifier.send_notification(
        f"✅ Action approved via dashboard by {body.approved_by}",
        level=NOTIFY_LEVEL_SUCCESS,
        mission_id=action.mission_id,
    )
    log.info("action_approved", action_id=action_id, approved_by=body.approved_by)
    return updated


@router.post("/{action_id}/reject", response_model=Action)
async def reject_action(
    action_id: str,
    body: RejectRequest,
    user: User = Depends(current_user),
) -> Action:
    """Reject a pending action.

    Args:
        action_id: Action to reject.
        body: Who rejected it and why.
        user: Authenticated user (injected by dependency).

    Returns:
        The updated :class:`Action`.

    Raises:
        HTTPException: 404 if not found or mission not owned, 409 if not pending.
    """
    action = await _require_pending(action_id)
    if await db.get_owned_mission(action.mission_id, user.user_id) is None:
        raise HTTPException(status_code=404, detail="action not found")
    updated = await db.update_action_status(action_id, "rejected", approved_by=body.approved_by)
    await db.emit_event(
        action.mission_id,
        EVENT_ACTION_REJECTED,
        {"action_id": action_id, "approved_by": body.approved_by, "reason": body.reason},
        SOURCE_USER,
    )
    await telegram_notifier.send_notification(
        "❌ Action rejected via dashboard",
        level=NOTIFY_LEVEL_WARNING,
        mission_id=action.mission_id,
    )
    log.info("action_rejected", action_id=action_id, reason=body.reason)
    return updated


async def _require_pending(action_id: str) -> Action:
    """Load an action, enforcing existence (404) and pending status (409)."""
    action = await db.get_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    if action.status != PENDING:
        raise HTTPException(status_code=409, detail=f"action already {action.status}")
    return action


async def _maybe_trigger_execution(app: object, action: Action) -> None:
    """Dispatch an ExecutionAgent for an approved executable action.

    Creates a task describing the action and runs the ExecutionAgent (built via
    the orchestrator's factory) in the background. The agent re-checks approval
    before calling the MCP tool, so this never executes an unapproved action.
    """
    if action.action_type != ACTION_GITLAB_ROLLBACK:
        return
    task = await db.create_task(
        action.mission_id, "execution", f"Execute approved {action.action_type} (action {action.action_id})"
    )
    agent = await app.state.orchestrator.execution_agent_factory(action.mission_id)
    tool_args = {
        "project_id": settings.gitlab_project_id,
        "ref": action.payload.get("ref", "main"),
        "variables": {"ROLLBACK_TO": action.payload.get("sha")},
    }
    coro = agent.run({"task": task.model_dump(mode="json"), "action_id": action.action_id, "tool_args": tool_args})
    background = asyncio.create_task(coro, name=f"exec-{action.action_id}")
    _BACKGROUND.add(background)
    background.add_done_callback(_BACKGROUND.discard)
