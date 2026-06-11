"""NERVE FastAPI application entrypoint.

Wires together logging, the MongoDB connection, the orchestrator, and the HTTP
surface. Heavy logic lives in the ``orchestrator/`` and ``state/`` packages;
this module is the composition root only.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from time import perf_counter
from typing import AsyncIterator, Awaitable, Callable

import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

from config import settings
from exceptions import (
    DocumentNotFoundError,
    MCPError,
    MissionNotFoundError,
    NerveBaseError,
    PlanningFailedError,
)
from exceptions import ValidationError as StateValidationError
from failure_engine.injector import FailureEngine
from notifications.telegram_bot import telegram_notifier
from orchestrator.orchestrator import NERVEOrchestrator
from orchestrator.planner import MissionPlanner, TaskDefinition
from auth.dependencies import reject_unauthenticated_ws
from auth.middleware import AuthMiddleware
from auth.tokens import COOKIE_NAME, decode_token
from routes import actions, auth, dashboard, demo, failure, missions, webhooks
from state import database as db
from state.database import ensure_indexes
from websocket_manager import connection_manager

log = structlog.get_logger()

# Decomposition planner backing POST /internal/decompose. This is the function
# tool the (future) Agent Builder agent calls; today it decomposes via Gemini.
_decompose_planner = MissionPlanner()

# Application status strings (no magic strings in handlers).
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_READY = "ready"
STATUS_NOT_READY = "not_ready"
MONGO_OK = "ok"
MONGO_UNREACHABLE = "unreachable"


def configure_logging() -> None:
    """Configure structlog for structured JSON logging.

    Mirrors the processor chain defined in ARCHITECTURE.md section 2 so every
    log line carries a timestamp, level, merged contextvars, and renders as JSON.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


