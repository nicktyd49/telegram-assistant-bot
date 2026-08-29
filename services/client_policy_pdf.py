"""Builds the client-facing Policy Summary PDF — the document a client gets
when they check their own policy through the (separate) client-facing bot.

This is a 1:1 replica of Nic's real Policy Summary sheet (the Johnson
reference layout in policy_workbook.py): same columns, same order, same
blue header band, same alternating row banding, same yellow Total row,
Times New Roman throughout — just rendered as a PDF table instead of an
Excel sheet, since it's going to a client rather than staying on OneDrive.

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
TITLE_STYLE = ParagraphStyle(
    "ClientPdfTitle", parent=_styles["Title"], fontName="Times-Bold", fontSize=20,
    alignment=1, spaceAfter=4,
)
NAME_STYLE = ParagraphStyle(
    # Base style is the plain black "Scope of Aggregated Coverage for" label
    # (matches LABEL_FONT on the real sheet's A3 cell) - the client name
    # itself is wrapped in inline <b><font color="red"> markup below so only
    # the name matches CLIENT_NAME_FONT (F3: bold red), not the whole line.
    "ClientPdfName", parent=_styles["Normal"], fontName="Times-Roman", fontSize=14,
    alignment=1, spaceAfter=2,
)
SUBTITLE_STYLE = ParagraphStyle(
    "ClientPdfSubtitle", parent=_styles["Normal"], fontName="Times-Roman", fontSize=11,
    textColor=LABEL_GREY, alignment=1, spaceAfter=0,
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
FOOTER_STYLE = ParagraphStyle(
    "ClientPdfFooter", parent=_styles["Normal"], fontName="Times-Italic", fontSize=9,
    textColor=LABEL_GREY, spaceBefore=10,
)

# (header text, dict key, decimals-or-None, relative width unit) — mirrors
# COLUMN_HEADERS / COLUMN_FIELDS / COLUMN_WIDTHS in policy_workbook.py.
_COLUMNS = [
    ("No", None, None, 4.17),
    ("Company", "company", None, 10.63),
    ("Policy No", "policy_no", None, 16),
    ("Payment Date", "payment_date", None, 13),
    ("Plan Type", "plan_type", None, 13),
    ("Premium Annual\n(Cash)", "premium_cash", 2, 13),
    ("Premium Annual\n(CPF)", "premium_cpf", 2, 12.91),
    ("Payment Frequency", "payment_frequency", None, 11.03),
    ("Mode of Payment", "mode_of_payment", None, 10.49),
    ("Total Death Coverage", "death_coverage", 0, 15.33),
    ("Total Permanent Disability Coverage", "tpd_coverage", 0, 14.39),
    ("Critical Illness Coverage", "ci_coverage", 0, 10.36),
    ("Early Stage Illness Coverage", "early_stage_coverage", 0, 11.43),
    ("Disability Income\n(Per Mth)", "di_coverage", 0, 9.14),
    ("Total Accident - Lump Sum", "accident_lump_sum", 0, 12.51),
    ("Total Accident - Medical Reimbursement", "accident_medical", 0, 15.87),
    ("Remarks", "remarks", None, 26),
]

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


def _build_table(policies: list[dict], totals: dict) -> Table:
    total_width = sum(w for *_r, w in _COLUMNS)
    page_width = landscape(A3)[0] - 20 * mm  # minus left+right margins
    col_widths = [(w / total_width) * page_width for *_r, w in _COLUMNS]

    header_row = [Paragraph(h, HEADER_CELL_STYLE) for h, *_r in _COLUMNS]
    rows = [header_row]

    for i, policy in enumerate(policies, start=1):
        row = []
        for col_idx, (_h, key, decimals, _w) in enumerate(_COLUMNS):
            value = _cell_value(policy, key, decimals, i)
            style = CELL_STYLE_LEFT if key == "remarks" else CELL_STYLE
            row.append(Paragraph(value, style))
        rows.append(row)

    total_row = []
    for col_idx, (_h, key, decimals, _w) in enumerate(_COLUMNS):
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
        # Yellow total row (last row)
        ("BACKGROUND", (0, -1), (-1, -1), TOTAL_YELLOW),
    ]
    # Alternating light-grey banding on data rows, matching _style_data_row.
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
    story = [Paragraph("Policy Summary", TITLE_STYLE)]
    safe_client_name = escape(summary["client_name"])
    story.append(Paragraph(
        f'Scope of Aggregated Coverage for <font color="#FF0000"><b>{safe_client_name}</b></font>',
        NAME_STYLE,
    ))
    if summary.get("date_of_birth"):
        story.append(Paragraph(f"Date of Birth: {summary['date_of_birth']}", SUBTITLE_STYLE))
    story.append(Spacer(1, 6 * mm))

    policies = summary.get("policies") or []
    if not policies:
        story.append(Paragraph("No policies on file yet.", _styles["Normal"]))
    else:
        story.append(_build_table(policies, summary.get("totals") or {}))

    story.append(Paragraph(
        f"Updated as of {date.today().strftime('%B %Y')}  |  Prepared by {agent_name}. "
        "This summary reflects the policies currently on file — please reach out to discuss your coverage in full.",
        FOOTER_STYLE,
    ))

    doc.build(story)
    return out_path
