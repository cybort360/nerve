"""Task dependency graph for a mission.

Builds a DAG from tasks' ``depends_on`` links and answers the questions the
orchestration loop needs: which tasks are runnable now, whether the mission is
finished, and what the longest dependency chain (critical path) is.
"""

from __future__ import annotations

from state.models import Task

_TERMINAL_TASK_STATUSES = ("completed", "failed")


class MissionGraph:
    """A directed acyclic graph over a mission's tasks."""

    def __init__(self, tasks: list[Task]) -> None:
        """Build the graph from a list of tasks.

        Args:
            tasks: All tasks belonging to the mission.
        """
        self._tasks = tasks
        self._by_id: dict[str, Task] = {t.task_id: t for t in tasks}

    def get_ready_tasks(self) -> list[Task]:
        """Return pending tasks whose dependencies have all completed.

        Returns:
            Tasks that can be executed now (in declaration order).
        """
        return [t for t in self._tasks if t.status == "pending" and self._deps_satisfied(t)]

    def is_complete(self) -> bool:
        """Return True if every task has reached a terminal status.

        Returns:
            True when there is at least one task and all are completed or failed.
        """
        return bool(self._tasks) and all(t.status in _TERMINAL_TASK_STATUSES for t in self._tasks)

    def get_critical_path(self) -> list[Task]:
        """Return the longest dependency chain through the graph.

        Returns:
            The tasks on the longest path, ordered from root to leaf. Empty if
            there are no tasks.
        """
        memo: dict[str, list[str]] = {}
        best: list[str] = []
        for task in self._tasks:
            chain = self._longest_ending_at(task.task_id, memo, set())
            if len(chain) > len(best):
                best = chain
        return [self._by_id[task_id] for task_id in best]

    def _deps_satisfied(self, task: Task) -> bool:
        """Return True if all of a task's dependencies are completed."""
        for dep_id in task.depends_on:
            dep = self._by_id.get(dep_id)
            if dep is None or dep.status != "completed":
                return False
        return True

    def _longest_ending_at(self, task_id: str, memo: dict[str, list[str]], stack: set[str]) -> list[str]:
        """Compute the longest path ending at ``task_id`` (cycle-safe).

        Args:
            task_id: Node to compute the longest incoming chain for.
            memo: Cache of already-computed chains by task id.
            stack: Ids on the current recursion path, to break cycles.

        Returns:
            Task ids on the longest path ending at ``task_id``.
        """
        if task_id in memo:
            return memo[task_id]
        if task_id in stack or task_id not in self._by_id:
            return []
        stack.add(task_id)
        best_prefix: list[str] = []
        for dep_id in self._by_id[task_id].depends_on:
            prefix = self._longest_ending_at(dep_id, memo, stack)
            if len(prefix) > len(best_prefix):
                best_prefix = prefix
        stack.discard(task_id)
        memo[task_id] = best_prefix + [task_id]
        return memo[task_id]
