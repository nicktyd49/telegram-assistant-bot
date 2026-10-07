"""Builds "ILP Funds Update" workbooks — the report format worked out by
hand with Nic for client Lau Jing Wen (commencement / 1st-anniversary /
current fund prices, allocation, gain/loss, remarks), now parameterized so
the Telegram bot can generate one for any client from a short chat wizard
instead of a one-off manual build.

Mirrors services/policy_workbook.py's conventions (OneDrive folder layout,
filename sanitizing) but is otherwise a self-contained builder — this report
has nothing to do with the Policy Summary sheet layout.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import openpyxl
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from services import onedrive_service

logger = logging.getLogger("assistant-bot.fund_update_workbook")

SHEET_NAME = "ILP Funds Update"
ONEDRIVE_WORKBOOK_FOLDER = "Client"

GREY = "FF999999"
GREEN = "FF00B050"
RED = "FFFF0000"

_ONEDRIVE_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _onedrive_safe_name(text: str | None, fallback: str = "Unknown Client") -> str:
    cleaned = _ONEDRIVE_ILLEGAL_CHARS.sub("", (text or "")).strip().rstrip(".")
    return cleaned or fallback


@dataclass
class FundRow:
    name: str
    allocation_pct: float  # e.g. 25 for 25%
    price_commencement: float
    price_anniversary: float
    price_current: float
    remark: Optional[str] = None


@dataclass
class FundUpdateData:
    client_name: str
    product: str
    policy_number: str
    commencement_date: date
    anniversary_date: date
    current_date: date
    total_invested: float
    account_value: float
    account_value_asof: date
    funds: list[FundRow]
    ref_illustration: Optional[str] = None  # e.g. "$13,911 (8% IRR)"
    action_date_label: Optional[date] = None  # defaults to current_date


def _set_row_heights(ws: Worksheet, heights: dict[int, float]) -> None:
    for row, height in heights.items():
        ws.row_dimensions[row].height = height


def build_fund_update_workbook(data: FundUpdateData) -> bytes:
    """Returns the finished .xlsx as bytes, ready to hand to Telegram and/or
    upload to OneDrive."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = SHEET_NAME

    n_funds = len(data.funds)
    if n_funds == 0:
        raise ValueError("Need at least one fund to build a report")

    first_fund_row = 6
    last_fund_row = first_fund_row + n_funds - 1
    action_row = last_fund_row + 2
    action_date_row = action_row + 1
    prompt_rows_start = action_date_row + 2

    # --- Header block -----------------------------------------------------
    ws["A1"] = data.client_name.upper()
    ws["A1"].font = Font(size=16, bold=True)
    ws["B1"] = data.product
    ws["B1"].font = Font(size=12, italic=True)
    ws["D1"] = "TOTAL INVESTMENT:"
    ws["D1"].font = Font(bold=True)
    ws.merge_cells("E1:F1")
    ws["E1"] = f"${data.total_invested:,.0f}"
    ws["E1"].font = Font(bold=True)

    ws["A2"] = "POLICY NUMBER"
    ws["B2"] = data.policy_number
    ws["D2"] = "ACCOUNT VALUE:"
    ws["D2"].font = Font(bold=True)
    ws.merge_cells("E2:F2")
    ws["E2"] = f"${data.account_value:,.0f} ({data.account_value_asof:%d/%m/%Y})"
    ws["E2"].font = Font(bold=True)

    ws["A3"] = "COMMENCEMENT"
    ws["B3"] = data.commencement_date
    ws["B3"].number_format = "dd/mm/yyyy"
    ws["D3"] = "REF POLICY ILLUSTRATION:"
    ws["D3"].font = Font(bold=True)
    ws.merge_cells("E3:F3")
    if data.ref_illustration:
        ws["E3"] = data.ref_illustration
        ws["E3"].font = Font(color=GREY)

    for row in (1, 2, 3):
        ws.row_dimensions[row].height = 25
    ws.row_dimensions[4].height = 15

    # --- Table header -------------------------------------------------
    headers = ["Allocation", "Fund", data.commencement_date, data.anniversary_date, data.current_date, "+/-", "Remark"]
    for col, value in zip("ABCDEFG", headers):
        cell = ws[f"{col}5"]
        cell.value = value
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for col in ("C", "D", "E"):
        ws[f"{col}5"].number_format = "dd/mm/yyyy"
    ws.row_dimensions[5].height = 25

    # --- Fund rows ----------------------------------------------------
    for i, fund in enumerate(data.funds):
        r = first_fund_row + i
        ws[f"A{r}"] = fund.allocation_pct / 100
        ws[f"A{r}"].number_format = "0%"
        ws[f"A{r}"].alignment = Alignment(horizontal="center")
        ws[f"B{r}"] = fund.name
        ws[f"C{r}"] = fund.price_commencement
        ws[f"D{r}"] = fund.price_anniversary
        ws[f"E{r}"] = fund.price_current
        for col in ("C", "D", "E"):
            ws[f"{col}{r}"].number_format = "0.0000"
            ws[f"{col}{r}"].alignment = Alignment(horizontal="center")

        change = (fund.price_current - fund.price_anniversary) / fund.price_anniversary if fund.price_anniversary else 0.0
        cell = ws[f"F{r}"]
        cell.value = change
        cell.number_format = "0%"
        cell.font = Font(bold=True, color=GREEN if change >= 0 else RED)
        cell.alignment = Alignment(horizontal="center")

        if fund.remark:
            ws[f"G{r}"] = fund.remark
            ws[f"G{r}"].font = Font(color=GREY)
        ws.row_dimensions[r].height = 25

    # --- Action section -------------------------------------------------
    ws.merge_cells(f"A{action_row}:G{action_row}")
    ws[f"A{action_row}"] = "ACTION:"
    ws[f"A{action_row}"].font = Font(bold=True)
    ws.row_dimensions[action_row].height = 15

    ws.merge_cells(f"A{action_date_row}:G{action_date_row}")
    action_date = data.action_date_label or data.current_date
    ws[f"A{action_date_row}"] = f"{action_date:%d/%m/%Y} - "
    ws.row_dimensions[action_date_row].height = 60

    prompts = [
        "Initial Objective of this investment\n",
        "Market Update in the last 12 months\n",
        "Outlook in the next 12 months\n",
    ]
    for i, prompt in enumerate(prompts):
        r = prompt_rows_start + i
        ws.merge_cells(f"A{r}:G{r}")
        ws[f"A{r}"] = prompt
        ws[f"A{r}"].alignment = Alignment(vertical="top", wrap_text=True)
        ws.row_dimensions[r].height = 70
    ws.row_dimensions[prompt_rows_start - 1].height = 15

    # --- Column widths / page setup --------------------------------------
    widths = {"A": 14.83, "B": 46.0, "C": 11.5, "D": 11.5, "E": 11.5, "F": 8.5, "G": 12.16}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _onedrive_remote_path(client_name: str, filename: str) -> str:
    folder = _onedrive_safe_name(client_name)
    return f"{ONEDRIVE_WORKBOOK_FOLDER}/{folder}/{filename}"


