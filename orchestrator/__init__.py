"""Orchestration layer for NERVE.

Holds the mission dependency graph (:mod:`orchestrator.mission_graph`), the goal
decomposer (:mod:`orchestrator.planner`), and the main async execution loop
(:mod:`orchestrator.orchestrator`). The loop is the only component that advances
``missions.status`` (CLAUDE.md invariant 4).
"""
