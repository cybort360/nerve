"""Unit tests for MissionGraph (orchestrator/mission_graph.py)."""

from __future__ import annotations

from orchestrator.mission_graph import MissionGraph
from state.models import Task

MISSION_ID = "m-1"


def _task(description: str, status: str = "pending", depends_on=None) -> Task:
    return Task(
        mission_id=MISSION_ID,
        agent_role="execution",
        description=description,
        status=status,
        depends_on=depends_on or [],
    )


def test_ready_tasks_with_no_dependencies():
    a, b = _task("a"), _task("b")
    graph = MissionGraph([a, b])
    assert {t.task_id for t in graph.get_ready_tasks()} == {a.task_id, b.task_id}


def test_ready_tasks_blocked_until_dependency_completes():
    a = _task("a")
    b = _task("b", depends_on=[a.task_id])
    assert MissionGraph([a, b]).get_ready_tasks() == [a]  # b blocked

    a.status = "completed"
    ready = MissionGraph([a, b]).get_ready_tasks()
    assert ready == [b]  # b now unblocked, a no longer pending


def test_is_complete():
    a = _task("a", status="completed")
    b = _task("b", status="failed")
    assert MissionGraph([a, b]).is_complete() is True
    assert MissionGraph([]).is_complete() is False  # no tasks != complete

    c = _task("c", status="in_progress")
    assert MissionGraph([a, c]).is_complete() is False


def test_critical_path_is_longest_chain():
    a = _task("a")
    b = _task("b", depends_on=[a.task_id])
    c = _task("c", depends_on=[b.task_id])
    standalone = _task("standalone")
    path = MissionGraph([a, b, c, standalone]).get_critical_path()
    assert [t.description for t in path] == ["a", "b", "c"]


def test_critical_path_handles_cycle_without_hanging():
    a = _task("a")
    b = _task("b")
    a.depends_on = [b.task_id]
    b.depends_on = [a.task_id]  # pathological cycle
    path = MissionGraph([a, b]).get_critical_path()
    assert len(path) >= 1  # terminates and returns something sane
