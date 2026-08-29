"""Builds the client-facing Policy Summary PDF — the document a client gets
when they check their own policy through the (separate) client-facing bot.

This is a 1:1 replica of Nic's real Policy Summary sheet (the Johnson
reference layout in policy_workbook.py): same columns, same order, same
blue header band, same alternating row banding, same yellow Total row,
Times New Roman throughout, AND the same header/footer cell positions
(title under column I, name under F, DOB under J/K, Age Next Birthday
under P/Q, footer split A / P) — just rendered as a PDF table instead of
an Excel sheet, since it's going to a client rather than staying on
OneDrive.

Still excludes the Policy Illustration sheet and the Action Plan /
next-action notes (Nic presents those in person) — only the Policy
Summary sheet itself is mirrored here, Remarks column included.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Colors matched to policy_workbook.py's HEADER_FILL / BAND_FILL / TOTAL_FILL
# (FF99CCFF light blue, ~D9D9D9 light grey banding, FFFF00 yellow total row).
HEADER_BLUE = colors.HexColor("#99CCFF")
BAND_GREY = colors.HexColor("#D9D9D9")
TOTAL_YELLOW = colors.HexColor("#FFFF00")
BORDER_GREY = colors.HexColor("#999999")
LABEL_GREY = colors.Color(0x55 / 255, 0x55 / 255, 0x55 / 255)
CLIENT_NAME_RED = colors.HexColor("#FF0000")

_styles = getSampleStyleSheet()

# --- Header/footer styles - matched 1:1 to policy_workbook.py's TITLE_FONT /
# LABEL_FONT / CLIENT_NAME_FONT / DOB_FONT / FOOTER_FONT (all Times New
# Roman, left-aligned, no centering - Excel cells default to left-aligned
# text and that's how the real sheet reads).
TITLE_STYLE = ParagraphStyle(
    "ClientPdfTitle", parent=_styles["Normal"], fontName="Times-Bold", fontSize=20, alignment=0,
)
LABEL_STYLE = ParagraphStyle(
    # Matches LABEL_FONT: Times New Roman 14, not bold, black.
    "ClientPdfLabel", parent=_styles["Normal"], fontName="Times-Roman", fontSize=14, alignment=0,
)
CLIENT_NAME_STYLE = ParagraphStyle(
    # Matches CLIENT_NAME_FONT: Times New Roman 14, bold, red.
    "ClientPdfClientName", parent=_styles["Normal"], fontName="Times-Bold", fontSize=14,
    textColor=CLIENT_NAME_RED, alignment=0,
)
DOB_STYLE = ParagraphStyle(
    # Matches DOB_FONT: Times New Roman 12, bold, black - also used for the
    # Age Next Birthday value (Q3 shares DOB_FONT in the real sheet).
    "ClientPdfDob", parent=_styles["Normal"], fontName="Times-Bold", fontSize=12, alignment=0,
)
FOOTER_STYLE = ParagraphStyle(
    # Matches FOOTER_FONT: Times New Roman 10, not italic on the real sheet -
    # kept plain here too rather than the italic treatment used before.
    "ClientPdfFooterCell", parent=_styles["Normal"], fontName="Times-Roman", fontSize=10, alignment=0,
)
NOTE_STYLE = ParagraphStyle(
    "ClientPdfNote", parent=_styles["Normal"], fontName="Times-Italic", fontSize=8.5,
    textColor=LABEL_GREY, spaceBefore=6,
)
CELL_STYLE = ParagraphStyle(
    "ClientPdfCell", parent=_styles["Normal"], fontName="Times-Roman", fontSize=8,
    alignment=1, leading=9.5,
)
CELL_STYLE_LEFT = ParagraphStyle(
    "ClientPdfCellLeft", parent=CELL_STYLE, alignment=0,
)
HEADER_CELL_STYLE = ParagraphStyle(
    "ClientPdfHeaderCell", parent=_styles["Normal"], fontName="Times-Bold", fontSize=8,
    alignment=1, leading=9.5,
)
TOTAL_CELL_STYLE = ParagraphStyle(
    "ClientPdfTotalCell", parent=_styles["Normal"], fontName="Times-Bold", fontSize=9,
    alignment=1,
)

# (header text, dict key, decimals-or-None, relative width unit) — mirrors
# COLUMN_HEADERS / COLUMN_FIELDS / COLUMN_WIDTHS in policy_workbook.py, in
# the same A-through-Q order, so column N below lines up with column N here.
_COLUMNS = [
    ("No", None, None, 4.17),                                              # A
    ("Company", "company", None, 10.63),                                   # B
    ("Policy No", "policy_no", None, 16),                                  # C
    ("Payment Date", "payment_date", None, 13),                            # D
    ("Plan Type", "plan_type", None, 13),                                  # E
    ("Premium Annual\n(Cash)", "premium_cash", 2, 13),                     # F
    ("Premium Annual\n(CPF)", "premium_cpf", 2, 12.91),                    # G
    ("Payment Frequency", "payment_frequency", None, 11.03),               # H
    ("Mode of Payment", "mode_of_payment", None, 10.49),                   # I
    ("Total Death Coverage", "death_coverage", 0, 15.33),                  # J
    ("Total Permanent Disability Coverage", "tpd_coverage", 0, 14.39),     # K
    ("Critical Illness Coverage", "ci_coverage", 0, 10.36),                # L
    ("Early Stage Illness Coverage", "early_stage_coverage", 0, 11.43),    # M
    ("Disability Income\n(Per Mth)", "di_coverage", 0, 9.14),              # N
    ("Total Accident - Lump Sum", "accident_lump_sum", 0, 12.51),          # O
    ("Total Accident - Medical Reimbursement", "accident_medical", 0, 15.87),  # P
    ("Remarks", "remarks", None, 26),                                      # Q
]
# 0-based column indexes for the header/footer cell positions on the real
# sheet (A=0 ... Q=16).
COL_A, COL_F, COL_I, COL_J, COL_K, COL_P, COL_Q = 0, 5, 8, 9, 10, 15, 16

_NUMERIC_KEYS = {"premium_cash", "premium_cpf", "death_coverage", "tpd_coverage",
                  "ci_coverage", "early_stage_coverage", "di_coverage",
                  "accident_lump_sum", "accident_medical"}


def _fmt_money(value, decimals: int = 2):
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return f"${value:,.{decimals}f}"
    return str(value)


def _cell_value(policy: dict, key, decimals, row_no: int):
    if key is None:  # "No" column
        return str(row_no)
    raw = policy.get(key)
    if decimals is not None:
        return _fmt_money(raw, decimals)
    if raw in (None, ""):
        return ""
    return str(raw)


def _col_widths(page_width: float) -> list[float]:
    total_units = sum(w for *_r, w in _COLUMNS)
    return [(w / total_units) * page_width for *_r, w in _COLUMNS]


def _positioned_row(col_widths: list[float], placements: dict[int, tuple]) -> Table:
    """One borderless row aligned to the same 17-column grid as the data
    table, mirroring how Excel cells sit at fixed column letters. `placements`
    maps a start column index -> (Paragraph, end column index) for a span."""
    n = len(col_widths)
    row = [""] * n
    style = [
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]
    for start_col, (paragraph, end_col) in placements.items():
        row[start_col] = paragraph
        if end_col > start_col:
            style.append(("SPAN", (start_col, 0), (end_col, 0)))
    table = Table([row], colWidths=col_widths)
    table.setStyle(TableStyle(style))
    return table


def _build_table(policies: list[dict], totals: dict, col_widths: list[float]) -> Table:
    header_row = [Paragraph(h, HEADER_CELL_STYLE) for h, *_r in _COLUMNS]
    rows = [header_row]

    for i, policy in enumerate(policies, start=1):
        row = []
        for _h, key, decimals, _w in _COLUMNS:
            value = _cell_value(policy, key, decimals, i)
            style = CELL_STYLE_LEFT if key == "remarks" else CELL_STYLE
            row.append(Paragraph(value, style))
        rows.append(row)

    total_row = []
    for _h, key, decimals, _w in _COLUMNS:
        if key == "policy_no":
            total_row.append(Paragraph("Total", TOTAL_CELL_STYLE))
        elif key in _NUMERIC_KEYS:
            total_row.append(Paragraph(_fmt_money(totals.get(key), decimals or 0), TOTAL_CELL_STYLE))
        else:
            total_row.append(Paragraph("", TOTAL_CELL_STYLE))
    rows.append(total_row)

    table = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BLUE),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER_GREY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, -1), (-1, -1), TOTAL_YELLOW),
    ]
    for i in range(1, len(rows) - 1):
        if (i - 1) % 2 == 1:
            style.append(("BACKGROUND", (0, i), (-1, i), BAND_GREY))
    table.setStyle(TableStyle(style))
    return table


def build_client_policy_pdf(summary: dict, agent_name: str, output_dir: Path) -> Path:
    """summary is the dict from policy_workbook.get_client_summary(). Writes
    '<Client Name> - Policy Summary.pdf' into output_dir and returns the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in summary["client_name"] if c not in '<>:"/\\|?*').strip()
    out_path = output_dir / f"{safe_name} - Policy Summary.pdf"

    doc = SimpleDocTemplate(
        str(out_path), pagesize=landscape(A3),
        topMargin=12 * mm, bottomMargin=12 * mm, leftMargin=10 * mm, rightMargin=10 * mm,
    )
    col_widths = _col_widths(landscape(A3)[0] - 20 * mm)
    story = []

    # Row 1 (matches I1): title starts under column I, same as the real sheet.
    story.append(_positioned_row(col_widths, {
        COL_I: (Paragraph("Policy Summary", TITLE_STYLE), COL_Q),
    }))
    story.append(Spacer(1, 4 * mm))

    # Row 3 (matches A3/F3/J3/K3/P3): label+name+DOB+age, each under its own
    # column, not stacked/centered.
    client_name = escape(summary["client_name"])
    row3 = {
        COL_A: (Paragraph("Scope of Aggregated Coverage for", LABEL_STYLE), COL_F - 1),
        COL_F: (Paragraph(client_name, CLIENT_NAME_STYLE), COL_I),
    }
    if summary.get("date_of_birth"):
        row3[COL_J] = (Paragraph("Date of Birth:", LABEL_STYLE), COL_J)
        row3[COL_K] = (Paragraph(str(summary["date_of_birth"]), DOB_STYLE), COL_K + 2)
    if summary.get("age_next_birthday") is not None:
        row3[COL_P] = (
            Paragraph(f"Age Next Birthday: {summary['age_next_birthday']}", LABEL_STYLE), COL_Q,
        )
    story.append(_positioned_row(col_widths, row3))
    story.append(Spacer(1, 5 * mm))

    policies = summary.get("policies") or []
    if not policies:
        story.append(Paragraph("No policies on file yet.", _styles["Normal"]))
    else:
        story.append(_build_table(policies, summary.get("totals") or {}, col_widths))
    story.append(Spacer(1, 3 * mm))

    # Footer (matches "Updated as of ..." in column A / "Prepared by ..." in
    # column P on the real sheet).
    story.append(_positioned_row(col_widths, {
        COL_A: (Paragraph(f"Updated as of {date.today().strftime('%B %Y')}", FOOTER_STYLE), COL_F - 1),
        COL_P: (Paragraph(f"Prepared by {agent_name}", FOOTER_STYLE), COL_Q),
    }))
    story.append(Paragraph(
        "This summary reflects the policies currently on file — please reach out to discuss your coverage in full.",
        NOTE_STYLE,
    ))

    doc.build(story)
    return out_path
