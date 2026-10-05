"""Appends new leads and closed cases straight into Nic's
"2026 Sales Tracker - Nicholas.xlsx" workbook, which lives in a Google
Drive folder shared with him by his manager (Clarice) — it's a real
uploaded .xlsx, not a native Google Sheet, so the Sheets API (used by
sheets_service.py for receipts) can't touch it; this uses the Drive API
instead to download/upload the file's bytes.

Why not just `openpyxl.load_workbook(...).save(...)`? That workbook has a
chart on the Growth tab and legacy VML drawings, and a full openpyxl
load+save round trip is known to silently drop content it doesn't fully
understand. Instead, this edits only the raw XML of the one worksheet
being written to (Leads or Production), inside the .xlsx zip, leaving
every other part of the file — other tabs, the chart, styles, drawings —
byte-for-byte untouched.

Setup (see README): the Sales Tracker file (or its parent folder) must be
shared with the service account's email ("client_email" in the JSON key)
with "Editor" access, the same account already used for Calendar/Sheets,
and the Google Drive API must be enabled in that same Cloud project.
"""
from __future__ import annotations

import asyncio
import copy
import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from openpyxl.utils.datetime import to_excel

from config import settings

logger = logging.getLogger("assistant-bot.sales_tracker")

SCOPES = ["https://www.googleapis.com/auth/drive"]

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ET.register_namespace("", NS)
ET.register_namespace("r", R_NS)


def _tag(name: str) -> str:
    return f"{{{NS}}}{name}"


LEADS_SHEET = "Leads"
PRODUCTION_SHEET = "Production"

# column letter -> field key, in sheet order. "key_col" is the column used
# to decide whether a pre-built blank row already has data in it.
LEADS_FIELDS = [
    ("A", "date", "date"),
    ("B", "name", "text"),
    ("C", "contact", "text"),
    ("D", "lead_source", "text"),
    ("E", "last_contact", "date"),
    ("F", "next_follow_up", "date"),
    ("G", "appointment_date", "date"),
    ("H", "stage", "text"),
    ("I", "proposed_plan", "text"),
    ("J", "potential_premium", "number"),
    ("K", "potential_eape", "number"),
    ("L", "potential_fyc", "number"),
    ("M", "remark", "text"),
]
LEADS_KEY_COL = "B"  # Name

PRODUCTION_FIELDS = [
    ("A", "client_name", "text"),
    ("B", "life_assured", "text"),
    ("C", "policy_number", "text"),
    ("D", "product_category", "text"),
    ("E", "product_name", "text"),
    ("F", "issued_date", "date"),
    ("G", "payment_mode", "text"),
    ("H", "premium", "number"),
    ("I", "eape", "number"),
    ("J", "fyc", "number"),
    ("K", "remark", "text"),
]
PRODUCTION_KEY_COL = "A"  # Client name


class SalesTrackerNotConfigured(RuntimeError):
    pass


class SalesTrackerFull(RuntimeError):
    """Raised if we somehow run past the last pre-built blank row (1000)
    and can't safely fabricate a new one with matching formatting."""


_drive_service = None


def _get_drive_service():
    global _drive_service
    if _drive_service is not None:
        return _drive_service
    if not settings.sales_tracker_configured:
        raise SalesTrackerNotConfigured(
            "The Sales Tracker isn't set up yet — GOOGLE_SERVICE_ACCOUNT_JSON/FILE and "
            "GOOGLE_SALES_TRACKER_FILE_ID need to be configured (see README), and the file "
            "needs to be shared with the service account's email as an Editor."
        )
    creds = service_account.Credentials.from_service_account_info(
        settings.google_service_account_info, scopes=SCOPES
    )
    _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


def _download_bytes_sync(file_id: str) -> bytes:
    service = _get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _upload_bytes_sync(file_id: str, data: bytes) -> None:
    service = _get_drive_service()
    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        resumable=False,
    )
    service.files().update(fileId=file_id, media_body=media).execute()


def _sheet_xml_path(zf: zipfile.ZipFile, sheet_name: str) -> str:
    """Resolves a worksheet's zip path (e.g. "xl/worksheets/sheet2.xml") by
    its tab name, via workbook.xml + its rels — not hardcoded, so this
    keeps working even if Clarice reorders/renames/adds tabs later."""
    wb_xml = zf.read("xl/workbook.xml").decode("utf-8")
    wb_root = ET.fromstring(wb_xml)
    rid = None
    for sheet_el in wb_root.find(_tag("sheets")):
        if sheet_el.get("name") == sheet_name:
            rid = sheet_el.get(f"{{{R_NS}}}id")
            break
    if rid is None:
        raise RuntimeError(f"No tab named '{sheet_name}' found in the workbook.")

    rels_xml = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    rels_root = ET.fromstring(rels_xml)
    REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
    for rel in rels_root.findall(f"{{{REL_NS}}}Relationship"):
        if rel.get("Id") == rid:
            target = rel.get("Target")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise RuntimeError(f"Could not resolve the file for tab '{sheet_name}'.")


def _cell_has_value(c_el: Optional[ET.Element]) -> bool:
    if c_el is None:
        return False
    if c_el.find(_tag("v")) is not None:
        return True
    if c_el.find(_tag("is")) is not None:
        return True
    return False


