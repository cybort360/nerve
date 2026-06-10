"""Motor-backed persistence API for NERVE.

Every MongoDB read and write goes through this module. Raw Motor/pymongo
exceptions never escape: they are caught and re-raised as typed
:class:`~exceptions.StateError` subclasses (see ARCHITECTURE.md section 1).
Writes are retried per the standard policy (2 attempts, fixed 1s wait).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar
from uuid import uuid4

import structlog
from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from config import settings
from exceptions import (
    AuthError,
    DocumentNotFoundError,
    StateError,
    ValidationError,
    WriteFailedError,
)
from state.models import (
    Action,
    ActionStatus,
    ActionType,
    AgentRole,
    Belief,
    Event,
    EventSource,
    MetricSample,
    Mission,
    MissionState,
    MissionStatus,
    MissionType,
    Snapshot,
    Task,
    TaskStatus,
    User,
)

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

# --------------------------------------------------------------------------- #
# Structural constants. Collection names are part of the storage contract, not
# environment-tunable config, so they live here rather than in config.py.
# --------------------------------------------------------------------------- #
COLLECTION_MISSIONS = "missions"
COLLECTION_TASKS = "tasks"
COLLECTION_EVENTS = "events"
COLLECTION_ACTIONS = "actions"
COLLECTION_SNAPSHOTS = "snapshots"
COLLECTION_BELIEFS = "beliefs"
COLLECTION_METRICS = "metrics"
COLLECTION_USERS = "users"
DEFAULT_RECENT_EVENTS_LIMIT = 50
DEFAULT_METRIC_SERIES_LIMIT = 48
DEFAULT_LIST_MISSIONS_LIMIT = 12

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


# --------------------------------------------------------------------------- #
# Connection + collection accessors
# --------------------------------------------------------------------------- #
def get_database() -> AsyncIOMotorDatabase:
    """Return the shared Motor database, initializing the client once.

    Returns:
        The singleton :class:`AsyncIOMotorDatabase` for the configured database.
    """
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(settings.mongodb_uri)
        _db = _client[settings.mongodb_database]
        log.info("database_initialized", database=settings.mongodb_database)
    return _db


def get_missions_collection() -> AsyncIOMotorCollection:
    """Return the ``missions`` collection."""
    return get_database()[COLLECTION_MISSIONS]


def get_tasks_collection() -> AsyncIOMotorCollection:
    """Return the ``tasks`` collection."""
    return get_database()[COLLECTION_TASKS]


def get_events_collection() -> AsyncIOMotorCollection:
    """Return the ``events`` collection."""
    return get_database()[COLLECTION_EVENTS]


def get_actions_collection() -> AsyncIOMotorCollection:
    """Return the ``actions`` collection."""
    return get_database()[COLLECTION_ACTIONS]


def get_snapshots_collection() -> AsyncIOMotorCollection:
    """Return the ``snapshots`` collection."""
    return get_database()[COLLECTION_SNAPSHOTS]


def get_beliefs_collection() -> AsyncIOMotorCollection:
    """Return the ``beliefs`` collection."""
    return get_database()[COLLECTION_BELIEFS]


def get_metrics_collection() -> AsyncIOMotorCollection:
    """Return the ``metrics`` collection."""
    return get_database()[COLLECTION_METRICS]


def get_users_collection() -> AsyncIOMotorCollection:
    """Return the ``users`` collection."""
    return get_database()[COLLECTION_USERS]


async def ensure_indexes() -> None:
    """Create the MongoDB indexes the state layer relies on.

    Called once at app startup. Idempotent: re-creating an existing index is a
    no-op, so this is safe to run on every boot.
    """
    missions = get_missions_collection()
    await missions.create_index("mission_id", unique=True, background=True)
    await missions.create_index("status", background=True)
    await missions.create_index("created_at", background=True)

    tasks = get_tasks_collection()
    await tasks.create_index("task_id", unique=True, background=True)
    await tasks.create_index("mission_id", background=True)
    await tasks.create_index("status", background=True)
    await tasks.create_index([("mission_id", ASCENDING), ("status", ASCENDING)], background=True)

    events = get_events_collection()
    await events.create_index("event_id", unique=True, background=True)
    await events.create_index("mission_id", background=True)
    await events.create_index([("created_at", DESCENDING)], background=True)

    actions = get_actions_collection()
    await actions.create_index("action_id", unique=True, background=True)
    await actions.create_index("mission_id", background=True)
    await actions.create_index("status", background=True)

    snapshots = get_snapshots_collection()
    await snapshots.create_index("mission_id", background=True)
    await snapshots.create_index([("created_at", DESCENDING)], background=True)

    beliefs = get_beliefs_collection()
    await beliefs.create_index(
        [("mission_id", ASCENDING), ("key", ASCENDING)],
        unique=True,
        background=True,
    )
    await beliefs.create_index(
        [("mission_id", ASCENDING), ("updated_at", ASCENDING)],
        background=True,
    )

    metrics = get_metrics_collection()
    await metrics.create_index("mission_id", background=True)
    await metrics.create_index(
        [("mission_id", ASCENDING), ("created_at", ASCENDING)],
        background=True,
    )

    users = get_users_collection()
    await users.create_index("user_id", unique=True, background=True)
    await users.create_index("email", unique=True, background=True)

    log.info("indexes_ensured")


# --------------------------------------------------------------------------- #
# Low-level helpers: retry, exception translation, model hydration
# --------------------------------------------------------------------------- #
@retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    retry=retry_if_exception_type(PyMongoError),
    reraise=True,
)
async def _run_with_retry(op: Callable[[], Awaitable[Any]]) -> Any:
    """Run a Motor operation with the standard write retry policy."""
    return await op()


async def _execute_write(op: Callable[[], Awaitable[Any]], *, error_ctx: dict) -> Any:
    """Execute a write op with retries, translating failures to typed errors.

    Args:
        op: Zero-arg callable returning the Motor write coroutine.
        error_ctx: Structured context for logging and the raised error.

    Returns:
        The Motor operation result.

    Raises:
        WriteFailedError: If the write fails after exhausting retries.
    """
    try:
        return await _run_with_retry(op)
    except PyMongoError as exc:
        log.error("mongo_write_failed", error=str(exc), **error_ctx)
        raise WriteFailedError(
            "MongoDB write failed", context=error_ctx, recoverable=True
        ) from exc


async def _execute_read(op: Callable[[], Awaitable[Any]], *, error_ctx: dict) -> Any:
    """Execute a read op, translating Motor failures to a typed StateError.

    Args:
        op: Zero-arg callable returning the Motor read coroutine.
        error_ctx: Structured context for logging and the raised error.

    Returns:
        The Motor operation result.

    Raises:
        StateError: If the read fails.
    """
    try:
        return await op()
    except PyMongoError as exc:
        log.error("mongo_read_failed", error=str(exc), **error_ctx)
        raise StateError(
            "MongoDB read failed", context=error_ctx, recoverable=True
        ) from exc


def _to_model(model_cls: type[T], doc: dict, *, error_ctx: dict) -> T:
    """Validate a raw Mongo document into a Pydantic model.

    Args:
        model_cls: Target model class.
        doc: Raw document (may include ``_id``, which is ignored).
        error_ctx: Structured context for logging and the raised error.

    Returns:
        The validated model instance.

    Raises:
        ValidationError: If the document does not satisfy the model contract.
    """
    try:
        return model_cls.model_validate(doc)
    except PydanticValidationError as exc:
        log.error("model_validation_failed", error=str(exc), **error_ctx)
        raise ValidationError(
            "Document failed validation", context=error_ctx
        ) from exc


async def _update_fields(
    coll: AsyncIOMotorCollection, query: dict, fields: dict, *, error_ctx: dict
) -> dict | None:
    """Apply a ``$set`` update then return the refreshed document (or None)."""
    await _execute_write(
        lambda: coll.update_one(query, {"$set": fields}), error_ctx=error_ctx
    )
    return await _execute_read(lambda: coll.find_one(query), error_ctx=error_ctx)


# --------------------------------------------------------------------------- #
# Missions
# --------------------------------------------------------------------------- #
async def create_mission(
    goal: str, mission_type: MissionType, context: dict | None = None
) -> Mission:
    """Insert a new mission.

    Args:
        goal: Raw user goal text.
        mission_type: ``INCIDENT_RESPONSE`` or ``GENERAL``.
        context: Optional initial context payload.

    Returns:
        The created :class:`Mission`.
    """
    mission = Mission(goal=goal, mission_type=mission_type, context=context or {})
    ctx = {"mission_id": mission.mission_id, "op": "create_mission"}
    await _execute_write(
        lambda: get_missions_collection().insert_one(mission.model_dump()),
        error_ctx=ctx,
    )
    log.info("mission_created", mission_id=mission.mission_id, mission_type=mission_type)
    return mission


async def get_mission(mission_id: str) -> Mission | None:
    """Fetch a mission by id.

    Args:
        mission_id: Mission identifier.

    Returns:
        The :class:`Mission`, or ``None`` if it does not exist.
    """
    ctx = {"mission_id": mission_id, "op": "get_mission"}
    doc = await _execute_read(
        lambda: get_missions_collection().find_one({"mission_id": mission_id}),
        error_ctx=ctx,
    )
    if doc is None:
        log.info("mission_not_found", mission_id=mission_id)
        return None
    return _to_model(Mission, doc, error_ctx=ctx)


async def find_mission_by_problem_id(problem_id: str) -> Mission | None:
    """Return the most recent mission whose context carries ``problem_id``.

    Used to correlate a later Dynatrace webhook (e.g. PROBLEM_RESOLVED) back to
    the mission opened for that problem.

    Args:
        problem_id: Dynatrace problem id stored in ``mission.context``.

    Returns:
        The matching :class:`Mission`, or ``None``.
    """
    ctx = {"problem_id": problem_id, "op": "find_mission_by_problem_id"}
    docs = await _execute_read(
        lambda: get_missions_collection()
        .find({"context.problem_id": problem_id})
        .sort("created_at", -1)
        .limit(1)
        .to_list(length=1),
        error_ctx=ctx,
    )
    if not docs:
        return None
    return _to_model(Mission, docs[0], error_ctx=ctx)


async def update_mission_status(mission_id: str, status: MissionStatus) -> Mission:
    """Update a mission's status and ``updated_at``.

    Args:
        mission_id: Mission identifier.
        status: New mission status.

    Returns:
        The refreshed :class:`Mission`.

    Raises:
        DocumentNotFoundError: If no mission matches ``mission_id``.
    """
    ctx = {"mission_id": mission_id, "op": "update_mission_status"}
    fields = {"status": status, "updated_at": datetime.utcnow()}
    doc = await _update_fields(
        get_missions_collection(), {"mission_id": mission_id}, fields, error_ctx=ctx
    )
    if doc is None:
        raise DocumentNotFoundError("Mission not found", context=ctx)
    log.info("mission_status_updated", mission_id=mission_id, status=status)
    return _to_model(Mission, doc, error_ctx=ctx)


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
async def create_task(
    mission_id: str,
    agent_role: AgentRole,
    description: str,
    depends_on: list[str] | None = None,
) -> Task:
    """Insert a task and link it to its mission's ``task_ids``.

    Args:
        mission_id: Owning mission identifier.
        agent_role: Role responsible for the task.
        description: Human-readable task description.
        depends_on: Task ids that must complete first.

    Returns:
        The created :class:`Task`.
    """
    task = Task(
        mission_id=mission_id,
        agent_role=agent_role,
        description=description,
        depends_on=depends_on or [],
    )
    ctx = {"task_id": task.task_id, "mission_id": mission_id, "op": "create_task"}
    await _execute_write(
        lambda: get_tasks_collection().insert_one(task.model_dump()), error_ctx=ctx
    )
    await _execute_write(
        lambda: get_missions_collection().update_one(
            {"mission_id": mission_id},
            {"$push": {"task_ids": task.task_id}, "$set": {"updated_at": datetime.utcnow()}},
        ),
        error_ctx=ctx,
    )
    log.info("task_created", task_id=task.task_id, mission_id=mission_id)
    return task


async def get_task(task_id: str) -> Task | None:
    """Fetch a task by id.

    Args:
        task_id: Task identifier.

    Returns:
        The :class:`Task`, or ``None`` if it does not exist.
    """
    ctx = {"task_id": task_id, "op": "get_task"}
    doc = await _execute_read(
        lambda: get_tasks_collection().find_one({"task_id": task_id}), error_ctx=ctx
    )
    if doc is None:
        log.info("task_not_found", task_id=task_id)
        return None
    return _to_model(Task, doc, error_ctx=ctx)


async def update_task_status(
    task_id: str,
    status: TaskStatus,
    *,
    result: dict | None = None,
    error: str | None = None,
    retry_count: int | None = None,
) -> Task:
    """Update a task's status and optional result/error/retry fields.

    Args:
        task_id: Task identifier.
        status: New task status.
        result: Optional result payload to store.
        error: Optional error message to store.
        retry_count: Optional new retry count.

    Returns:
        The refreshed :class:`Task`.

    Raises:
        DocumentNotFoundError: If no task matches ``task_id``.
    """
    fields: dict = {"status": status, "updated_at": datetime.utcnow()}
    if result is not None:
        fields["result"] = result
    if error is not None:
        fields["error"] = error
    if retry_count is not None:
        fields["retry_count"] = retry_count
    ctx = {"task_id": task_id, "op": "update_task_status"}
    doc = await _update_fields(
        get_tasks_collection(), {"task_id": task_id}, fields, error_ctx=ctx
    )
    if doc is None:
        raise DocumentNotFoundError("Task not found", context=ctx)
    log.info("task_status_updated", task_id=task_id, status=status)
    return _to_model(Task, doc, error_ctx=ctx)


async def get_tasks_for_mission(mission_id: str) -> list[Task]:
    """Return all tasks belonging to a mission.

    Args:
        mission_id: Owning mission identifier.

    Returns:
        List of :class:`Task` (possibly empty).
    """
    ctx = {"mission_id": mission_id, "op": "get_tasks_for_mission"}
    docs = await _execute_read(
        lambda: get_tasks_collection().find({"mission_id": mission_id}).to_list(length=None),
        error_ctx=ctx,
    )
    return [_to_model(Task, d, error_ctx=ctx) for d in docs]


async def add_tasks(tasks: list[Task]) -> None:
    """Persist pre-built tasks (preserving ids and dependencies) and link them.

    Used by the planner/orchestrator to insert a whole plan at once. All tasks
    must belong to the same mission.

    Args:
        tasks: Task models to insert; their ids/``depends_on`` are kept intact.
    """
    if not tasks:
        return
    mission_id = tasks[0].mission_id
    task_ids = [t.task_id for t in tasks]
    ctx = {"mission_id": mission_id, "op": "add_tasks", "count": len(tasks)}
    await _execute_write(
        lambda: get_tasks_collection().insert_many([t.model_dump() for t in tasks]), error_ctx=ctx
    )
    await _execute_write(
        lambda: get_missions_collection().update_one(
            {"mission_id": mission_id},
            {"$push": {"task_ids": {"$each": task_ids}}, "$set": {"updated_at": datetime.utcnow()}},
        ),
        error_ctx=ctx,
    )
    log.info("tasks_added", mission_id=mission_id, count=len(tasks))


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
async def create_action(
    mission_id: str, action_type: ActionType, payload: dict | None = None
) -> Action:
    """Insert a new action pending human approval.

    Args:
        mission_id: Owning mission identifier.
        action_type: Type of external side effect.
        payload: Action-specific data.

    Returns:
        The created :class:`Action`.
    """
    action = Action(
        mission_id=mission_id, action_type=action_type, payload=payload or {}
    )
    ctx = {"action_id": action.action_id, "mission_id": mission_id, "op": "create_action"}
    await _execute_write(
        lambda: get_actions_collection().insert_one(action.model_dump()), error_ctx=ctx
    )
    log.info("action_created", action_id=action.action_id, action_type=action_type)
    return action


async def get_action(action_id: str) -> Action | None:
    """Fetch an action by id.

    Args:
        action_id: Action identifier.

    Returns:
        The :class:`Action`, or ``None`` if it does not exist.
    """
    ctx = {"action_id": action_id, "op": "get_action"}
    doc = await _execute_read(
        lambda: get_actions_collection().find_one({"action_id": action_id}), error_ctx=ctx
    )
    if doc is None:
        log.info("action_not_found", action_id=action_id)
        return None
    return _to_model(Action, doc, error_ctx=ctx)


async def update_action_status(
    action_id: str,
    status: ActionStatus,
    *,
    approved_by: str | None = None,
    executed_at: datetime | None = None,
) -> Action:
    """Update an action's status and optional approval/execution fields.

    Args:
        action_id: Action identifier.
        status: New action status.
        approved_by: Identity that approved the action, if any.
        executed_at: Execution timestamp, if the action ran.

    Returns:
        The refreshed :class:`Action`.

    Raises:
        DocumentNotFoundError: If no action matches ``action_id``.
    """
    fields: dict = {"status": status}
    if approved_by is not None:
        fields["approved_by"] = approved_by
    if executed_at is not None:
        fields["executed_at"] = executed_at
    ctx = {"action_id": action_id, "op": "update_action_status"}
    doc = await _update_fields(
        get_actions_collection(), {"action_id": action_id}, fields, error_ctx=ctx
    )
    if doc is None:
        raise DocumentNotFoundError("Action not found", context=ctx)
    log.info("action_status_updated", action_id=action_id, status=status)
    return _to_model(Action, doc, error_ctx=ctx)


async def get_actions_for_mission(
    mission_id: str, status: ActionStatus | None = None
) -> list[Action]:
    """Return a mission's actions, optionally filtered by status.

    Args:
        mission_id: Owning mission identifier.
        status: Optional action status to filter by (e.g. ``"pending"``).

    Returns:
        List of :class:`Action` (possibly empty).
    """
    query: dict = {"mission_id": mission_id}
    if status is not None:
        query["status"] = status
    ctx = {"mission_id": mission_id, "op": "get_actions_for_mission"}
    docs = await _execute_read(
        lambda: get_actions_collection().find(query).to_list(length=None), error_ctx=ctx
    )
    return [_to_model(Action, d, error_ctx=ctx) for d in docs]


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
async def create_user(email: str, password_hash: str) -> User:
    """Create a user; raise AuthError if the (normalized) email already exists.

    Args:
        email: Raw email address (will be trimmed and lowercased).
        password_hash: Pre-hashed password string.

    Returns:
        The created :class:`User`.

    Raises:
        AuthError: If the normalized email is already registered.
    """
    normalized = email.strip().lower()
    if await get_user_by_email(normalized) is not None:
        raise AuthError("email already registered", context={"email": normalized})
    user = User(email=normalized, password_hash=password_hash)
    ctx = {"user_id": user.user_id, "op": "create_user"}
    await _execute_write(
        lambda: get_users_collection().insert_one(user.model_dump()), error_ctx=ctx
    )
    log.info("user_created", user_id=user.user_id)
    return user


async def get_user_by_email(email: str) -> User | None:
    """Return the user with this (normalized) email, or None.

    Args:
        email: Email address to look up (will be trimmed and lowercased).

    Returns:
        The :class:`User`, or ``None`` if not found.
    """
    normalized = email.strip().lower()
    ctx = {"email": normalized, "op": "get_user_by_email"}
    doc = await _execute_read(
        lambda: get_users_collection().find_one({"email": normalized}), error_ctx=ctx
    )
    return _to_model(User, doc, error_ctx=ctx) if doc else None


async def get_user(user_id: str) -> User | None:
    """Return the user with this id, or None.

    Args:
        user_id: User identifier.

    Returns:
        The :class:`User`, or ``None`` if not found.
    """
    ctx = {"user_id": user_id, "op": "get_user"}
    doc = await _execute_read(
        lambda: get_users_collection().find_one({"user_id": user_id}), error_ctx=ctx
    )
    return _to_model(User, doc, error_ctx=ctx) if doc else None


# --------------------------------------------------------------------------- #
# Beliefs (working memory)
# --------------------------------------------------------------------------- #
async def write_belief(
    mission_id: str,
    key: str,
    label: str,
    value: str,
    confidence: float = 0.5,
    op: str = "write",
) -> Belief:
    """Upsert a belief by (mission_id, key), bump version, emit BELIEF_UPDATED.

    If a belief for the given key already exists, its ``belief_id`` is kept and
    ``version`` is incremented. A new belief starts at version 0.

    Args:
        mission_id: Owning mission identifier.
        key: Stable identity key (e.g. ``"root_cause"``).
        label: Display label (e.g. ``"Root cause"``).
        value: Display value (e.g. ``"#4827 · conn-pool"``).
        confidence: Confidence score in [0.0, 1.0].
        op: Update operation: ``write``, ``update``, ``confirm``, or ``contradict``.

    Returns:
        The upserted :class:`Belief`.
    """
    ctx = {"mission_id": mission_id, "key": key, "op": "write_belief"}
    existing_doc = await _execute_read(
        lambda: get_beliefs_collection().find_one({"mission_id": mission_id, "key": key}),
        error_ctx=ctx,
    )
    now = datetime.utcnow()
    if existing_doc is not None:
        existing = _to_model(Belief, existing_doc, error_ctx=ctx)
        new_version = existing.version + 1
        belief_id = existing.belief_id
        created_at = existing.created_at
    else:
        new_version = 0
        belief_id = str(uuid4())
        created_at = now

    belief = Belief(
        belief_id=belief_id,
        mission_id=mission_id,
        key=key,
        label=label,
        value=value,
        confidence=confidence,
        op=op,  # type: ignore[arg-type]
        version=new_version,
        created_at=created_at,
        updated_at=now,
    )
    await _execute_write(
        lambda: get_beliefs_collection().replace_one(
            {"mission_id": mission_id, "key": key},
            belief.model_dump(),
            upsert=True,
        ),
        error_ctx=ctx,
    )
    await emit_event(
        mission_id,
        "BELIEF_UPDATED",
        {
            "key": key,
            "label": label,
            "value": value,
            "confidence": confidence,
            "op": op,
            "version": new_version,
        },
        "agent",
    )
    log.info("belief_written", mission_id=mission_id, key=key, version=new_version)
    return belief


async def get_beliefs(mission_id: str) -> list[Belief]:
    """Return all beliefs for a mission, ordered by updated_at ascending.

    Args:
        mission_id: Owning mission identifier.

    Returns:
        List of :class:`Belief` (possibly empty).
    """
    ctx = {"mission_id": mission_id, "op": "get_beliefs"}
    docs = await _execute_read(
        lambda: get_beliefs_collection()
        .find({"mission_id": mission_id})
        .sort([("updated_at", ASCENDING), ("version", ASCENDING), ("_id", ASCENDING)])
        .to_list(length=None),
        error_ctx=ctx,
    )
    return [_to_model(Belief, d, error_ctx=ctx) for d in docs]


# --------------------------------------------------------------------------- #
# Metrics (time-series samples)
# --------------------------------------------------------------------------- #
async def record_metric(
    mission_id: str,
    label: str,
    value: float,
    unit: str = "",
    baseline: float | None = None,
) -> None:
    """Insert one MetricSample and emit a METRIC_SAMPLE event.

    Args:
        mission_id: Owning mission identifier.
        label: Metric label (e.g. ``"checkout · error rate"``).
        value: Sampled metric value.
        unit: Optional unit string (``"ms"``, ``"%"``, etc.).
        baseline: Optional baseline value for comparison.
    """
    sample = MetricSample(
        mission_id=mission_id,
        label=label,
        value=value,
        unit=unit,
        baseline=baseline,
    )
    ctx = {"mission_id": mission_id, "label": label, "op": "record_metric"}
    await _execute_write(
        lambda: get_metrics_collection().insert_one(sample.model_dump()),
        error_ctx=ctx,
    )
    await emit_event(
        mission_id,
        "METRIC_SAMPLE",
        {"label": label, "value": value, "unit": unit, "baseline": baseline},
        "agent",
    )
    log.info("metric_recorded", mission_id=mission_id, label=label, value=value)


async def get_metric_series(
    mission_id: str, limit: int = DEFAULT_METRIC_SERIES_LIMIT
) -> list[MetricSample]:
    """Return the most recent samples for a mission, oldest to newest.

    Args:
        mission_id: Owning mission identifier.
        limit: Maximum number of samples to return (default 48).

    Returns:
        List of :class:`MetricSample` ordered by ``created_at`` ascending.
    """
    ctx = {"mission_id": mission_id, "op": "get_metric_series"}
    # Fetch the N most-recent (desc created_at, desc _id for tiebreaking), then
    # reverse in Python so the caller receives oldest→newest order.
    docs = await _execute_read(
        lambda: get_metrics_collection()
        .find({"mission_id": mission_id})
        .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
        .limit(limit)
        .to_list(length=limit),
        error_ctx=ctx,
    )
    docs.reverse()
    return [_to_model(MetricSample, d, error_ctx=ctx) for d in docs]


# --------------------------------------------------------------------------- #
# Missions — fleet listing
# --------------------------------------------------------------------------- #
async def list_recent_missions(limit: int = DEFAULT_LIST_MISSIONS_LIMIT) -> list[Mission]:
    """Return the most recently updated missions for the fleet roster.

    Args:
        limit: Maximum number of missions to return.

    Returns:
        List of :class:`Mission` ordered by ``updated_at`` descending.
    """
    ctx = {"op": "list_recent_missions", "limit": limit}
    docs = await _execute_read(
        lambda: get_missions_collection()
        .find()
        .sort("updated_at", DESCENDING)
        .limit(limit)
        .to_list(length=limit),
        error_ctx=ctx,
    )
    return [_to_model(Mission, d, error_ctx=ctx) for d in docs]


# --------------------------------------------------------------------------- #
# Events + snapshots
# --------------------------------------------------------------------------- #
async def emit_event(
    mission_id: str,
    event_type: str,
    payload: dict,
    source: EventSource,
    task_id: str | None = None,
) -> None:
    """Append an event to the audit trail (ARCHITECTURE.md section 2).

    Args:
        mission_id: Mission the event belongs to.
        event_type: Snake-case event type (e.g. ``TASK_STARTED``).
        payload: Structured event data.
        source: Originating component.
        task_id: Associated task id, if any.
    """
    event = Event(
        mission_id=mission_id,
        task_id=task_id,
        event_type=event_type,
        payload=payload,
        source=source,
    )
    ctx = {"mission_id": mission_id, "event_type": event_type, "op": "emit_event"}
    await _execute_write(
        lambda: get_events_collection().insert_one(event.model_dump()), error_ctx=ctx
    )
    log.info("event_emitted", mission_id=mission_id, event_type=event_type, task_id=task_id)
    await _broadcast_event(mission_id, event)


async def _broadcast_event(mission_id: str, event: Event) -> None:
    """Push a persisted event to connected dashboards (best-effort).

    Imported lazily so the state layer stays decoupled from the web layer at
    import time. Any failure here is logged and swallowed: the event is already
    persisted and delivery must never break persistence.
    """
    try:
        from websocket_manager import connection_manager

        await connection_manager.broadcast(mission_id, event.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 — broadcast is best-effort, never fatal
        log.warning("event_broadcast_failed", mission_id=mission_id, error=str(exc))


async def get_recent_events_for_mission(
    mission_id: str, limit: int = DEFAULT_RECENT_EVENTS_LIMIT
) -> list[Event]:
    """Return a mission's most recent events, newest first.

    Args:
        mission_id: Owning mission identifier.
        limit: Maximum number of events to return.

    Returns:
        List of :class:`Event` ordered by ``created_at`` descending.
    """
    ctx = {"mission_id": mission_id, "op": "get_recent_events_for_mission"}
    docs = await _execute_read(
        lambda: get_events_collection()
        .find({"mission_id": mission_id})
        .sort("created_at", -1)
        .limit(limit)
        .to_list(length=limit),
        error_ctx=ctx,
    )
    return [_to_model(Event, d, error_ctx=ctx) for d in docs]


async def get_recent_events_excluding(
    mission_id: str, exclude_types: list[str], limit: int = DEFAULT_RECENT_EVENTS_LIMIT
) -> list[Event]:
    """Return recent events of meaningful types, newest first.

    Excludes high-frequency internal event types so the narrative survives in a
    fixed window regardless of polling volume (used by the dashboard feed).

    Args:
        mission_id: Owning mission identifier.
        exclude_types: Event types to omit (e.g. per-poll monitoring events).
        limit: Maximum number of events to return.

    Returns:
        List of :class:`Event` ordered by ``created_at`` descending.
    """
    ctx = {"mission_id": mission_id, "op": "get_recent_events_excluding"}
    docs = await _execute_read(
        lambda: get_events_collection()
        .find({"mission_id": mission_id, "event_type": {"$nin": exclude_types}})
        .sort("created_at", -1)
        .limit(limit)
        .to_list(length=limit),
        error_ctx=ctx,
    )
    return [_to_model(Event, d, error_ctx=ctx) for d in docs]


async def get_events_page(mission_id: str, limit: int, offset: int) -> list[Event]:
    """Return a chronological page of a mission's events.

    Args:
        mission_id: Owning mission identifier.
        limit: Page size.
        offset: Number of events to skip from the start.

    Returns:
        List of :class:`Event` ordered by ``created_at`` ascending.
    """
    ctx = {"mission_id": mission_id, "op": "get_events_page"}
    docs = await _execute_read(
        lambda: get_events_collection()
        .find({"mission_id": mission_id})
        .sort("created_at", 1)
        .skip(offset)
        .limit(limit)
        .to_list(length=limit),
        error_ctx=ctx,
    )
    return [_to_model(Event, d, error_ctx=ctx) for d in docs]


async def count_events_for_mission(mission_id: str) -> int:
    """Return the total number of events recorded for a mission."""
    ctx = {"mission_id": mission_id, "op": "count_events_for_mission"}
    return await _execute_read(
        lambda: get_events_collection().count_documents({"mission_id": mission_id}), error_ctx=ctx
    )


async def get_latest_event_of_type(mission_id: str, event_type: str) -> Event | None:
    """Return the most recent event of a given type, regardless of volume.

    Used for stable derived values (e.g. the latest risk score) that must not
    fall out of a fixed recent-events window as activity accumulates.

    Args:
        mission_id: Owning mission identifier.
        event_type: Event type to look up.

    Returns:
        The latest matching :class:`Event`, or ``None`` if there is none.
    """
    ctx = {"mission_id": mission_id, "event_type": event_type, "op": "get_latest_event_of_type"}
    docs = await _execute_read(
        lambda: get_events_collection()
        .find({"mission_id": mission_id, "event_type": event_type})
        .sort("created_at", -1)
        .limit(1)
        .to_list(length=1),
        error_ctx=ctx,
    )
    if not docs:
        return None
    return _to_model(Event, docs[0], error_ctx=ctx)


def _summarize_state(state: MissionState) -> dict:
    """Build a compact summary dict of a MissionState for snapshotting."""
    return {
        "mission_status": state.mission.status,
        "task_count": len(state.tasks),
        "task_status_counts": dict(Counter(t.status for t in state.tasks)),
        "recent_event_count": len(state.recent_events),
    }


async def create_snapshot(
    state: MissionState,
    cycle: int,
    failure_injections_active: list[dict] | None = None,
) -> Snapshot:
    """Persist a summary of the current mission state for a loop cycle.

    Args:
        state: Aggregated mission state to summarize.
        cycle: Orchestration loop cycle number.
        failure_injections_active: Active failure injections at snapshot time.

    Returns:
        The created :class:`Snapshot`.
    """
    snapshot = Snapshot(
        mission_id=state.mission.mission_id,
        cycle=cycle,
        state_summary=_summarize_state(state),
        failure_injections_active=failure_injections_active or [],
    )
    ctx = {"mission_id": snapshot.mission_id, "cycle": cycle, "op": "create_snapshot"}
    await _execute_write(
        lambda: get_snapshots_collection().insert_one(snapshot.model_dump()), error_ctx=ctx
    )
    log.info("snapshot_created", mission_id=snapshot.mission_id, cycle=cycle)
    return snapshot


async def get_latest_snapshot(mission_id: str) -> Snapshot | None:
    """Return a mission's most recent snapshot (highest cycle), or None.

    Args:
        mission_id: Owning mission identifier.

    Returns:
        The latest :class:`Snapshot`, or ``None`` if none exist yet.
    """
    ctx = {"mission_id": mission_id, "op": "get_latest_snapshot"}
    docs = await _execute_read(
        lambda: get_snapshots_collection()
        .find({"mission_id": mission_id})
        .sort("cycle", -1)
        .limit(1)
        .to_list(length=1),
        error_ctx=ctx,
    )
    if not docs:
        return None
    return _to_model(Snapshot, docs[0], error_ctx=ctx)


async def get_mission_state(mission_id: str) -> MissionState | None:
    """Assemble the aggregate observation view for a mission.

    Bundles the mission with its tasks, recent events, current beliefs
    (working memory), and recent metric samples.

    Args:
        mission_id: Mission identifier.

    Returns:
        A :class:`MissionState` bundling mission, tasks, recent events,
        beliefs, and metric series; or ``None`` if the mission does not exist.
    """
    mission = await get_mission(mission_id)
    if mission is None:
        return None
    tasks = await get_tasks_for_mission(mission_id)
    recent_events = await get_recent_events_for_mission(mission_id)
    beliefs = await get_beliefs(mission_id)
    metric_series = await get_metric_series(mission_id)
    return MissionState(
        mission=mission,
        tasks=tasks,
        recent_events=recent_events,
        beliefs=beliefs,
        metric_series=metric_series,
    )
