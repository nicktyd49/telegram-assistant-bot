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

    google_service_account_info: Optional[dict]
    google_calendar_id: Optional[str]
    google_sheet_id: Optional[str]

    @property
    def calendar_configured(self) -> bool:
        return bool(self.google_service_account_info and self.google_calendar_id)

    @property
    def sheets_configured(self) -> bool:
        return bool(self.google_service_account_info and self.google_sheet_id)


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

    settings = Settings(
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        allowed_user_id=int(os.environ["ALLOWED_USER_ID"]),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
        timezone=os.environ.get("TIMEZONE", "Asia/Singapore"),
        default_currency=os.environ.get("DEFAULT_CURRENCY", "SGD"),
        google_service_account_info=service_account_info,
        google_calendar_id=calendar_id,
        google_sheet_id=sheet_id,
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

    return settings


settings = load_settings()
