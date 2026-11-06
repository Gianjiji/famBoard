"""Configuration, loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv is optional at runtime
    pass

ROOT = Path(__file__).resolve().parent.parent


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    pihole_url: str = os.getenv("PIHOLE_URL", "https://192.168.1.46").rstrip("/")
    pihole_password: str = os.getenv("PIHOLE_PASSWORD", "")
    pihole_verify_tls: bool = _bool(os.getenv("PIHOLE_VERIFY_TLS"), False)
    db_path: Path = ROOT / os.getenv("DB_PATH", "data/famboard.db")
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "60"))
    categories_dir: Path = ROOT / "data" / "categories"
    # Device census CSV: human-editable backup of device labels (kept in sync
    # by `census-sync` / `prune`; survives uninstall).
    census_csv: Path = ROOT / os.getenv("CENSUS_CSV", "censimentoMAC/census.csv")
    # Dashboard login. If empty, auth is disabled (LAN-only; a warning is logged).
    app_password: str = os.getenv("APP_PASSWORD", "")
    # Session cookie signing key. Auto-generated and persisted if unset.
    secret_key: str = os.getenv("SECRET_KEY", "")
    # Optional webhook for minor alerts (e.g. an ntfy topic URL). POST json+text.
    webhook_url: str = os.getenv("WEBHOOK_URL", "")
    # Optional SMTP for weekly reports by email. Email is sent only if host+to set.
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    smtp_to: str = os.getenv("SMTP_TO", "")
    smtp_tls: bool = _bool(os.getenv("SMTP_TLS"), True)
    # Attention report (event-driven): min seconds between auto-sends, and how
    # often the poller re-evaluates the situation.
    attention_min_interval: int = int(os.getenv("ATTENTION_MIN_INTERVAL", "21600"))
    attention_check_interval: int = int(os.getenv("ATTENTION_CHECK_INTERVAL", "900"))
    # Optional local LLM (OpenAI-compatible endpoint, e.g. ollama / llama.cpp
    # server) for a context-aware summary at the top of the attention report.
    # Off unless a URL is set; failures are non-fatal.
    ai_summary_url: str = os.getenv("AI_SUMMARY_URL", "")
    ai_summary_model: str = os.getenv("AI_SUMMARY_MODEL", "")
    ai_summary_key: str = os.getenv("AI_SUMMARY_KEY", "")

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.categories_dir.mkdir(parents=True, exist_ok=True)

    def resolve_secret(self) -> str:
        """Return the session secret, generating and persisting one if needed."""
        if self.secret_key:
            return self.secret_key
        key_file = self.db_path.parent / ".secret_key"
        if key_file.exists():
            return key_file.read_text().strip()
        import secrets
        key = secrets.token_hex(32)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key)
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        return key


settings = Settings()