def _row_cells(row_el: ET.Element) -> dict[str, ET.Element]:
    cells = {}
    for c_el in row_el.findall(_tag("c")):
        ref = c_el.get("r", "")
        m = re.match(r"([A-Z]+)(\d+)", ref)
        if m:
            cells[m.group(1)] = c_el
    return cells


def _find_first_blank_row(sheet_data: ET.Element, key_col: str) -> ET.Element:
    """First pre-built <row> (skipping row 1, the header) whose key column
    cell has no value yet. Pre-built blank rows already exist (with the
    right per-column styling) up to row 1000 in this workbook."""
    for row_el in sheet_data.findall(_tag("row")):
        r = int(row_el.get("r"))
        if r == 1:
            continue
        cells = _row_cells(row_el)
        if not _cell_has_value(cells.get(key_col)):
            return row_el
    raise SalesTrackerFull(
        "Ran out of pre-formatted blank rows (1000) in this tab — tell Nic to extend the "
        "sheet before logging more entries."
    )


def _parse_date(value) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _set_cell_text(c_el: ET.Element, text: str) -> None:
    for child in list(c_el):
        c_el.remove(child)
    c_el.set("t", "inlineStr")
    is_el = ET.SubElement(c_el, _tag("is"))
    t_el = ET.SubElement(is_el, _tag("t"))
    t_el.text = text
    # Preserve whitespace exactly as given (leading/trailing spaces, if any).
    t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _set_cell_number(c_el: ET.Element, number: float) -> None:
    for child in list(c_el):
        c_el.remove(child)
    if "t" in c_el.attrib:
        del c_el.attrib["t"]
    v_el = ET.SubElement(c_el, _tag("v"))
    v_el.text = repr(number) if isinstance(number, float) else str(number)


def _set_cell_date(c_el: ET.Element, d: date) -> None:
    for child in list(c_el):
        c_el.remove(child)
    if "t" in c_el.attrib:
        del c_el.attrib["t"]
    v_el = ET.SubElement(c_el, _tag("v"))
    v_el.text = str(to_excel(d))


def _apply_field(c_el: ET.Element, kind: str, raw_value) -> None:
    if raw_value is None or raw_value == "":
        return
    if kind == "number":
        try:
            _set_cell_number(c_el, float(raw_value))
        except (TypeError, ValueError):
            _set_cell_text(c_el, str(raw_value))
    elif kind == "date":
        parsed = _parse_date(raw_value)
        if parsed is not None:
            _set_cell_date(c_el, parsed)
        else:
            _set_cell_text(c_el, str(raw_value))
    else:
        _set_cell_text(c_el, str(raw_value))


def _append_row_sync(sheet_name: str, fields_spec: list[tuple[str, str, str]],
                      key_col: str, values: dict) -> int:
    file_id = settings.google_sales_tracker_file_id
    raw = _download_bytes_sync(file_id)

    with zipfile.ZipFile(io.BytesIO(raw), "r") as zin:
        sheet_path = _sheet_xml_path(zin, sheet_name)
        sheet_xml = zin.read(sheet_path).decode("utf-8")
        names = zin.namelist()
        infos = {i.filename: i for i in zin.infolist()}
        originals = {name: zin.read(name) for name in names}

    root = ET.fromstring(sheet_xml)
    sheet_data = root.find(_tag("sheetData"))
    row_el = _find_first_blank_row(sheet_data, key_col)
    row_num = int(row_el.get("r"))
    cells = _row_cells(row_el)

    for col, field_key, kind in fields_spec:
        if field_key not in values:
            continue
        c_el = cells.get(col)
        if c_el is None:
            # Shouldn't happen (pre-built rows have every column up to the
            # table width) but handle it rather than silently dropping data.
            c_el = ET.SubElement(row_el, _tag("c"))
            c_el.set("r", f"{col}{row_num}")
        _apply_field(c_el, kind, values[field_key])

    new_sheet_xml = ET.tostring(root, encoding="unicode")
    new_sheet_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + new_sheet_xml

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in names:
            data = new_sheet_xml.encode("utf-8") if name == sheet_path else originals[name]
            zout.writestr(infos[name], data)

    _upload_bytes_sync(file_id, out_buf.getvalue())
    return row_num


async def append_lead(fields: dict) -> int:
    """fields keys: date, name, contact, lead_source, last_contact,
    next_follow_up, appointment_date, stage, proposed_plan,
    potential_premium, potential_eape, potential_fyc, remark. Only `name`
    is really required; everything else is optional. Returns the row
    number written."""
    try:
        return await asyncio.to_thread(_append_row_sync, LEADS_SHEET, LEADS_FIELDS, LEADS_KEY_COL, fields)
    except HttpError as exc:
        logger.exception("append_lead failed")
        raise RuntimeError(f"Google Drive rejected the request: {exc.reason}") from exc


async def append_case(fields: dict) -> int:
    """fields keys: client_name, life_assured, policy_number,
    product_category, product_name, issued_date, payment_mode, premium,
    eape, fyc, remark. issued_date should be ISO (YYYY-MM-DD) — it feeds
    the Dashboard tab's formulas, so a case without a real issued_date
    won't show up in MTD/YTD/Monthly Production. Returns the row number
    written."""
    try:
        return await asyncio.to_thread(_append_row_sync, PRODUCTION_SHEET, PRODUCTION_FIELDS, PRODUCTION_KEY_COL, fields)
    except HttpError as exc:
        logger.exception("append_case failed")
        raise RuntimeError(f"Google Drive rejected the request: {exc.reason}") from exc
