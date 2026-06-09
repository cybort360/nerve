"""Markdown templates for Incident Autopilot GitLab artifacts.

Pure formatting only — no I/O. Produces the incident issue body and the rollback
merge-request description, and exposes the canonical label set.
"""

from __future__ import annotations

from mcp_tools.dynatrace import DynatraceProblemDetail
from mcp_tools.gitlab import GitLabDeployment

_MAX_TIMELINE_EVENTS = 10
_FOOTER = "_Created automatically by NERVE Incident Autopilot._"


class IncidentTemplates:
    """Builds GitLab issue and rollback MR markdown for an incident."""

    #: Labels applied to every NERVE-created incident issue.
    LABELS = ["incident", "p1", "nerve-created"]

    @staticmethod
    def gitlab_issue_body(
        problem: DynatraceProblemDetail,
        deployment: GitLabDeployment | None,
        reasoning: str,
        *,
        external_context: str = "",
        grounding_sources: list[str] | None = None,
    ) -> str:
        """Render the incident issue body.

        Args:
            problem: Dynatrace problem detail (summary, timeline, impact).
            deployment: Correlated deployment, or ``None`` if none was found.
            reasoning: Gemini's correlation analysis text.
            external_context: Summary of Google Search grounding findings, if any.
            grounding_sources: URLs Gemini cited during grounded reasoning.

        Returns:
            A formatted markdown issue body.
        """
        services = ", ".join(problem.impacted_services) or "unknown"
        return "\n".join(
            [
                f"# 🚨 Incident: {problem.title}",
                "",
                "## Summary",
                f"- **Problem ID:** {problem.problem_id}",
                f"- **Severity:** {problem.severity}",
                f"- **Status:** {problem.status}",
                f"- **Affected service(s):** {services}",
                f"- **Root cause (Dynatrace):** {problem.root_cause or 'n/a'}",
                "",
                "## Anomaly Timeline",
                IncidentTemplates._timeline_section(problem),
                "",
                "## Correlated Deployment",
                IncidentTemplates._deployment_section(deployment),
                "",
                "## NERVE Analysis",
                reasoning.strip() or "_No analysis available._",
                IncidentTemplates._external_intelligence_section(external_context, grounding_sources or []),
                "## Recommended Actions",
                IncidentTemplates._actions_section(deployment),
                "",
                "---",
                _FOOTER,
            ]
        )

    @staticmethod
    def _external_intelligence_section(external_context: str, sources: list[str]) -> str:
        """Render the grounding section (empty string when there's nothing to show)."""
        if not external_context and not sources:
            return ""
        lines = ["", "## External Intelligence"]
        if external_context:
            lines.append(external_context.strip())
        if sources:
            lines.append("")
            lines.append("**Sources:**")
            lines.extend(f"- {url}" for url in sources)
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def rollback_mr_body(deployment: GitLabDeployment) -> str:
        """Render the rollback merge-request description.

        Args:
            deployment: The deployment to roll back.

        Returns:
            A formatted markdown MR description.
        """
        return "\n".join(
            [
                f"# Rollback: revert deployment {deployment.id}",
                "",
                "Reverts the deployment correlated with an active incident by NERVE "
                "Incident Autopilot.",
                "",
                "## Deployment under rollback",
                f"- **Deployment ID:** {deployment.id}",
                f"- **Ref:** {deployment.ref}",
                f"- **Commit:** `{deployment.sha or 'unknown'}`",
                f"- **Environment:** {deployment.environment or 'unknown'}",
                "",
                "## Approval",
                "This rollback requires human approval before execution (NERVE invariant).",
                "",
                "---",
                _FOOTER,
            ]
        )

    @staticmethod
    def _timeline_section(problem: DynatraceProblemDetail) -> str:
        """Format the anomaly timeline as a markdown bullet list."""
        lines: list[str] = []
        if problem.start_time:
            lines.append(f"- **Detected:** {problem.start_time.isoformat()}")
        if problem.end_time:
            lines.append(f"- **Ended:** {problem.end_time.isoformat()}")
        for event in problem.timeline[:_MAX_TIMELINE_EVENTS]:
            timestamp = event.get("timestamp") or event.get("time") or ""
            description = event.get("description") or event.get("event") or ""
            lines.append(f"- {timestamp} {description}".rstrip())
        return "\n".join(lines) or "_No timeline events reported._"

    @staticmethod
    def _deployment_section(deployment: GitLabDeployment | None) -> str:
        """Format the correlated-deployment section (or a 'none' notice)."""
        if deployment is None:
            return "_No correlated deployment identified._"
        parts = [f"- **Deployment ID:** {deployment.id}", f"- **Ref:** {deployment.ref}"]
        if deployment.sha:
            parts.append(f"- **Commit:** `{deployment.sha}`")
        if deployment.environment:
            parts.append(f"- **Environment:** {deployment.environment}")
        if deployment.created_at:
            parts.append(f"- **Deployed at:** {deployment.created_at.isoformat()}")
        return "\n".join(parts)

    @staticmethod
    def _actions_section(deployment: GitLabDeployment | None) -> str:
        """Format the recommended-actions section."""
        if deployment is None:
            return "- Investigate the anomaly manually; no deployment correlation was found."
        return "\n".join(
            [
                f"- Review deployment {deployment.id} (`{deployment.sha or deployment.ref}`).",
                "- If confirmed as the cause, **approve the rollback action** in NERVE.",
            ]
        )
