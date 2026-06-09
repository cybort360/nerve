"""State layer: Pydantic models and the Motor-backed persistence API.

All MongoDB access in NERVE goes through this package. Agents and the
orchestrator import the CRUD functions in :mod:`state.database`; they never
touch Motor directly (see CLAUDE.md invariant 1).
"""
