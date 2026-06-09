"""Long-term incident memory for NERVE.

Stores resolved incidents in Vertex AI Agent Platform Memory Bank and retrieves
similar past incidents to enrich reasoning over time. Opt-in via
``MEMORY_BANK_ENABLED`` (see :mod:`memory.incident_memory`).
"""
