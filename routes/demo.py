"""Demo route: start the scripted demo scenario (opt-in via DEMO_MODE)."""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from auth.dependencies import current_user
from config import settings
from state.models import User

log = structlog.get_logger()
router = APIRouter(prefix="/demo", tags=["demo"])

_BACKGROUND: set[asyncio.Task] = set()


@router.get("/start")
async def start_demo(request: Request, user: User = Depends(current_user)) -> dict:
    """Start the scripted demo scenario as a background task.

    Args:
        request: FastAPI request (for the orchestrator on app.state).
        user: The authenticated user who clicked start (becomes mission owner).

    Returns:
        A status payload.

    Raises:
        HTTPException: 403 if demo mode is disabled.
    """
    if not settings.demo_mode:
        raise HTTPException(status_code=403, detail="demo mode disabled")
    from failure_engine.demo_scenario import DemoScenario

    scenario = DemoScenario(owner_id=user.user_id)
    mission_id = await scenario.prepare()  # create the mission so we can return its id
    task = asyncio.create_task(scenario.run(request.app.state.orchestrator), name="demo-scenario")
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    log.info("demo_scenario_started", mission_id=mission_id)
    return {"status": "demo_started", "mission_id": mission_id}
