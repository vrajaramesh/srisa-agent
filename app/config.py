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
    # The shop's WhatsApp number (digits) for the "Order on WhatsApp" link sent on Instagram.
    whatsapp_order_number: str = "919491647691"
    # Short redirect to the pre-typed WhatsApp order message, for text links on Instagram (long wa.me links aren't
    # clickable there): {order_link_base}/KUR-101/IG-7K3QA. Served by the shop site (mybill-ecom nginx). Empty = wa.me.
    order_link_base: str = "https://srisafabrics.com/wa"
    # The shop website: a design's fabrics are sold there ({shop_url}/?fabrics=FRO-11 lists them all).
    shop_url: str = "https://srisafabrics.com"

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

    # Instagram (API with Instagram Login). Webhooks may be signed with the Instagram app secret
    # (Instagram > API setup), which can differ from WA_APP_SECRET; either is accepted.
    ig_app_secret: SecretStr | None = None
    ig_verify_token: SecretStr | None = None  # defaults to WA_VERIFY_TOKEN
    ig_account_id: str = ""  # Instagram professional account id (srisa_fabrics: 17841463885925115)
    # Local/tests only. In Cloud Run the token is read from Secret Manager (IG_ACCESS_TOKEN) with a 1-hour cache.
    ig_access_token: SecretStr | None = None
    # Comma-separated Instagram-scoped user ids. When set, the bot replies ONLY to these accounts on Instagram.
    allowed_ig_senders: str = ""
    # Keep a raw copy of every Instagram webhook in agent.raw_webhooks (for collecting payload samples).
    ig_capture_raw: bool = True
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
    # POST /internal/events (payment notifications from mybill-service): Google ID token whose email is this
    # (mybill-service's runtime service account) and audience TASKS_TARGET_URL (this service's URL).
    mybill_events_sa_email: str = "276371417285-compute@developer.gserviceaccount.com"

    # Staff alerts when a chat is handed to the team: WhatsApp template to these numbers (comma-separated,
    # digits with country code). Empty = no alerts. The template must be approved in WhatsApp Manager.
    staff_alert_numbers: str = ""
    staff_alert_template: str = "staff_alert"
    staff_alert_template_lang: str = "en"
    # AUTHENTICATION template for website password-reset codes (srisafabrics.com "Forgot password").
    reset_code_template: str = "srisa_reset_code"
    reset_code_template_lang: str = "en"
    mybill_ui_url: str = ""  # link in the alert: {mybill_ui_url}/?chat=<conversation id>

    admin_api_key: SecretStr | None = None
    admin_allowed_emails: str = ""

    # Follow-ups for quiet customers (app/followups.py, POST /internal/jobs/followups every 30 min).
    followups_enabled: bool = True
    followup_enquiry_hours: float = 20      # after her last message (inside the free 24 h window, 9 AM-9 PM IST)
    followup_payment_hours: str = "2,20"    # after the order was created, while unpaid
    # Daily AI report (app/insights.py): model and effort for the one analysis call per day.
    insights_model: str = "claude-opus-5-5"
    insights_effort: str = "high"
    # Cloud Scheduler's service account (OIDC token on the follow-up and daily-report jobs).
    scheduler_sa_email: str = ""

    @property
    def followup_payment_hour_list(self) -> list[float]:
        return sorted(float(h) for h in self.followup_payment_hours.split(",") if h.strip())

    @property
    def staff_alert_number_list(self) -> list[str]:
        return [n.strip().lstrip("+").replace(" ", "") for n in self.staff_alert_numbers.split(",") if n.strip()]

    @property
    def allowed_ig_sender_set(self) -> frozenset[str]:
        return frozenset(n.strip() for n in self.allowed_ig_senders.split(",") if n.strip())

    @property
    def ig_webhook_secrets(self) -> list[str]:
        """Secrets an Instagram webhook signature may be made with."""
        return [s.get_secret_value() for s in (self.ig_app_secret, self.wa_app_secret) if s and s.get_secret_value()]

    @property
    def ig_verify_token_value(self) -> str:
        return (self.ig_verify_token or self.wa_verify_token).get_secret_value()

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
