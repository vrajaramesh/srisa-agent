"""Application settings loaded from environment variables (and `.env` locally)."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database-only settings, so Alembic can run without the WhatsApp secrets."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Neon POOLED connection string (host contains "-pooler"), used by the app.
    database_url: SecretStr
    # Neon DIRECT connection string, used only by Alembic migrations.
    database_url_direct: SecretStr | None = None
    # Postgres schema holding all agent tables. Tests use "agent_test".
    db_schema: str = "agent"


class AgentSettings(BaseSettings):
    """AI agent settings (also used on their own by scripts/chat_cli.py, which needs no WhatsApp/DB config)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # None = let the SDK resolve credentials (ANTHROPIC_API_KEY env, `ant auth login` profile, ...).
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5-5"
    llm_max_tokens: int = 600
    llm_max_tool_iterations: int = 5
    llm_timeout_seconds: float = 30.0
    # low = fast chat replies; the model skips thinking on most simple messages.
    llm_effort: str = "low"
    history_messages: int = 20
    brand_name: str = "SRISA Boutique"

    # mybill-service /api/agent/** (read-only catalog for the agent's tools).
    mybill_base_url: str = ""  # e.g. https://mybill-service-276371417285.us-central1.run.app
    mybill_audience: str = ""  # ID-token audience; defaults to mybill_base_url
    # google = Google ID token (metadata server on Cloud Run, or MYBILL_ID_TOKEN); none = local mybill (local profile)
    mybill_auth: str = "google"
    mybill_id_token: SecretStr | None = None  # static token for local runs (gcloud auth print-identity-token ...)
    # mybill-service runs with min-instances=0: a cold start takes ~20-25 s, so allow for it.
    mybill_timeout_seconds: float = 30.0


@lru_cache
def get_agent_settings() -> AgentSettings:
    """Agent-only settings (cached)."""
    return AgentSettings()


class Settings(DatabaseSettings, AgentSettings):
    """All runtime configuration. Secrets are SecretStr so they never appear in reprs/logs."""

    wa_access_token: SecretStr
    wa_app_secret: SecretStr
    wa_verify_token: SecretStr
    wa_phone_number_id: str
    graph_api_version: str = "v25.0"
    log_level: str = "INFO"
    # Comma-separated WhatsApp ids (e.g. "919876543210,919812345678"). When set, the bot
    # replies ONLY to these numbers and just logs everyone else - for safe testing on the
    # live shop number. Empty = reply to everyone.
    allowed_senders: str = ""

    # Cloud Tasks. With USE_CLOUD_TASKS=false (local dev) messages are processed in a
    # FastAPI BackgroundTask instead.
    use_cloud_tasks: bool = False
    tasks_queue: str = ""
    tasks_location: str = ""
    tasks_target_url: str = ""  # base URL of this service, e.g. https://srisa-agent-xxx.a.run.app
    tasks_service_account: str = ""  # email in the OIDC token Cloud Tasks attaches
    gcp_project: str = ""

    # POST /admin/** auth: Bearer ADMIN_API_KEY, or a Google ID token (audience = TASKS_TARGET_URL)
    # whose email is in ADMIN_ALLOWED_EMAILS (comma-separated).
    admin_api_key: SecretStr | None = None
    admin_allowed_emails: str = ""

    @property
    def admin_email_set(self) -> frozenset[str]:
        """Parsed ADMIN_ALLOWED_EMAILS (lower-cased)."""
        return frozenset(e.strip().lower() for e in self.admin_allowed_emails.split(",") if e.strip())

    @property
    def allowed_sender_set(self) -> frozenset[str]:
        """Parsed ALLOWED_SENDERS ('+' and spaces stripped); empty means no restriction."""
        return frozenset(
            n.strip().lstrip("+").replace(" ", "") for n in self.allowed_senders.split(",") if n.strip()
        )

    @property
    def messages_url(self) -> str:
        """Cloud API endpoint used for sending messages and read receipts."""
        return (
            f"https://graph.facebook.com/{self.graph_api_version}"
            f"/{self.wa_phone_number_id}/messages"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance (cached)."""
    return Settings()  # type: ignore[call-arg]
