"""Incident memory backed by Vertex AI Agent Platform Memory Bank.

Stores resolved incidents (with structured metadata) and retrieves similar past
incidents to feed back into reasoning. Opt-in: when ``MEMORY_BANK_ENABLED`` is
false (or no ``MEMORY_BANK_ID`` is set), ``store_incident`` is a no-op and
``retrieve_similar`` returns ``[]``.

Memory is best-effort and must never break incident response: all Memory Bank
errors are caught, logged, and swallowed (store) or degraded to empty (retrieve).

The Memory Bank client is injectable (``IncidentMemory(client=...)``) so it can
be mocked in tests; the default client wraps the real Vertex SDK lazily.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from config import settings

log = structlog.get_logger()


class MemoryEntry(BaseModel):
    """A single past-incident memory returned from the Memory Bank."""

    model_config = ConfigDict(extra="ignore")

    memory_id: str
    summary: str
    metadata: dict = Field(default_factory=dict)
    relevance_score: float = 0.0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MemoryBankClient(Protocol):
    """The async client surface :class:`IncidentMemory` depends on."""

    async def create_memory(self, content: str, metadata: dict) -> str:
        """Persist a memory and return its id."""
        ...

    async def retrieve_memories(self, query: str, top_k: int) -> list[dict]:
        """Return up to ``top_k`` memories relevant to ``query``."""
        ...


class IncidentMemory:
    """Stores and retrieves incident patterns via Vertex AI Memory Bank."""

    def __init__(self, client: MemoryBankClient | None = None) -> None:
        """Initialize the memory layer.

        Args:
            client: Memory Bank client; injected in tests. ``None`` builds the
                real Vertex adapter on first use.
        """
        self._client = client
        self._log = structlog.get_logger().bind(component="incident_memory")

    @property
    def enabled(self) -> bool:
        """Whether memory is active (feature flag + a configured bank id)."""
        return settings.memory_bank_enabled and bool(settings.memory_bank_id)

    async def store_incident(
        self,
        mission_id: str,
        problem: Any,
        deployment: Any,
        reasoning: str,
        outcome: dict,
    ) -> None:
        """Persist a resolved incident as a memory entry (no-op when disabled).

        Args:
            mission_id: Mission the incident belonged to.
            problem: Dynatrace problem detail.
            deployment: Correlated deployment (or ``None``).
            reasoning: The correlation reasoning text.
            outcome: Resolution details (``recommendation``, ``changed_files``,
                ``resolution_time_seconds``, ``status``).
        """
        if not self.enabled:
            self._log.debug("incident_memory_disabled_store_skipped", mission_id=mission_id)
            return
        content = self._summarize(problem, deployment, reasoning, outcome)
        metadata = self._build_metadata(mission_id, problem, outcome)
        try:
            memory_id = await self._get_client().create_memory(content, metadata)
            self._log.info("incident_memory_stored", mission_id=mission_id, memory_id=memory_id)
        except Exception as exc:  # noqa: BLE001 — memory is best-effort, never fatal
            self._log.warning("incident_memory_store_failed", mission_id=mission_id, error=str(exc))

    async def retrieve_similar(self, problem_description: str, limit: int = 3) -> list[MemoryEntry]:
        """Return up to ``limit`` past incidents similar to ``problem_description``.

        Args:
            problem_description: Free-text description of the current problem.
            limit: Maximum number of memories to return.

        Returns:
            A list of :class:`MemoryEntry` (empty when disabled or on failure).
        """
        if not self.enabled:
            return []
        try:
            raw = await self._get_client().retrieve_memories(problem_description, limit)
        except Exception as exc:  # noqa: BLE001 — degrade to no context on failure
            self._log.warning("incident_memory_retrieve_failed", error=str(exc))
            return []
        entries = [self._to_entry(item) for item in raw][:limit]
        self._log.info("incident_memory_retrieved", count=len(entries))
        return entries

    # ----------------------------------------------------------------- #
    # Helpers
    # ----------------------------------------------------------------- #
    def _get_client(self) -> MemoryBankClient:
        """Return the injected client, building the real adapter on demand."""
        if self._client is None:
            self._client = _VertexMemoryBankClient(
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
                memory_bank_id=settings.memory_bank_id,
            )
        return self._client

    @staticmethod
    def _summarize(problem: Any, deployment: Any, reasoning: str, outcome: dict) -> str:
        """Build the human-readable memory content for an incident."""
        service = (getattr(problem, "impacted_services", None) or ["unknown"])[0]
        dep = f"deployment {deployment.id} ({deployment.sha or deployment.ref})" if deployment else "no deployment"
        parts = [
            f"Incident '{getattr(problem, 'title', '')}' on service '{service}', correlated to {dep}.",
            reasoning.strip(),
            f"Recommendation: {outcome.get('recommendation', 'investigate')}.",
        ]
        rt = outcome.get("resolution_time_seconds")
        if rt is not None:
            parts.append(f"Resolution time: {rt}s.")
        return " ".join(p for p in parts if p)

    @staticmethod
    def _build_metadata(mission_id: str, problem: Any, outcome: dict) -> dict:
        """Build the structured metadata stored alongside the memory."""
        services = getattr(problem, "impacted_services", None) or []
        return {
            "mission_id": mission_id,
            "affected_service": services[0] if services else None,
            "changed_files": outcome.get("changed_files", []),
            "error_pattern": getattr(problem, "title", ""),
            "recommendation": outcome.get("recommendation"),
            "resolution_time_seconds": outcome.get("resolution_time_seconds"),
        }

    @staticmethod
    def _to_entry(raw: dict) -> MemoryEntry:
        """Map a raw Memory Bank result into a :class:`MemoryEntry`."""
        created = raw.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                created = datetime.utcnow()
        return MemoryEntry(
            memory_id=str(raw.get("memory_id") or raw.get("id") or ""),
            summary=str(raw.get("summary") or raw.get("content") or raw.get("fact") or ""),
            metadata=raw.get("metadata") or {},
            relevance_score=float(raw.get("relevance_score") or raw.get("score") or 0.0),
            created_at=created if isinstance(created, datetime) else datetime.utcnow(),
        )


class _VertexMemoryBankClient:
    """Adapter over the Vertex AI Agent Platform Memory Bank.

    UNVERIFIED against a live Vertex environment — the Memory Bank SDK surface is
    new and may differ; adjust ``_create``/``_retrieve`` to match your SDK
    version. Tests inject a fake client and never exercise this adapter.
    """

    def __init__(self, project: str, location: str, memory_bank_id: str) -> None:
        self._project = project
        self._location = location
        self._bank = memory_bank_id

    async def create_memory(self, content: str, metadata: dict) -> str:
        """Persist a memory (sync SDK call run off the event loop)."""
        return await asyncio.to_thread(self._create, content, metadata)

    async def retrieve_memories(self, query: str, top_k: int) -> list[dict]:
        """Retrieve relevant memories (sync SDK call run off the event loop)."""
        return await asyncio.to_thread(self._retrieve, query, top_k)

    def _create(self, content: str, metadata: dict) -> str:
        import vertexai  # lazy: heavy optional dependency

        vertexai.init(project=self._project, location=self._location)
        client = vertexai.Client()
        result = client.agent_engines.create_memory(
            name=self._bank, fact=content, scope=metadata
        )
        return str(getattr(result, "name", None) or getattr(result, "id", ""))

    def _retrieve(self, query: str, top_k: int) -> list[dict]:
        import vertexai  # lazy: heavy optional dependency

        vertexai.init(project=self._project, location=self._location)
        client = vertexai.Client()
        results = client.agent_engines.retrieve_memories(name=self._bank, query=query, top_k=top_k)
        return [
            {
                "memory_id": getattr(r, "name", ""),
                "summary": getattr(r, "fact", ""),
                "metadata": dict(getattr(r, "scope", None) or getattr(r, "metadata", None) or {}),
                "relevance_score": float(getattr(r, "distance", 0.0) or 0.0),
                "created_at": getattr(r, "create_time", None),
            }
            for r in results
        ]
