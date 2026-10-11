"""Impostazioni lette dalle variabili d'ambiente (vedi .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Carica un file .env minimale senza sovrascrivere variabili già impostate."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD"))
    database_path: str = field(default_factory=lambda: _env("DATABASE_PATH", str(ROOT / "leads.db")))
    centri_path: str = field(default_factory=lambda: _env("CENTRI_PATH", str(ROOT / "config" / "centri.yaml")))

    voice_provider: str = field(default_factory=lambda: _env("VOICE_PROVIDER", "dry_run"))
    vapi_api_key: str = field(default_factory=lambda: _env("VAPI_API_KEY"))
    vapi_phone_number_id: str = field(default_factory=lambda: _env("VAPI_PHONE_NUMBER_ID"))
    vapi_server_secret: str = field(default_factory=lambda: _env("VAPI_SERVER_SECRET"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-haiku-5-5"))
    voice_provider_name: str = field(default_factory=lambda: _env("VOICE_PROVIDER_NAME", "azure"))
    voice_id: str = field(default_factory=lambda: _env("VOICE_ID", "it-IT-ElsaNeural"))
    transcriber_language: str = field(default_factory=lambda: _env("TRANSCRIBER_LANGUAGE", "it"))
    max_concurrent_calls: int = field(default_factory=lambda: int(_env("MAX_CONCURRENT_CALLS", "3")))

    meta_verify_token: str = field(default_factory=lambda: _env("META_VERIFY_TOKEN"))
    meta_app_secret: str = field(default_factory=lambda: _env("META_APP_SECRET"))
    meta_page_access_token: str = field(default_factory=lambda: _env("META_PAGE_ACCESS_TOKEN"))
    meta_graph_version: str = field(default_factory=lambda: _env("META_GRAPH_VERSION", "v21.0"))

    google_lead_key: str = field(default_factory=lambda: _env("GOOGLE_LEAD_KEY"))
    generic_webhook_token: str = field(default_factory=lambda: _env("GENERIC_WEBHOOK_TOKEN"))
    google_service_account_file: str = field(default_factory=lambda: _env("GOOGLE_SERVICE_ACCOUNT_FILE"))
    google_service_account_json: str = field(default_factory=lambda: _env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    # Messaggi al cliente (conferme e promemoria). Se MESSAGGI_ATTIVI non è "1" vengono solo simulati nei log.
    messaggi_attivi: bool = field(default_factory=lambda: _env("MESSAGGI_ATTIVI") == "1")
    twilio_account_sid: str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    twilio_sms_from: str = field(default_factory=lambda: _env("TWILIO_SMS_FROM"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    email_from: str = field(default_factory=lambda: _env("EMAIL_FROM"))
    notify_webhook_url: str = field(default_factory=lambda: _env("NOTIFY_WEBHOOK_URL"))


settings = Settings()
