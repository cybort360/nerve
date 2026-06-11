"""Application configuration loaded from environment variables.

All constants and tunables live here. No magic strings or numbers elsewhere in
the codebase (see ARCHITECTURE.md section 3). Import the module-level ``settings``
singleton rather than instantiating :class:`Settings` directly.

Production note:
    On Cloud Run, secrets (``MONGODB_URI``, ``DYNATRACE_API_TOKEN``, ``GITLAB_TOKEN``,
    etc.) are NOT read from a ``.env`` file. They are stored in Google Cloud Secret
    Manager and surfaced to the container as environment variables via the service's
    ``--set-secrets`` mapping (or mounted secret volumes). pydantic-settings reads
    those env vars transparently, so no code change is needed between environments.
    The ``.env`` file is a local-development convenience only and must never be
    committed or shipped in the image (see ``.dockerignore``).
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings sourced from ``.env`` and the environment.

    Field names map case-insensitively to the variables defined in
    ``.env.example``. Secret-bearing fields have no default and must be supplied.
    """

    # NOTE: ARCHITECTURE.md illustrates ``model_config = ConfigDict(env_file=".env")``,
    # but pydantic-settings v2 requires ``SettingsConfigDict`` for env-file loading.
    # Using the correct type here; flagged per the "never deviate without flagging" rule.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Google Cloud / Gemini ---
    google_cloud_project: str = Field(default="", description="GCP project ID hosting Vertex AI.")
    google_cloud_location: str = Field(
        default="us-central1", description="Region for Gemini API calls."
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash", description="Gemini model identifier."
    )
    gemini_grounding_enabled: bool = Field(
        default=True, description="Use Google Search grounding in Gemini reasoning."
    )
    memory_bank_enabled: bool = Field(
        default=False, description="Store/retrieve past incidents via Vertex AI Memory Bank."
    )
    memory_bank_id: str = Field(default="", description="Vertex AI Memory Bank resource id.")

    # --- Google Cloud Agent Builder (ADK planner) ---
    agent_builder_agent_id: str = Field(
        default="",
        description="Vertex AI Agent Engine resource name for the deployed nerve-planner "
        "agent. When set, planning runs through the deployed Agent Builder agent; "
        "unset falls back to the in-process ADK runner, then direct Gemini.",
    )
    agent_builder_location: str = Field(
        default="us-central1", description="Region of the deployed Agent Builder agent."
    )
    agent_builder_enabled: bool = Field(
        default=True,
        description="Route planning through the ADK agent (deployed or in-process). "
        "When false, the planner uses direct Gemini calls only.",
    )

    # --- MongoDB ---
    mongodb_uri: str = Field(description="MongoDB Atlas connection string.")
    mongodb_database: str = Field(default="nerve", description="Database name.")

    # --- Dynatrace ---
    dynatrace_environment_url: str = Field(
        description="Dynatrace environment URL (no trailing slash)."
    )
    dynatrace_api_token: str = Field(description="Dynatrace API token.")
    dynatrace_webhook_secret: str = Field(
        default="", description="Shared secret for validating the Dynatrace webhook signature."
    )

    # --- GitLab ---
    gitlab_url: str = Field(
        default="https://gitlab.com", description="GitLab instance URL."
    )
    gitlab_token: str = Field(description="GitLab personal access token (api scope).")
    gitlab_project_id: str = Field(description="Default GitLab project ID.")

    # --- Web Search (Tavily) ---
    tavily_api_key: str = Field(
        default="", description="Tavily API key for web search (free tier ~1,000/month)."
    )
    tavily_api_url: str = Field(
        default="https://api.tavily.com/search", description="Tavily search endpoint."
    )
    web_search_max_results: int = Field(
        default=5, ge=1, description="Max web search results per query."
    )

    # --- Orchestrator ---
    orchestration_interval_seconds: int = Field(
        default=30, ge=1, description="Seconds between orchestration loop cycles."
    )
    risk_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0, description="Risk score that triggers replanning."
    )
    max_task_retries: int = Field(
        default=3, ge=0, description="Max retries per task before marking failed."
    )

    # --- Telegram (mobile approvals + notifications) ---
    telegram_bot_token: str = Field(
        default="", description="Bot token from @BotFather (empty disables the bot)."
    )
    telegram_chat_id: str = Field(
        default="", description="Chat id that receives approval requests/notifications."
    )
    telegram_enabled: bool = Field(
        default=False, description="Enable the Telegram bot (opt-in; off by default)."
    )

    # --- Feature Flags ---
    failure_engine_enabled: bool = Field(
        default=False, description="Enable failure injection (demo/testing only)."
    )
    demo_mode: bool = Field(
        default=False, description="Run the scripted demo scenario."
    )

    # --- Auth (JWT session) ---
    jwt_secret: str = Field(default="", description="HS256 signing key; set in prod. Empty => ephemeral per-instance secret.")
    jwt_expire_minutes: int = Field(default=10080, ge=1, description="Session lifetime in minutes (default 7 days).")
    cookie_secure: bool = Field(default=True, description="Set the session cookie Secure flag. Set false for local http (curl) testing.")
    incidents_owner_email: str = Field(default="", description="Email of the user who owns Dynatrace-webhook incidents (no logged-in user). Empty => unowned.")

    # --- Per-user settings encryption ---
    settings_encryption_key: str = Field(default="", description="Fernet key (urlsafe base64) for encrypting per-user integration tokens at rest. Generate: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")



settings = Settings()
"""Module-level singleton. Import this everywhere instead of re-instantiating."""