def _filename(client_name: str) -> str:
    safe = _onedrive_safe_name(client_name)
    return f"ILP Funds Update {safe}.xlsx"


def _parse_dollar_amount(value) -> Optional[float]:
    """Parses "$12,345" (or a raw number already) back into a float. Used by
    _parse_existing() to read totals written as plain display strings by
    build_fund_update_workbook()."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = re.sub(r"[^0-9.]", "", str(value))
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def _parse_existing(xlsx_bytes: bytes) -> dict:
    """Reads back the fields build_fund_update_workbook() wrote into a
    previous report, so a repeat /fundupdate for the same client can reuse
    them instead of re-asking from scratch. Live fund prices, the
    1st-anniversary date and "current" date are never reused here — those
    get recomputed/refetched fresh on every update; only the data this
    function can't recompute (product, policy number, commencement date,
    total invested, ref illustration, fund list + allocations) is read
    back."""
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=False)
    ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active

    product = ws["B1"].value
    policy_number = ws["B2"].value

    commencement_date = ws["B3"].value
    if isinstance(commencement_date, datetime):
        commencement_date = commencement_date.date()
    elif not isinstance(commencement_date, date):
        commencement_date = None

    total_invested = _parse_dollar_amount(ws["E1"].value)

    ref_illustration = ws["E3"].value
    if isinstance(ref_illustration, str) and not ref_illustration.strip():
        ref_illustration = None

    funds: list[dict] = []
    r = 6
    while True:
        name = ws[f"B{r}"].value
        if not name:
            break
        allocation_raw = ws[f"A{r}"].value
        allocation_pct = float(allocation_raw) * 100 if isinstance(allocation_raw, (int, float)) else None
        funds.append({"name": str(name).strip(), "allocation_pct": allocation_pct})
        r += 1

    return {
        "product": product,
        "policy_number": policy_number,
        "commencement_date": commencement_date,
        "total_invested": total_invested,
        "ref_illustration": ref_illustration,
        "funds": funds,
    }


async def load_existing(client_name: str) -> Optional[dict]:
    """Looks up this client's most recent "ILP Funds Update" report on
    OneDrive (same path save_to_onedrive() writes to) and returns its
    reusable fields, or None if there's no prior report for this client, or
    it couldn't be read/parsed. Used by the /fundupdate wizard to offer
    pre-filling a repeat update instead of starting from scratch, the same
    way policy_workbook.py lets the Policy Summary wizard build on an
    existing client folder."""
    filename = _filename(client_name)
    remote_path = _onedrive_remote_path(client_name, filename)
    try:
        xlsx_bytes = await onedrive_service.download_bytes(remote_path)
    except Exception:
        logger.exception(
            "Failed to check OneDrive for an existing fund update report (client=%s)",
            client_name,
        )
        return None
    if xlsx_bytes is None:
        return None
    try:
        return _parse_existing(xlsx_bytes)
    except Exception:
        logger.exception(
            "Failed to parse existing fund update report (client=%s)", client_name
        )
        return None


async def save_to_onedrive(client_name: str, xlsx_bytes: bytes) -> str:
    """Uploads the workbook to Client/<name>/ on OneDrive (same convention as
    policy_workbook.py), overwriting any previous fund-update report for this
    client. Returns the filename used. Raises whatever onedrive_service
    raises (e.g. OneDriveNotConfigured) — caller decides how to surface that
    to Nic; the file itself is still fine to send over Telegram either way."""
    filename = _filename(client_name)
    remote_path = _onedrive_remote_path(client_name, filename)
    await onedrive_service.upload_bytes(remote_path, xlsx_bytes)
    return filename
