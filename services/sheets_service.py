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
ROW_KEYS = ["date", "vendor", "amount", "currency", "category", "notes", "logged_at"]

_service = None
_header_checked = False


class SheetsNotConfigured(RuntimeError):
    pass


class NoReceiptToUndo(RuntimeError):
    """Raised when /undo is used but the Receipts tab has no logged rows
    (just the header, or nothing at all) — nothing to remove."""


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


def _receipts_tab_id_sync(service, sheet_id: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == SHEET_TAB:
            return s["properties"]["sheetId"]
    raise NoReceiptToUndo("There's no Receipts tab yet — nothing's been logged.")


def _last_receipt_row_sync(service, sheet_id: str) -> Optional[int]:
    """1-indexed row number of the most recently logged receipt (the last
    row with data in column A), or None if there's nothing past the header
    row yet."""
    values = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{SHEET_TAB}!A:A")
        .execute()
        .get("values", [])
    )
    last_row = len(values)
    return last_row if last_row > 1 else None  # row 1 is the header


def _delete_last_receipt_sync() -> dict:
    _ensure_header_sync()
    service = _get_service()
    sheet_id = settings.google_sheet_id

    last_row = _last_receipt_row_sync(service, sheet_id)
    if last_row is None:
        raise NoReceiptToUndo("There's no logged receipt to undo.")

    # Read the row before deleting it, so the caller can confirm to the user
    # exactly what was removed.
    row_values = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{SHEET_TAB}!A{last_row}:G{last_row}")
        .execute()
        .get("values", [[]])
    )
    row_values = row_values[0] if row_values else []
    removed = dict(zip(ROW_KEYS, row_values + [""] * (len(ROW_KEYS) - len(row_values))))

    tab_id = _receipts_tab_id_sync(service, sheet_id)
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": tab_id,
                            "dimension": "ROWS",
                            "startIndex": last_row - 1,  # deleteDimension is 0-indexed
                            "endIndex": last_row,
                        }
                    }
                }
            ]
        },
    ).execute()

    return removed


async def delete_last_receipt() -> dict:
    """Deletes the most recently logged receipt row and returns what was
    removed (date/vendor/amount/etc.) so the caller can tell the user
    exactly what got undone. Raises NoReceiptToUndo if the Receipts tab is
    empty (nothing logged yet)."""
    try:
        return await asyncio.to_thread(_delete_last_receipt_sync)
    except NoReceiptToUndo:
        raise
    except HttpError as exc:
        logger.exception("Sheets delete_last_receipt failed")
        raise RuntimeError(f"Google Sheets rejected the request: {exc.reason}") from exc
