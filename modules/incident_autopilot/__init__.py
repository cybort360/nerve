"""Incident Autopilot: the INCIDENT_RESPONSE mission type.

Correlates a Dynatrace anomaly with recent GitLab deployments, files an incident
issue, proposes a (human-approved) rollback, and monitors the service until its
error rate returns to baseline. See CLAUDE.md "Incident Autopilot Module".
"""

from modules.incident_autopilot.workflow import (
    CorrelationResult,
    IncidentAutopilotWorkflow,
)

__all__ = ["IncidentAutopilotWorkflow", "CorrelationResult"]