async def connect_to_mongo() -> AsyncIOMotorClient:
    """Open and verify a Motor client against MongoDB Atlas.

    Returns:
        A connected :class:`AsyncIOMotorClient`.

    Raises:
        Exception: Propagated if the initial ``ping`` fails so startup aborts loudly.
    """
    client: AsyncIOMotorClient = AsyncIOMotorClient(settings.mongodb_uri)
    await client.admin.command("ping")
    log.info("mongo_connected", database=settings.mongodb_database)
    return client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup and shutdown: logging, Mongo, and the orchestrator.

    Args:
        app: The FastAPI application whose ``state`` holds shared resources.

    Yields:
        Control to the running application.
    """
    configure_logging()
    if settings.google_cloud_project:
        import vertexai

        vertexai.init(
            project=settings.google_cloud_project,
            location=settings.google_cloud_location,
        )
        log.info("vertexai_initialized", project=settings.google_cloud_project, location=settings.google_cloud_location)
    client = await connect_to_mongo()
    app.state.mongo_client = client
    app.state.db = client[settings.mongodb_database]
    app.state.failure_engine = FailureEngine()
    app.state.orchestrator = NERVEOrchestrator(failure_engine=app.state.failure_engine)
    app.state.ws_manager = connection_manager
    app.state.telegram = telegram_notifier
    await ensure_indexes()
    if settings.telegram_enabled:
        app.state.telegram_task = asyncio.create_task(
            telegram_notifier.start_polling(), name="telegram-polling"
        )
    log.info("nerve_startup_complete")
    try:
        yield
    finally:
        await app.state.orchestrator.shutdown()
        await telegram_notifier.stop()
        client.close()
        log.info("nerve_shutdown_complete")


app = FastAPI(title="NERVE", version="0.1.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware)

# HTTP status mapping for typed NERVE errors (no stack traces leak to clients).
_ERROR_STATUS: dict[type[NerveBaseError], int] = {
    DocumentNotFoundError: 404,
    MissionNotFoundError: 404,
    StateValidationError: 422,
    PlanningFailedError: 502,
    MCPError: 502,
}


def _status_for(exc: NerveBaseError) -> int:
    """Map a typed NERVE error to an HTTP status code (default 500)."""
    for error_type, status in _ERROR_STATUS.items():
        if isinstance(exc, error_type):
            return status
    return 500


@app.exception_handler(NerveBaseError)
async def nerve_error_handler(request: Request, exc: NerveBaseError) -> JSONResponse:
    """Return a structured JSON error for any typed NERVE error."""
    status = _status_for(exc)
    log.error("request_error", path=request.url.path, status=status, **exc.to_dict())
    return JSONResponse(status_code=status, content={"error": exc.to_dict()})


@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Log every request with method, path, status, and duration."""
    start = perf_counter()
    response = await call_next(request)
    duration_ms = (perf_counter() - start) * 1000
    log.info(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


@app.get("/health")
async def health() -> dict:
    """Report system health and the number of active mission loops.

    Returns:
        Status payload with overall health and component states.
    """
    orchestrator: NERVEOrchestrator | None = getattr(app.state, "orchestrator", None)
    mongo_ok = getattr(app.state, "db", None) is not None
    return {
        "status": STATUS_OK if mongo_ok else STATUS_DEGRADED,
        "components": {
            "mongo": STATUS_OK if mongo_ok else STATUS_DEGRADED,
            "orchestrator": STATUS_OK if orchestrator is not None else STATUS_DEGRADED,
        },
        "active_missions": orchestrator.active_mission_ids if orchestrator else [],
    }


@app.get("/health/live")
async def health_live() -> dict:
    """Liveness probe: the process is up. No dependencies are checked."""
    return {"status": STATUS_OK}


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Readiness probe: 200 if MongoDB answers a ping, else 503.

    Returns:
        ``{"status": "ready", "mongodb": "ok"}`` (200) or
        ``{"status": "not_ready", "mongodb": "unreachable"}`` (503). Never raises.
    """
    db_handle = getattr(app.state, "db", None)
    try:
        if db_handle is None:
            raise RuntimeError("database not initialized")
        await db_handle.command("ping")
    except Exception as exc:  # noqa: BLE001 — readiness must never raise
        log.warning("readiness_check_failed", error=str(exc))
        return JSONResponse(
            status_code=503, content={"status": STATUS_NOT_READY, "mongodb": MONGO_UNREACHABLE}
        )
    return JSONResponse(status_code=200, content={"status": STATUS_READY, "mongodb": MONGO_OK})


@app.websocket("/ws/{mission_id}")
async def mission_ws(websocket: WebSocket, mission_id: str) -> None:
    """Push live mission events to a dashboard client over a WebSocket.

    The client opens one socket per mission it is viewing; the connection is
    held open and the ConnectionManager pushes each persisted event to it.
    Inbound frames are read only to detect disconnection.

    Args:
        websocket: The client WebSocket.
        mission_id: Mission whose events the client wants to receive.
    """
    if await reject_unauthenticated_ws(websocket):  # WS isn't gated by the HTTP middleware
        return
    uid = decode_token(websocket.cookies.get(COOKIE_NAME))
    if uid is None or await db.get_owned_mission(mission_id, uid) is None:
        await websocket.close(code=4404)  # not your mission (or gone)
        return
    manager = app.state.ws_manager
    await manager.connect(mission_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive; client may send pings
    except WebSocketDisconnect:
        manager.disconnect(mission_id, websocket)
    except Exception as exc:  # noqa: BLE001 — ensure cleanup on any socket error
        log.warning("ws_endpoint_error", mission_id=mission_id, error=str(exc))
        manager.disconnect(mission_id, websocket)


# --------------------------------------------------------------------------- #
# Internal router — function tools called during planning.
# --------------------------------------------------------------------------- #
internal_router = APIRouter(prefix="/internal", tags=["internal"])


class DecomposeRequest(BaseModel):
    """Request body for the decomposition function tool."""

    goal: str
    context: dict = Field(default_factory=dict)


@internal_router.post("/decompose")
async def decompose(request: DecomposeRequest) -> list[TaskDefinition]:
    """Decompose a goal into structured task definitions.

    This is the function-tool endpoint the (future) Agent Builder agent calls
    during planning; it currently decomposes via Gemini behind ``MissionPlanner``.

    Args:
        request: Goal text plus optional mission context.

    Returns:
        A JSON list of task definitions for the planner/agent to consume.

    Raises:
        HTTPException: 502 if decomposition fails.
    """
    try:
        return await _decompose_planner.decompose(request.goal, request.context)
    except PlanningFailedError as exc:
        log.error("decompose_failed", error=str(exc))
        raise HTTPException(status_code=502, detail="decomposition failed") from exc


app.include_router(internal_router)
app.include_router(auth.router)
app.include_router(missions.router)
app.include_router(actions.router)
app.include_router(failure.router)
app.include_router(demo.router)
app.include_router(webhooks.router)
app.include_router(dashboard.router)
