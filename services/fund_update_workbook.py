"""Builds "ILP Funds Update" workbooks, matching the house template Nic
keeps at OneDrive Client/ILP Funds Update Template.xlsx (commencement vs
current fund price, allocation, gain/loss since inception, remarks) —
parameterized so the Telegram bot can generate one for any client from a
short chat wizard instead of a one-off manual build.

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
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.worksheet.worksheet import Worksheet

from services import onedrive_service

logger = logging.getLogger("assistant-bot.fund_update_workbook")

SHEET_NAME = "ILP Funds Update"
ONEDRIVE_WORKBOOK_FOLDER = "Client"

# House style, copied from the template: Calibri 11 throughout, a muted
# navy for all text, thin gray rules/borders, green/red only for the +/-
# column, and a lighter gray for the free-text remark column.
NAVY = "FF44546A"
BORDER_GRAY = "FFA6A6A6"
GREEN = "FF70AD47"
RED = "FFC00000"
REMARK_GRAY = "FFBFBFBF"

# Page header/footer text, copied verbatim (including its own spacing
# quirks) from the template — Nic confirmed this should be reused as-is.
_HEADER_TEXT = "P R O P E R T Y O F  C A S S  N A O M I  P O H  O R G A N I S A T I ON"
_FOOTER_TEXT = "&K03+000P R I V A T E  A N D  C O N F I D E N T I A L"

_ONEDRIVE_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _onedrive_safe_name(text: str | None, fallback: str = "Unknown Client") -> str:
    cleaned = _ONEDRIVE_ILLEGAL_CHARS.sub("", (text or "")).strip().rstrip(".")
    return cleaned or fallback


@dataclass
class FundRow:
    name: str
    allocation_pct: float  # e.g. 25 for 25%
    price_commencement: float
    price_current: float
    remark: Optional[str] = None


@dataclass
class FundUpdateData:
    client_name: str
    product: str
    policy_number: str
    commencement_date: date
    current_date: date
    total_invested: float
    account_value: float
    account_value_asof: date
    funds: list[FundRow]
    ref_illustration: Optional[str] = None  # e.g. "$13,911 (8% IRR)"
    action_date_label: Optional[date] = None  # defaults to current_date


def _thin(color: str) -> Side:
    return Side(style="thin", color=color)


def _box(ws: Worksheet, cell_range: str, color: str = BORDER_GRAY, full_grid: bool = False) -> None:
    """Draws a border around cell_range. full_grid=True boxes every cell
    (a real grid, used for the fund table); otherwise only the outer
    perimeter gets a border (used for the ACTION box)."""
    rows = list(ws[cell_range])
    n_rows = len(rows)
    for r_idx, row in enumerate(rows):
        n_cols = len(row)
        for c_idx, cell in enumerate(row):
            if full_grid:
                cell.border = Border(left=_thin(color), right=_thin(color), top=_thin(color), bottom=_thin(color))
                continue
            left = _thin(color) if c_idx == 0 else None
            right = _thin(color) if c_idx == n_cols - 1 else None
            top = _thin(color) if r_idx == 0 else None
            bottom = _thin(color) if r_idx == n_rows - 1 else None
            if left or right or top or bottom:
                cell.border = Border(left=left, right=right, top=top, bottom=bottom)


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

    # Every styled cell gets an explicit Calibri/11 — leaving name/size
    # unset makes some renderers fall back to a wider substitute font,
    # which is wide enough to clip "COMMENCEMENT" and "POLICY NUMBER"
    # against the column next to them even though they fit comfortably
    # in real Calibri (this bit Nic's Taufiq report before the font was
    # pinned down explicitly).
    def navy_font(color: str = NAVY, **kw) -> Font:
        return Font(name="Calibri", size=11, color=color, **kw)

    # --- Header block -------------------------------------------------
    # No client name in the body by design (Nic confirmed) — just the
    # product under a generic "POLICY" label, same as POLICY NUMBER and
    # COMMENCEMENT below it. The three right-side labels are right-aligned
    # so a long one (REF POLICY ILLUSTRATION:) overflows left into the
    # empty C column instead of getting clipped against its value cell.
    ws["A1"] = "POLICY"
    ws["A1"].font = navy_font()
    ws["B1"] = data.product
    ws["B1"].font = navy_font()
    ws["D1"] = "TOTAL INVESTMENT:"
    ws["D1"].font = navy_font()
    ws["D1"].alignment = Alignment(horizontal="right")
    ws.merge_cells("E1:F1")
    ws["E1"] = data.total_invested
    ws["E1"].number_format = '"$"#,##0'
    ws["E1"].font = navy_font()
    ws["E1"].alignment = Alignment(horizontal="center")
    ws["E1"].border = Border(bottom=_thin(BORDER_GRAY))

    ws["A2"] = "POLICY NUMBER"
    ws["A2"].font = navy_font()
    ws["B2"] = data.policy_number
    ws["B2"].font = navy_font()
    ws["D2"] = "ACCOUNT VALUE:"
    ws["D2"].font = navy_font()
    ws["D2"].alignment = Alignment(horizontal="right")
    ws.merge_cells("E2:F2")
    ws["E2"] = f"${data.account_value:,.0f} ({data.account_value_asof:%d/%m/%Y})"
    ws["E2"].font = navy_font()
    ws["E2"].alignment = Alignment(horizontal="center")
    ws["E2"].border = Border(bottom=_thin(BORDER_GRAY))

    ws["A3"] = "COMMENCEMENT"
    ws["A3"].font = navy_font()
    ws["B3"] = data.commencement_date
    ws["B3"].number_format = "d mmmm yyyy"
    ws["B3"].font = navy_font()
    ws["D3"] = "REF POLICY ILLUSTRATION:"
    ws["D3"].font = navy_font()
    ws["D3"].alignment = Alignment(horizontal="right")
    ws.merge_cells("E3:F3")
    ws["E3"] = data.ref_illustration or "-"
    ws["E3"].font = navy_font()
    ws["E3"].alignment = Alignment(horizontal="center")
    ws["E3"].border = Border(bottom=_thin(BORDER_GRAY))

    for row in (1, 2, 3):
        ws.row_dimensions[row].height = 25
    ws.row_dimensions[4].height = 15

    # --- Table header -------------------------------------------------
    # C = current price, D = commencement price (template puts the newer
    # date on the left, inception date on the right — not chronological,
    # but that's what Nic's template does).
    headers = ["Allocation", "Fund", data.current_date, data.commencement_date, "+/-", "Remark"]
    for col, value in zip("ABCDEF", headers):
        cell = ws[f"{col}5"]
        cell.value = value
        cell.font = navy_font()
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for col in ("C", "D"):
        ws[f"{col}5"].number_format = "d-mmm-yy"
    ws.row_dimensions[5].height = 25

    # --- Fund rows ------------------------------------------------------
    # The Fund column is narrower in this layout (30 vs. the old 46), so a
    # long fund name can need more than one wrapped line — bump that row's
    # height instead of leaving wrap_text to silently clip it against a
    # fixed 25pt row (same clipping failure mode as the header block,
    # just in the table this time).
    chars_per_line = 24
    for i, fund in enumerate(data.funds):
        r = first_fund_row + i
        ws[f"A{r}"] = fund.allocation_pct / 100
        ws[f"A{r}"].number_format = "0%"
        ws[f"A{r}"].font = navy_font()
        ws[f"A{r}"].alignment = Alignment(horizontal="center")

        ws[f"B{r}"] = fund.name
        ws[f"B{r}"].font = navy_font()
        ws[f"B{r}"].alignment = Alignment(vertical="center", wrap_text=True)

        ws[f"C{r}"] = fund.price_current
        ws[f"D{r}"] = fund.price_commencement
        for col in ("C", "D"):
            ws[f"{col}{r}"].number_format = '"$"#,##0.000'
            ws[f"{col}{r}"].font = navy_font()
            ws[f"{col}{r}"].alignment = Alignment(horizontal="center")

        # Gain/loss since the policy's commencement (not year-over-year —
        # the template only tracks two price points per fund, not three).
        change = (
            (fund.price_current - fund.price_commencement) / fund.price_commencement
            if fund.price_commencement
            else 0.0
        )
        cell = ws[f"E{r}"]
        cell.value = f"=(C{r}-D{r})/D{r}"
        cell.number_format = "0%"
        cell.font = navy_font(color=GREEN if change >= 0 else RED)
        cell.alignment = Alignment(horizontal="center")

        if fund.remark is not None:
            ws[f"F{r}"] = fund.remark
        ws[f"F{r}"].font = navy_font(color=REMARK_GRAY)
        ws[f"F{r}"].alignment = Alignment(horizontal="center")

        needed_lines = -(-len(fund.name) // chars_per_line)  # ceil div
        ws.row_dimensions[r].height = max(25.0, needed_lines * 14.0 + 6.0)

    _box(ws, f"A5:F{last_fund_row}", full_grid=True)

    # --- Action section -------------------------------------------------
    ws[f"A{action_row}"] = "ACTION:"
    ws[f"A{action_row}"].font = navy_font()
    ws.row_dimensions[action_row].height = 15

    ws.merge_cells(f"A{action_date_row}:F{action_date_row}")
    action_date = data.action_date_label or data.current_date
    ws[f"A{action_date_row}"] = f"{action_date:%d/%m/%Y} - "
    ws[f"A{action_date_row}"].font = navy_font()
    ws[f"A{action_date_row}"].alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    ws.row_dimensions[action_date_row].height = 60

    _box(ws, f"A{action_row}:F{action_date_row}", full_grid=False)

    # --- Prompt sections (no border, per the template) -------------------
    prompts = [
        "Initial Objective of this investment\n",
        "Market Update in the last 12 months\n",
        "Outlook in the next 12 months\n",
    ]
    for i, prompt in enumerate(prompts):
        r = prompt_rows_start + i
        ws.merge_cells(f"A{r}:F{r}")
        ws[f"A{r}"] = prompt
        ws[f"A{r}"].font = navy_font()
        ws[f"A{r}"].alignment = Alignment(vertical="top", horizontal="left", wrap_text=True)
        ws.row_dimensions[r].height = 70
    ws.row_dimensions[prompt_rows_start - 1].height = 15

    # --- Column widths / page setup --------------------------------------
    widths = {"A": 14.83, "B": 30.0, "C": 10.83, "D": 12.16, "E": 10.83, "F": 12.16}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    ws.oddHeader.center.text = _HEADER_TEXT
    ws.oddFooter.center.text = _FOOTER_TEXT

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
    them instead of re-asking from scratch. Live fund prices and the
    "current" date are never reused here — those get recomputed/refetched
    fresh on every update; only the data this function can't recompute
    (product, policy number, commencement date, total invested, ref
    illustration, fund list + allocations) is read back."""
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
    if isinstance(ref_illustration, str) and (not ref_illustration.strip() or ref_illustration.strip() == "-"):
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
