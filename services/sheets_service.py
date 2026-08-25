"""Thin wrapper around the Google Sheets API for logging receipts.

Setup note (see README): the target spreadsheet must be *shared* with the
service account's email (found in the JSON key as "client_email"), with
"Editor" access — otherwise every call here will fail with a 403.

Expects (and will create if missing) a tab named "Receipts" with header row:
Date | Vendor | Amount | Currency | Category | Notes | Logged At
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings

logger = logging.getLogger("assistant-bot.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_TAB = "Receipts"
HEADER_ROW = ["Date", "Vendor", "Amount", "Currency", "Category", "Notes", "Logged At"]

_service = None
_header_checked = False


class SheetsNotConfigured(RuntimeError):
    pass


def _get_service():
    global _service
    if _service is not None:
        return _service
    if not settings.sheets_configured:
        raise SheetsNotConfigured(
            "Receipt logging isn't set up yet — GOOGLE_SERVICE_ACCOUNT_JSON and "
            "GOOGLE_SHEET_ID need to be configured (see README)."
        )
    creds = service_account.Credentials.from_service_account_info(
        settings.google_service_account_info, scopes=SCOPES
    )
    _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service


def _ensure_header_sync() -> None:
    global _header_checked
    if _header_checked:
        return
    service = _get_service()
    sheet_id = settings.google_sheet_id

    # Make sure the "Receipts" tab exists.
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if SHEET_TAB not in tab_titles:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_TAB}}}]},
        ).execute()

    # Make sure row 1 has headers.
    existing = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{SHEET_TAB}!A1:G1")
        .execute()
    )
    if not existing.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{SHEET_TAB}!A1:G1",
            valueInputOption="RAW",
            body={"values": [HEADER_ROW]},
        ).execute()

    _header_checked = True


def _append_receipt_sync(date: str, vendor: str, amount: float, currency: str,
                          category: Optional[str], notes: Optional[str]) -> None:
    _ensure_header_sync()
    service = _get_service()
    logged_at = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M")
    row = [date, vendor, amount, currency, category or "", notes or "", logged_at]
    service.spreadsheets().values().append(
        spreadsheetId=settings.google_sheet_id,
        range=f"{SHEET_TAB}!A:G",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


async def append_receipt(date: str, vendor: str, amount: float, currency: str = "SGD",
                          category: Optional[str] = None, notes: Optional[str] = None) -> None:
    try:
        await asyncio.to_thread(_append_receipt_sync, date, vendor, amount, currency, category, notes)
    except HttpError as exc:
        logger.exception("Sheets append_receipt failed")
        raise RuntimeError(f"Google Sheets rejected the request: {exc.reason}") from exc
