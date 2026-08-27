"""Central place for env-driven configuration.

Keeping this separate from bot.py means every module (calendar, sheets, pdf,
assistant) can `from config import settings` instead of re-reading os.environ
and duplicating validation.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("assistant-bot.config")


def _get_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    anthropic_api_key: str
    allowed_user_id: int
    anthropic_model: str
    timezone: str
    default_currency: str
    agent_name: str
    extraction_model: str

    google_service_account_info: Optional[dict]
    google_calendar_id: Optional[str]
    google_sheet_id: Optional[str]
    policy_pdf_storage_dir: Optional[str]

    onedrive_client_id: Optional[str]
    onedrive_token_cache: Optional[str]

    @property
    def calendar_configured(self) -> bool:
        return bool(self.google_service_account_info and self.google_calendar_id)

    @property
    def sheets_configured(self) -> bool:
        return bool(self.google_service_account_info and self.google_sheet_id)

    @property
    def onedrive_configured(self) -> bool:
        """True once ONEDRIVE_CLIENT_ID is set — enough to run /onedrive_setup.
        The token cache (ONEDRIVE_TOKEN_CACHE) is only needed for the actual
        upload/download calls, which raise their own clear error if it's
        missing/expired, so it isn't required here."""
        return bool(self.onedrive_client_id)


def _load_service_account_info() -> Optional[dict]:
    """Loads the Google service-account key from either:
    - GOOGLE_SERVICE_ACCOUNT_JSON: the raw JSON key content (recommended for Railway)
    - GOOGLE_SERVICE_ACCOUNT_FILE: a path to the JSON key file (handy for local dev)
    """
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error(
                "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON. "
                "Calendar and receipt logging will be disabled until this is fixed."
            )
            return None

    file_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Could not read GOOGLE_SERVICE_ACCOUNT_FILE (%s): %s", file_path, exc)
            return None

    return None


def load_settings() -> Settings:
    missing = [
        name
        for name in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "ALLOWED_USER_ID")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in."
        )

    service_account_info = _load_service_account_info()

    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip() or None
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip() or None
    policy_pdf_storage_dir = os.environ.get("POLICY_PDF_STORAGE_DIR", "").strip() or None
    onedrive_client_id = os.environ.get("ONEDRIVE_CLIENT_ID", "").strip() or None
    onedrive_token_cache = os.environ.get("ONEDRIVE_TOKEN_CACHE", "").strip() or None

    settings = Settings(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        allowed_user_id=int(os.environ["ALLOWED_USER_ID"]),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        timezone=os.environ.get("TIMEZONE", "Asia/Singapore"),
        default_currency=os.environ.get("DEFAULT_CURRENCY", "SGD"),
        agent_name=os.environ.get("AGENT_NAME", "Nicholas"),
        extraction_model=os.environ.get("EXTRACTION_MODEL", "claude-haiku-4-5-20251001"),
        google_service_account_info=service_account_info,
        google_calendar_id=calendar_id,
        google_sheet_id=sheet_id,
        policy_pdf_storage_dir=policy_pdf_storage_dir,
        onedrive_client_id=onedrive_client_id,
        onedrive_token_cache=onedrive_token_cache,
    )

    if not settings.calendar_configured:
        logger.warning(
            "Google Calendar is not fully configured (need GOOGLE_SERVICE_ACCOUNT_JSON/"
            "FILE and GOOGLE_CALENDAR_ID) — scheduling features will reply with an "
            "explanation instead of working."
        )
    if not settings.sheets_configured:
        logger.warning(
            "Google Sheets is not fully configured (need GOOGLE_SERVICE_ACCOUNT_JSON/"
            "FILE and GOOGLE_SHEET_ID) — receipt logging will reply with an "
            "explanation instead of working."
        )
    if not settings.policy_pdf_storage_dir:
        logger.warning(
            "POLICY_PDF_STORAGE_DIR is not set — original policy PDFs will not be archived."
        )
    if not settings.onedrive_configured:
        logger.warning(
            "ONEDRIVE_CLIENT_ID is not set — client policy workbooks and archived PDFs "
            "will only live on this server's disk and will be lost on the next deploy. "
            "Run /onedrive_setup once ONEDRIVE_CLIENT_ID is added."
        )

    return settings


settings = load_settings()
