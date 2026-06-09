"""AuditorAgent: validates mission-state consistency after each cycle.

Checks for stalled tasks, contradictory task records, and mission/task status
mismatches. Emits an AGENT_OBSERVATION event per issue and returns the list of
findings (an empty list means the state is clean).
"""

from __future__ import annotations

from datetime import datetime
from time import perf_counter

from agents.base_agent import EVENT_AGENT_OBSERVATION, SOURCE_AGENT, AgentResult, BaseAgent
from state import database as db
from state.models import Mission, MissionState, Task

# A task stuck in_progress beyond this many seconds is considered stalled.
# Local tuning constant rather than env config (see RiskAgent note).
STALE_TASK_SECONDS = 300


class AuditorAgent(BaseAgent):
    """Flags inconsistencies between tasks and mission status."""

    def __init__(self, mission_id: str) -> None:
        """Initialize the auditor for a mission."""
        super().__init__(name="auditor", mission_id=mission_id)

    async def run(self, context: dict) -> AgentResult:
        """Audit ``context['state']`` and emit an observation per finding.

        Args:
            context: Must contain ``state`` (MissionState).

        Returns:
            AgentResult whose output holds the list of findings.
        """
        start = perf_counter()
        state: MissionState = context["state"]
        findings = self.audit(state)
        for finding in findings:
            await db.emit_event(
                self.mission_id,
                EVENT_AGENT_OBSERVATION,
                finding,
                SOURCE_AGENT,
                task_id=finding.get("task_id"),
            )
        self._log.info("audit_complete", finding_count=len(findings))
        return self._finish("success", {"findings": findings}, [f"{len(findings)} findings"], start)

    def observe(self, state: MissionState) -> list[str]:
        """Note how many tasks are currently in progress."""
        in_progress = sum(1 for t in state.tasks if t.status == "in_progress")
        return [f"{in_progress} tasks in progress"]

    def audit(self, state: MissionState) -> list[dict]:
        """Run all consistency checks and return the combined findings.

        Args:
            state: Aggregated mission state to validate.

        Returns:
            List of finding dicts (empty when state is consistent).
        """
        findings: list[dict] = []
        findings.extend(self._check_stale(state.tasks))
        findings.extend(self._check_contradictions(state.tasks))
        findings.extend(self._check_mission_consistency(state.mission, state.tasks))
        return findings

    @staticmethod
    def _check_stale(tasks: list[Task]) -> list[dict]:
        """Flag tasks stuck in_progress longer than the stale threshold."""
        now = datetime.utcnow()
        findings: list[dict] = []
        for task in tasks:
            if task.status != "in_progress":
                continue
            age = (now - task.updated_at).total_seconds()
            if age > STALE_TASK_SECONDS:
                findings.append(
                    {"type": "stale_task", "task_id": task.task_id, "age_seconds": age}
                )
        return findings

    @staticmethod
    def _check_contradictions(tasks: list[Task]) -> list[dict]:
        """Flag completed tasks carrying an error or depending on a failed task."""
        status_by_id = {t.task_id: t.status for t in tasks}
        findings: list[dict] = []
        for task in tasks:
            if task.status == "completed" and task.error:
                findings.append(
                    {"type": "completed_with_error", "task_id": task.task_id, "detail": task.error}
                )
            if task.status == "completed":
                failed_deps = [d for d in task.depends_on if status_by_id.get(d) == "failed"]
                if failed_deps:
                    findings.append(
                        {"type": "dependency_violation", "task_id": task.task_id, "failed_deps": failed_deps}
                    )
        return findings

    @staticmethod
    def _check_mission_consistency(mission: Mission, tasks: list[Task]) -> list[dict]:
        """Flag mismatches between mission status and aggregate task status."""
        findings: list[dict] = []
        open_tasks = [t.task_id for t in tasks if t.status != "completed"]
        if mission.status == "resolved" and open_tasks:
            findings.append(
                {"type": "resolved_with_open_tasks", "detail": open_tasks}
            )
        if mission.status == "executing" and tasks and not open_tasks:
            findings.append({"type": "should_be_resolved", "detail": "all tasks completed"})
        return findings
