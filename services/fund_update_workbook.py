"""Builds "ILP Funds Update" workbooks, matching Nic's Lau Jing Wen report
(OneDrive Client/Dionne/ILP_Funds_Update_Lau_Jing_Wen.xlsx) cell-for-cell:
commencement / 1st-anniversary / current fund prices, allocation, gain/loss
since the anniversary, remarks — parameterized so the Telegram bot can
generate one for any client from a short chat wizard instead of a one-off
manual build.

One workbook per client, one sheet per policy (see _sheet_title_for /
save_to_onedrive) — a client with several ILP policies gets a tab for each
instead of the latest update overwriting the others.

Mirrors services/policy_workbook.py's conventions (OneDrive folder layout,
filename sanitizing) but is otherwise a self-contained builder — this report
has nothing to do with the Policy Summary sheet layout.
"""
from __future__ import annotations

import io
import logging
import re
from copy import copy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import openpyxl
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.worksheet.dimensions import SheetFormatProperties
from openpyxl.worksheet.worksheet import Worksheet

from services import onedrive_service

logger = logging.getLogger("assistant-bot.fund_update_workbook")

# Legacy single-sheet name, from before a client's workbook could hold more
# than one policy. New sheets are titled per-policy (see _sheet_title_for);
# this is kept only as a fallback for _parse_existing() reading an older
# file that still uses it.
SHEET_NAME = "ILP Funds Update"
ONEDRIVE_WORKBOOK_FOLDER = "Client"

# House style, copied cell-by-cell from Lau Jing Wen's report: Calibri 11
# throughout, navy for labels/most values, a distinct blue for the "current
# price" column, bold green/red for +/-, and gray for the remark column and
# the REF POLICY ILLUSTRATION value specifically.
NAVY = "FF44546A"
BLUE = "FF4472C4"
BORDER_GRAY = "FFA6A6A6"
GREEN = "FF00B050"
RED = "FFFF0000"
GREY = "FF999999"

# Page header/footer text, copied verbatim (including its own spacing
# quirks) from Nic's template — confirmed this should be reused as-is.
_HEADER_TEXT = "P R O P E R T Y O F  C A S S  N A O M I  P O H  O R G A N I S A T I ON"
_FOOTER_TEXT = "&K03+000P R I V A T E  A N D  C O N F I D E N T I A L"

_ONEDRIVE_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SHEET_ILLEGAL_CHARS = re.compile(r'[:\\/?*\[\]]')


def _onedrive_safe_name(text: str | None, fallback: str = "Unknown Client") -> str:
    cleaned = _ONEDRIVE_ILLEGAL_CHARS.sub("", (text or "")).strip().rstrip(".")
    return cleaned or fallback


def _sheet_title_for(data: "FundUpdateData") -> str:
    """A client can hold more than one policy, so each policy gets its own
    tab in that client's single workbook (see save_to_onedrive) instead of
    one policy's update overwriting another's. Named after the policy
    number — the one field that's always unique per policy — falling back
    to the product name, then a generic label, if it's ever missing.
    Excel sheet names can't contain : \\ / ? * [ ] and are capped at 31
    chars, so this sanitizes and truncates the same way _onedrive_safe_name
    does for folder/file names."""
    raw = data.policy_number or data.product or "Policy"
    safe = _SHEET_ILLEGAL_CHARS.sub("", raw).strip()
    return (safe or "Policy")[:31]


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


def _thin(color: str) -> Side:
    return Side(style="thin", color=color)


