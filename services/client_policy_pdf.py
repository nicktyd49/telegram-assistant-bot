"""Builds the client-facing Policy Summary PDF — the document a client gets
when they check their own policy through the (separate) client-facing bot.

Deliberately narrower than what Nic sees himself: only the objective policy
facts from the Policy Summary sheet (company, plan, policy number, premiums,
coverage amounts). No Policy Illustration (Nic presents that in person), no
Action Plan / next-action notes, and no `remarks` field, since that can hold
notes written for Nic rather than the client.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

HEADER_BLUE = colors.Color(0x99 / 255, 0xCC / 255, 0xFF / 255)
LABEL_GREY = colors.Color(0x55 / 255, 0x55 / 255, 0x55 / 255)
BORDER_GREY = colors.Color(0xBB / 255, 0xBB / 255, 0xBB / 255)

_styles = getSampleStyleSheet()
TITLE_STYLE = ParagraphStyle(
    "ClientPdfTitle", parent=_styles["Title"], fontName="Times-Bold", fontSize=20, spaceAfter=2,
)
SUBTITLE_STYLE = ParagraphStyle(
    "ClientPdfSubtitle", parent=_styles["Normal"], fontName="Times-Roman", fontSize=11,
    textColor=LABEL_GREY, spaceAfter=0,
)
FOOTER_STYLE = ParagraphStyle(
    "ClientPdfFooter", parent=_styles["Normal"], fontName="Times-Italic", fontSize=9,
    textColor=LABEL_GREY, spaceBefore=18,
)


def _fmt_money(value, decimals: int = 2):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return f"${value:,.{decimals}f}"
    return str(value)


# (label, dict key, decimals) - only rows with a real value on the policy get
# printed, same convention as the Telegram lookup uses.
_DETAIL_ROWS = [
    ("Policy No", "policy_no", None),
    ("Payment / Coverage Period", "payment_date", None),
    ("Premium (Cash)", "premium_cash", 2),
    ("Premium (CPF)", "premium_cpf", 2),
    ("Death Coverage", "death_coverage", 0),
    ("Total Permanent Disability", "tpd_coverage", 0),
    ("Critical Illness", "ci_coverage", 0),
    ("Early Stage Illness", "early_stage_coverage", 0),
    ("Disability Income (per mth)", "di_coverage", 0),
    ("Accident (Lump Sum)", "accident_lump_sum", 0),
    ("Accident (Medical Reimbursement)", "accident_medical", 0),
]


def _policy_table(policy: dict, header: str) -> Table:
    # Matches the look of the real Policy Summary workbook (see
    # policy_workbook.py's HEADER_FILL/CELL_BORDER): the same light-blue
    # header band (FF99CCFF) and a bordered grid on every cell, not just a
    # plain label/value list - this should read as the same document family
    # as what Nic already sends himself, not a generic export.
    rows = [[header, ""]]
    for label, key, decimals in _DETAIL_ROWS:
        raw = policy.get(key)
        value = _fmt_money(raw, decimals) if decimals is not None else (str(raw) if raw not in (None, "") else None)
        if value is None:
            continue
        rows.append([label, value])

    table = Table(rows, colWidths=[65 * mm, 90 * mm])
    style = [
        ("SPAN", (0, 0), (1, 0)),
        ("BACKGROUND", (0, 0), (1, 0), HEADER_BLUE),
        ("FONTNAME", (0, 0), (1, 0), "Times-Bold"),
        ("FONTSIZE", (0, 0), (1, 0), 12),
        ("ALIGN", (0, 0), (1, 0), "CENTER"),
        ("TOPPADDING", (0, 0), (1, 0), 6),
        ("BOTTOMPADDING", (0, 0), (1, 0), 6),
        ("FONTNAME", (0, 1), (0, -1), "Times-Roman"),
        ("FONTNAME", (1, 1), (1, -1), "Times-Roman"),
        ("FONTSIZE", (0, 1), (-1, -1), 10.5),
        ("TEXTCOLOR", (0, 1), (0, -1), LABEL_GREY),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.6, BORDER_GREY),
    ]
    table.setStyle(TableStyle(style))
    return table


def build_client_policy_pdf(summary: dict, agent_name: str, output_dir: Path) -> Path:
    """summary is the dict from policy_workbook.get_client_summary(). Writes
    '<Client Name> - Policy Summary.pdf' into output_dir and returns the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in summary["client_name"] if c not in '<>:"/\\|?*').strip()
    out_path = output_dir / f"{safe_name} - Policy Summary.pdf"

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
    )
    story = [Paragraph("Policy Summary", TITLE_STYLE)]

    subtitle_lines = [summary["client_name"]]
    if summary.get("date_of_birth"):
        subtitle_lines.append(f"Date of Birth: {summary['date_of_birth']}")
    subtitle_lines.append(f"As of {date.today().strftime('%d %B %Y')}")
    for line in subtitle_lines:
        story.append(Paragraph(line, SUBTITLE_STYLE))

    policies = summary.get("policies") or []
    if not policies:
        story.append(Spacer(1, 14 * mm))
        story.append(Paragraph("No policies on file yet.", _styles["Normal"]))
    for i, policy in enumerate(policies, start=1):
        company = policy.get("company") or "?"
        plan = policy.get("plan_type") or ""
        header = f"Policy {i}: {company} — {plan}" if plan else f"Policy {i}: {company}"
        story.append(Spacer(1, 10 * mm))
        story.append(_policy_table(policy, header))

    totals = summary.get("totals") or {}
    total_cash = _fmt_money(totals.get("premium_cash"))
    total_cpf = _fmt_money(totals.get("premium_cpf"))
    total_bits = [f"{v} cash" if k == "cash" else f"{v} CPF"
                  for k, v in (("cash", total_cash), ("cpf", total_cpf)) if v]
    if total_bits:
        story.append(Spacer(1, 8 * mm))
        story.append(Paragraph(f"<b>Total Annual Premium:</b> {' + '.join(total_bits)}", _styles["Normal"]))

    story.append(Paragraph(
        f"Prepared by {agent_name}. This summary reflects the policies currently on file and is for your "
        "reference — please reach out to discuss your coverage in full.",
        FOOTER_STYLE,
    ))

    doc.build(story)
    return out_path