def _box(ws: Worksheet, cell_range: str, color: str = BORDER_GRAY, full_grid: bool = False) -> None:
    """Draws a border around cell_range. full_grid=True boxes every cell
    (a real grid, used for the fund table); otherwise only the outer
    perimeter gets a border (used for the ACTION/prompt boxes)."""
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
    # Lau Jing Wen's workbook has its un-styled "Normal" cell style (cellXfs
    # index 0) set to Calibri size 12, not openpyxl's default of size 11.
    # That default-font size is what Excel/LibreOffice use as the basis for
    # converting a column's stored character-count width into actual pixels
    # — it's not just cosmetic. With our columns sized to the reference's
    # exact widths but openpyxl's narrower size-11 basis, short labels like
    # "COMMENCEMENT" render wide enough to eat the column's entire buffer
    # and butt straight up against the next cell ("COMMENCEMENT4-Oct-24")
    # instead of leaving the gap the reference shows. Mutating wb._fonts[0]
    # (the registry the style writer actually reads from at save time —
    # DEFAULT_FONT and the "Normal" named style's own .font are both
    # ignored at save time) matches that basis without having to fudge the
    # column widths themselves.
    wb._fonts[0].sz = 12
    ws = wb.active
    ws.title = _sheet_title_for(data)
    # Matches the reference's <sheetFormatPr> exactly (baseColWidth="10"
    # defaultColWidth="12.1640625" defaultRowHeight="25"), part of the same
    # width-basis fix above.
    ws.sheet_format = SheetFormatProperties(
        baseColWidth=10, defaultColWidth=12.1640625, defaultRowHeight=25, customHeight=True
    )

    n_funds = len(data.funds)
    if n_funds == 0:
        raise ValueError("Need at least one fund to build a report")

    first_fund_row = 6
    last_fund_row = first_fund_row + n_funds - 1
    action_row = last_fund_row + 2
    action_end_row = action_row + 1
    prompt_rows_start = action_end_row + 2

    # Every styled cell gets an explicit Calibri/11 — leaving name/size
    # unset makes some renderers fall back to a wider substitute font,
    # which is wide enough to clip "COMMENCEMENT" and "POLICY NUMBER"
    # against the column next to them even though they fit comfortably
    # in real Calibri.
    def styled_font(color: str = NAVY, bold: bool = False) -> Font:
        return Font(name="Calibri", size=11, color=color, bold=bold)

    # --- Header block -------------------------------------------------
    # No client name in the body by design (confirmed with Nic) — just the
    # product under a generic "POLICY" label, same as POLICY NUMBER and
    # COMMENCEMENT below it. The right-side labels sit in column E (not D
    # — the fund table below needs three price columns, C/D/E, so the
    # right-hand block is pushed one column over) and are right-aligned so
    # a long one (REF POLICY ILLUSTRATION:) overflows left into the empty
    # C/D gap instead of clipping against its value.
    ws["A1"] = "POLICY "
    ws["A1"].font = styled_font()
    ws["A1"].alignment = Alignment(vertical="center")
    ws["B1"] = data.product
    ws["B1"].font = styled_font()
    ws["B1"].alignment = Alignment(vertical="center")
    ws["E1"] = "TOTAL INVESTMENT:"
    ws["E1"].font = styled_font()
    ws["E1"].alignment = Alignment(horizontal="right", vertical="center")
    ws["F1"] = data.total_invested
    ws["F1"].number_format = '"$"#,##0'
    ws["F1"].font = styled_font()
    ws["F1"].alignment = Alignment(vertical="center")
    ws["F1"].border = Border(bottom=_thin(BORDER_GRAY))
    ws["G1"].alignment = Alignment(vertical="center")
    ws["G1"].border = Border(bottom=_thin(BORDER_GRAY))

    ws["A2"] = "POLICY NUMBER"
    ws["A2"].font = styled_font()
    ws["A2"].alignment = Alignment(vertical="center")
    ws["B2"] = data.policy_number
    ws["B2"].font = styled_font()
    ws["B2"].alignment = Alignment(vertical="center")
    ws["E2"] = "ACCOUNT VALUE:"
    ws["E2"].font = styled_font()
    ws["E2"].alignment = Alignment(horizontal="right", vertical="center")
    ws["F2"] = f"${data.account_value:,.0f} ({data.account_value_asof:%d/%m/%Y})"
    ws["F2"].number_format = '"$"#,##0'
    ws["F2"].font = styled_font()
    ws["F2"].alignment = Alignment(vertical="center")
    ws["F2"].border = Border(top=_thin(BORDER_GRAY), bottom=_thin(BORDER_GRAY))
    ws["G2"].alignment = Alignment(vertical="center")
    ws["G2"].border = Border(top=_thin(BORDER_GRAY), bottom=_thin(BORDER_GRAY))

    ws["A3"] = "COMMENCEMENT"
    ws["A3"].font = styled_font()
    ws["A3"].alignment = Alignment(vertical="center")
    ws["B3"] = data.commencement_date
    ws["B3"].number_format = "d-mmm-yy"
    ws["B3"].font = styled_font()
    ws["B3"].alignment = Alignment(horizontal="left", vertical="center")
    ws["E3"] = "REF POLICY ILLUSTRATION:"
    ws["E3"].font = styled_font()
    ws["E3"].alignment = Alignment(horizontal="right", vertical="center")
    # The ref-illustration value is styled gray (not navy) to read as a
    # secondary/reference figure, same as the Remark column.
    ws["F3"] = data.ref_illustration or "-"
    ws["F3"].font = styled_font(color=GREY)
    ws["F3"].alignment = Alignment(vertical="center")
    ws["F3"].border = Border(top=_thin(BORDER_GRAY), bottom=_thin(BORDER_GRAY))
    ws["G3"].alignment = Alignment(vertical="center")
    ws["G3"].border = Border(top=_thin(BORDER_GRAY), bottom=_thin(BORDER_GRAY))

    for row in (1, 2, 3):
        ws.row_dimensions[row].height = 25
    ws.row_dimensions[4].height = 15

    # --- Table header -------------------------------------------------
    headers = ["Allocation", "Fund", data.commencement_date, data.anniversary_date, data.current_date, "+/-", "Remark"]
    for col, value in zip("ABCDEFG", headers):
        cell = ws[f"{col}5"]
        cell.value = value
        cell.font = styled_font()
        # "Fund" (B5) is left like its column's data; every other header is
        # centered. Neither wraps — these are short fixed strings/dates.
        horizontal = None if col == "B" else "center"
        cell.alignment = Alignment(horizontal=horizontal, vertical="center")
    for col in ("C", "D", "E"):
        ws[f"{col}5"].number_format = "d-mmm-yy"
    ws.row_dimensions[5].height = 25

    # --- Fund rows ------------------------------------------------------
    for i, fund in enumerate(data.funds):
        r = first_fund_row + i
        ws[f"A{r}"] = fund.allocation_pct / 100
        ws[f"A{r}"].number_format = "0%"
        ws[f"A{r}"].font = styled_font()
        ws[f"A{r}"].alignment = Alignment(horizontal="center", vertical="center")

        ws[f"B{r}"] = fund.name
        ws[f"B{r}"].font = styled_font()
        ws[f"B{r}"].alignment = Alignment(vertical="center", wrap_text=True)

        ws[f"C{r}"] = fund.price_commencement
        ws[f"D{r}"] = fund.price_anniversary
        ws[f"C{r}"].font = styled_font()
        ws[f"D{r}"].font = styled_font()
        # The current-price column is picked out in blue so the most
        # recent figure reads distinctly from the two historical ones.
        ws[f"E{r}"] = fund.price_current
        ws[f"E{r}"].font = styled_font(color=BLUE)
        for col in ("C", "D", "E"):
            ws[f"{col}{r}"].number_format = '"$"#,##0.000'
            ws[f"{col}{r}"].alignment = Alignment(horizontal="center", vertical="center")

        change = (
            (fund.price_current - fund.price_anniversary) / fund.price_anniversary
            if fund.price_anniversary
            else 0.0
        )
        cell = ws[f"F{r}"]
        cell.value = change
        cell.number_format = "0%"
        cell.font = styled_font(color=GREEN if change >= 0 else RED, bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

        if fund.remark is not None:
            ws[f"G{r}"] = fund.remark
            if isinstance(fund.remark, (int, float)):
                ws[f"G{r}"].number_format = '_("$"* #,##0_);_("$"* \\(#,##0\\);_("$"* "-"??_);_(@_)'
        ws[f"G{r}"].font = styled_font(color=GREY)
        ws[f"G{r}"].alignment = Alignment(horizontal="center", vertical="center")

        ws.row_dimensions[r].height = 25

    _box(ws, f"A5:G{last_fund_row}", full_grid=True)
    ws.column_dimensions["C"].hidden = True
    ws.row_dimensions[last_fund_row + 1].height = 15

    # --- Action section -----------------------------------------------
    # One merged, boxed, two-row cell — "ACTION:" then a dated line for
    # Nic to continue typing his notes after.
    ws.merge_cells(f"A{action_row}:G{action_end_row}")
    action_date = data.action_date_label or data.current_date
    ws[f"A{action_row}"] = f"ACTION:\n\n{action_date:%d/%m/%Y} - "
    ws[f"A{action_row}"].font = styled_font()
    ws[f"A{action_row}"].alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
    ws.row_dimensions[action_row].height = 25
    ws.row_dimensions[action_end_row].height = 60
    _box(ws, f"A{action_row}:G{action_end_row}", full_grid=False)

    # --- Prompt sections (boxed, same as the fund table / ACTION) --------
    prompts = [
        "Initial Objective of this investment\n",
        "Market Update in the last 12 months\n",
        "Outlook in the next 12 months\n",
    ]
    for i, prompt in enumerate(prompts):
        r = prompt_rows_start + i
        ws.merge_cells(f"A{r}:G{r}")
        ws[f"A{r}"] = prompt
        ws[f"A{r}"].font = styled_font()
        ws[f"A{r}"].alignment = Alignment(vertical="top", horizontal="left", wrap_text=True)
        ws.row_dimensions[r].height = 70
        _box(ws, f"A{r}:G{r}", full_grid=False)
    ws.row_dimensions[prompt_rows_start - 1].height = 15

    # --- Column widths / page setup --------------------------------------
    widths = {"A": 14.83203125, "B": 46.0, "C": 8.6640625, "D": 11.5, "E": 11.5, "F": 8.5, "G": 12.1640625, "H": 12.1640625}
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = 0.25
    ws.page_margins.right = 0.25
    ws.page_margins.top = 0.75
    ws.page_margins.bottom = 0.75
    ws.page_margins.header = 0.3
    ws.page_margins.footer = 0.3

    ws.oddHeader.center.text = _HEADER_TEXT
    ws.oddFooter.center.text = _FOOTER_TEXT

    # Matches how Nic's own reports are set up: no gridline clutter behind
    # the content, opened at 180% zoom.
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 180
    ws.sheet_view.zoomScaleNormal = 180

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
    # A client's workbook can hold a sheet per policy now (see
    # _sheet_title_for / save_to_onedrive). wb.active is whichever sheet was
    # saved/updated most recently — that's the best guess for "the policy
    # Nic probably means" when he hasn't said which one, and he gets a
    # chance to say no if it's the wrong one (the caller shows him the
    # policy number before offering to reuse it). SHEET_NAME is only
    # checked first for a pre-multi-policy file that still uses it.
    ws = wb[SHEET_NAME] if SHEET_NAME in wb.sheetnames else wb.active

    product = ws["B1"].value
    policy_number = ws["B2"].value

    commencement_date = ws["B3"].value
    if isinstance(commencement_date, datetime):
        commencement_date = commencement_date.date()
    elif not isinstance(commencement_date, date):
        commencement_date = None

    total_invested = _parse_dollar_amount(ws["F1"].value)

    ref_illustration = ws["F3"].value
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
    """Looks up this client's most recently updated policy sheet in their
    "ILP Funds Update" workbook on OneDrive (same path save_to_onedrive()
    writes to) and returns its reusable fields, or None if there's no prior
    report for this client, or it couldn't be read/parsed. If the client
    has more than one policy, this is a best guess (whichever was updated
    last) — the /fundupdate wizard shows the policy number before offering
    to reuse it, so Nic can say no if it's the wrong one. Used to offer
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


def _copy_worksheet(src: Worksheet, dst: Worksheet) -> None:
    """Copies everything build_fund_update_workbook() sets on a sheet — cell
    values/styles, merges, column/row sizing, hidden columns, sheet view,
    page setup, header/footer — from src into dst. openpyxl has no built-in
    cross-workbook sheet copy, so this does it cell-by-cell. Used by
    _merge_sheet() to add a policy's sheet into a client's existing
    workbook without disturbing any other policy's sheet already in it."""
    for row in src.iter_rows():
        for cell in row:
            new_cell = dst.cell(row=cell.row, column=cell.column, value=cell.value)
            if cell.has_style:
                new_cell.font = copy(cell.font)
                new_cell.border = copy(cell.border)
                new_cell.fill = copy(cell.fill)
                new_cell.alignment = copy(cell.alignment)
                new_cell.protection = copy(cell.protection)
                new_cell.number_format = cell.number_format

    for merged_range in src.merged_cells.ranges:
        dst.merge_cells(str(merged_range))

    for col, dim in src.column_dimensions.items():
        dst.column_dimensions[col].width = dim.width
        dst.column_dimensions[col].hidden = dim.hidden

    for row_idx, dim in src.row_dimensions.items():
        dst.row_dimensions[row_idx].height = dim.height

    dst.sheet_format.baseColWidth = src.sheet_format.baseColWidth
    dst.sheet_format.defaultColWidth = src.sheet_format.defaultColWidth
    dst.sheet_format.defaultRowHeight = src.sheet_format.defaultRowHeight

    dst.sheet_view.showGridLines = src.sheet_view.showGridLines
    dst.sheet_view.zoomScale = src.sheet_view.zoomScale
    dst.sheet_view.zoomScaleNormal = src.sheet_view.zoomScaleNormal

    dst.page_setup.orientation = src.page_setup.orientation
    dst.page_setup.fitToWidth = src.page_setup.fitToWidth
    dst.page_setup.fitToHeight = src.page_setup.fitToHeight
    dst.sheet_properties.pageSetUpPr.fitToPage = src.sheet_properties.pageSetUpPr.fitToPage
    dst.page_margins.left = src.page_margins.left
    dst.page_margins.right = src.page_margins.right
    dst.page_margins.top = src.page_margins.top
    dst.page_margins.bottom = src.page_margins.bottom
    dst.page_margins.header = src.page_margins.header
    dst.page_margins.footer = src.page_margins.footer

    dst.oddHeader.center.text = src.oddHeader.center.text
    dst.oddFooter.center.text = src.oddFooter.center.text


def _merge_sheet(existing_bytes: bytes, sheet_title: str, new_sheet_bytes: bytes) -> bytes:
    """Adds/replaces one policy's sheet inside a client's existing workbook,
    leaving every other sheet (other policies for the same client) intact.
    new_sheet_bytes is a standalone single-sheet workbook as returned by
    build_fund_update_workbook(); existing_bytes is whatever's currently
    saved at that client's OneDrive path."""
    target_wb = openpyxl.load_workbook(io.BytesIO(existing_bytes))
    source_ws = openpyxl.load_workbook(io.BytesIO(new_sheet_bytes)).active

    if sheet_title in target_wb.sheetnames:
        del target_wb[sheet_title]
    target_ws = target_wb.create_sheet(title=sheet_title)
    _copy_worksheet(source_ws, target_ws)
    target_wb.active = target_wb.sheetnames.index(sheet_title)

    buf = io.BytesIO()
    target_wb.save(buf)
    return buf.getvalue()


async def save_to_onedrive(client_name: str, data: FundUpdateData, xlsx_bytes: bytes) -> str:
    """Uploads the workbook to Client/<name>/ on OneDrive (same convention as
    policy_workbook.py). A client can have more than one policy, so rather
    than overwriting the whole file, this adds/replaces just this policy's
    sheet (named by policy number — see _sheet_title_for) inside whatever
    workbook's already there, keeping every other policy's sheet intact.
    The new/updated sheet is left as the active tab, so load_existing()'s
    "most recent" fallback picks it up correctly. Returns the filename
    used. Raises whatever onedrive_service raises (e.g.
    OneDriveNotConfigured) — caller decides how to surface that to Nic; the
    file itself is still fine to send over Telegram either way."""
    filename = _filename(client_name)
    remote_path = _onedrive_remote_path(client_name, filename)

    # download_bytes() returns None only for a genuine 404 (no prior report
    # for this client — the normal case for someone's first policy) and
    # raises for any real error. A real error has to propagate rather than
    # be treated as "no existing file": silently falling back to a fresh
    # single-sheet workbook here would overwrite and destroy any other
    # policy's sheet already saved for this client.
    existing_bytes = await onedrive_service.download_bytes(remote_path)

    if existing_bytes:
        xlsx_bytes = _merge_sheet(existing_bytes, _sheet_title_for(data), xlsx_bytes)

    await onedrive_service.upload_bytes(remote_path, xlsx_bytes)
    return filename
