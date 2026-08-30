"""Turns whatever Nic sends during a /poster session (typed notes plus
photos of a fund house's slides) into a client-ready market outlook poster,
using the same two-step split every other extraction feature in this bot
uses: (1) ask Claude to read the raw material and pull out a small
structured brief, (2) render that brief into a fixed-layout image with
Pillow — no browser/HTML involved, since this needs to work as a plain PNG
Telegram can just display in a chat.

Two Claude calls happen nowhere here — extraction is one call. Rendering is
pure Pillow, so it's fast, free, and doesn't depend on the model "drawing"
anything.
"""
from __future__ import annotations

import json
import logging
import textwrap as _textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from config import settings

logger = logging.getLogger("assistant-bot.poster_service")

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS_DIR / name), size)


# ---------------------------------------------------------------- palette --
INK = (22, 33, 62)
INK_SOFT = (75, 85, 104)
PAPER = (246, 243, 236)
CARD = (255, 255, 255)
ACCENT = (62, 98, 89)       # teal — "this fund"
ACCENT_TINT = (222, 232, 227)
GOLD = (169, 130, 90)       # "the index" / secondary series
GOLD_TINT = (239, 228, 212)
LINE = (214, 206, 187)

# ---------------------------------------------------------------- geometry --
WIDTH = 1200
MARGIN = 72
CONTENT_W = WIDTH - 2 * MARGIN
TOP_BAND_H = 10


POSTER_SYSTEM_PROMPT = """You turn a Singapore life insurance agent's raw notes from a fund house \
presentation into a short, structured brief for a client-facing poster. You will be given a mix \
of typed notes and photos of presentation slides, in the order they were sent.

Slides are the fund house's own material (facts, figures, philosophy). Typed notes in between are \
the AGENT'S OWN commentary and analysis — keep these two sources distinct and never blend them.

Return ONLY a single JSON object (no markdown fences, no commentary) matching exactly this shape:

{
  "fund_name": "short fund/strategy name, e.g. 'Amundi US Equity Fundamental Growth'",
  "title": "a short, plain-language headline for the poster, under 9 words, no jargon",
  "dek": "one sentence subtitle expanding on the headline, under 22 words",
  "date_label": "e.g. 'Data as of 30 Jun 2026' if a data-as-of date appears in the slides, else empty string",
  "philosophy": [ {"title": "2-4 words", "desc": "under 18 words, plain language"} ],
  "positioning": [ {"label": "sector or theme name, under 3 words", "fund_pct": 0.0, "bench_pct": 0.0, "bench_name": "index name if stated, else 'Benchmark'"} ],
  "agent_notes": [ "the agent's own typed commentary, lightly cleaned up for grammar but keep their voice and meaning exactly — do not invent opinions they didn't write" ],
  "stats": [ {"value": "e.g. '35' or '27.4x' or '18.1%'", "label": "under 8 words explaining what it is"} ]
}

Rules:
- philosophy: 0-4 items — only include if the slides actually present an investment philosophy/approach with distinct named pillars. Otherwise return [].
- positioning: 0-6 items — only include if slides show a portfolio-vs-benchmark sector/weight comparison with actual percentages. Numbers must come directly from what's shown, not estimated.
- agent_notes: the single most important section — pull EVERY distinct piece of the agent's own typed commentary (not slide text). Keep each item to 1-2 sentences. If there are no typed notes at all, return [].
- stats: 0-6 items — only pull figures that are clearly labeled fund statistics (holdings count, P/E, beta, EPS growth, market cap, active share, etc). Use the exact figure shown.
- Never fabricate a number or claim that isn't visible in the material. Omit a field's items entirely rather than guessing.
- title/dek must be your own plain-language summary, not copied slide headlines verbatim.
"""


async def extract_poster_content(parts: list[dict]) -> dict:
    """parts is an ordered list of either {"type": "text", "text": str} or
    {"type": "image", "media_type": str, "data": base64-str}, exactly as
    Nic sent them during the session. Returns the parsed brief dict, or
    raises on failure (caller decides how to surface that)."""
    from assistant import anthropic_client  # local import avoids a cycle at module load

    content = []
    for part in parts:
        if part["type"] == "text":
            content.append({"type": "text", "text": part["text"]})
        else:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": part["media_type"], "data": part["data"]},
            })
    content.append({
        "type": "text",
        "text": "That's everything from this session — build the poster brief JSON now.",
    })

    response = await anthropic_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1536,
        system=POSTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    cleaned = raw.strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].lstrip()
    try:
        brief = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("Poster brief JSON parse failed: %s | raw=%s", exc, raw[:500])
        raise RuntimeError("Couldn't make sense of that material — try resending the notes/photos.") from exc

    for key, default in (
        ("philosophy", []), ("positioning", []), ("agent_notes", []), ("stats", []),
        ("date_label", ""), ("dek", ""), ("fund_name", ""), ("title", "Market Outlook"),
    ):
        brief.setdefault(key, default)
    return brief


# ------------------------------------------------------------- text layout --
def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines, current = [], words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _line_h(font: ImageFont.FreeTypeFont) -> int:
    asc, desc = font.getmetrics()
    return int((asc + desc) * 1.32)


def _tracked(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.FreeTypeFont,
             fill, tracking: int = 2) -> int:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += int(draw.textlength(ch, font=font)) + tracking
    return x


def _hr(draw: ImageDraw.ImageDraw, y: int) -> None:
    draw.line([(MARGIN, y), (WIDTH - MARGIN, y)], fill=LINE, width=1)


# ------------------------------------------------------------------ render --
def render_poster_image(brief: dict, signoff_name: str = "Nicholas Tan") -> bytes:
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).strftime("%d %B %Y")

    f_eyebrow = _font("DejaVuSansMono-Bold.ttf", 15)
    f_title = _font("STIXGeneralBol.ttf", 52)
    f_dek = _font("DejaVuSans.ttf", 21)
    f_chip = _font("DejaVuSansMono.ttf", 14)
    f_label = _font("DejaVuSansMono-Bold.ttf", 14)
    f_heading = _font("STIXGeneralBol.ttf", 29)
    f_pillar_title = _font("DejaVuSans-Bold.ttf", 19)
    f_pillar_desc = _font("DejaVuSans.ttf", 16)
    f_bar_label = _font("DejaVuSans-Bold.ttf", 16)
    f_bar_note = _font("DejaVuSansMono.ttf", 15)
    f_note = _font("DejaVuSans.ttf", 17)
    f_stat_val = _font("DejaVuSansMono-Bold.ttf", 30)
    f_stat_lab = _font("DejaVuSans.ttf", 14)
    f_footer_name = _font("STIXGeneralBol.ttf", 19)
    f_footer_role = _font("DejaVuSansMono.ttf", 13)
    f_disclaimer = _font("DejaVuSans.ttf", 13)

    # Generous scratch canvas — cropped to actual content height at the end.
    canvas = Image.new("RGB", (WIDTH, 4400), PAPER)
    draw = ImageDraw.Draw(canvas)

    draw.rectangle([0, 0, WIDTH, TOP_BAND_H], fill=INK)
    y = TOP_BAND_H + 46

    # ---- masthead ----
    _tracked(draw, (MARGIN, y), "FUND HOUSE TALK · NOTES FOR CLIENTS", f_eyebrow, GOLD, tracking=2)
    y += 40

    title_lines = _wrap(draw, brief.get("title") or "Market Outlook", f_title, CONTENT_W)
    for line in title_lines:
        draw.text((MARGIN, y), line, font=f_title, fill=INK)
        y += _line_h(f_title)
    y += 6

    dek = brief.get("dek") or ""
    if dek:
        for line in _wrap(draw, dek, f_dek, int(CONTENT_W * 0.82)):
            draw.text((MARGIN, y), line, font=f_dek, fill=INK_SOFT)
            y += _line_h(f_dek)
        y += 10

    chips = [c for c in [brief.get("fund_name"), brief.get("date_label"), f"NOTES BY {signoff_name.upper()}"] if c]
    cx = MARGIN
    chip_y = y
    for chip in chips:
        label = chip.upper()
        w = int(draw.textlength(label, font=f_chip)) + 22
        draw.rectangle([cx, chip_y, cx + w, chip_y + 30], outline=LINE, width=1, fill=CARD)
        draw.text((cx + 11, chip_y + 7), label, font=f_chip, fill=INK_SOFT)
        cx += w + 10
    y = chip_y + 30 + 34
    _hr(draw, y)
    y += 40

    # ---- philosophy ----
    philosophy = brief.get("philosophy") or []
    if philosophy:
        _tracked(draw, (MARGIN, y), "PHILOSOPHY", f_label, GOLD, tracking=2)
        y += 30
        draw.text((MARGIN, y), "What the fund manager is actually looking for", font=f_heading, fill=INK)
        y += _line_h(f_heading) + 14

        col_gap = 28
        col_w = (CONTENT_W - col_gap) // 2
        i = 0
        while i < len(philosophy):
            row = philosophy[i:i + 2]
            row_h = 0
            for j, item in enumerate(row):
                cx0 = MARGIN + j * (col_w + col_gap)
                title_lines2 = _wrap(draw, item.get("title", ""), f_pillar_title, col_w)
                desc_lines = _wrap(draw, item.get("desc", ""), f_pillar_desc, col_w)
                h = 4 + len(title_lines2) * _line_h(f_pillar_title) + 6 + len(desc_lines) * _line_h(f_pillar_desc) + 18
                row_h = max(row_h, h)
                ty = y
                draw.line([(cx0, y), (cx0, y + h - 18)], fill=ACCENT, width=3)
                for tl in title_lines2:
                    draw.text((cx0 + 16, ty), tl, font=f_pillar_title, fill=INK)
                    ty += _line_h(f_pillar_title)
                ty += 4
                for dl in desc_lines:
                    draw.text((cx0 + 16, ty), dl, font=f_pillar_desc, fill=INK_SOFT)
                    ty += _line_h(f_pillar_desc)
            y += row_h
            i += 2
        y += 20
        _hr(draw, y)
        y += 40

    # ---- positioning ----
    positioning = brief.get("positioning") or []
    if positioning:
        _tracked(draw, (MARGIN, y), "POSITIONING", f_label, GOLD, tracking=2)
        y += 30
        draw.text((MARGIN, y), "How the portfolio differs from the benchmark", font=f_heading, fill=INK)
        y += _line_h(f_heading) + 8

        bench_name = positioning[0].get("bench_name") or "Benchmark"
        legend_y = y
        draw.rectangle([MARGIN, legend_y + 4, MARGIN + 14, legend_y + 18], fill=ACCENT)
        draw.text((MARGIN + 22, legend_y), "This fund", font=f_bar_note, fill=INK_SOFT)
        lx = MARGIN + 22 + int(draw.textlength("This fund", font=f_bar_note)) + 26
        draw.rectangle([lx, legend_y + 4, lx + 14, legend_y + 18], fill=GOLD)
        draw.text((lx + 22, legend_y), bench_name, font=f_bar_note, fill=INK_SOFT)
        y += 34

        max_pct = max([p.get("fund_pct", 0) for p in positioning] + [p.get("bench_pct", 0) for p in positioning] + [1])
        bar_max = CONTENT_W - 210
        label_x = MARGIN
        bars_x = MARGIN + 190

        for item in positioning:
            label = item.get("label", "")
            fund_pct = float(item.get("fund_pct") or 0)
            bench_pct = float(item.get("bench_pct") or 0)
            draw.text((label_x, y), label, font=f_bar_label, fill=INK)

            bh = 13
            fw = int(bar_max * fund_pct / max_pct) if max_pct else 0
            draw.rectangle([bars_x, y, bars_x + bar_max, y + bh], outline=LINE, width=1)
            draw.rectangle([bars_x, y, bars_x + fw, y + bh], fill=ACCENT)
            draw.text((bars_x + bar_max + 14, y - 2), f"{fund_pct:g}%", font=f_bar_note, fill=INK)

            y2 = y + bh + 6
            bw = int(bar_max * bench_pct / max_pct) if max_pct else 0
            draw.rectangle([bars_x, y2, bars_x + bar_max, y2 + bh], outline=LINE, width=1)
            draw.rectangle([bars_x, y2, bars_x + bw, y2 + bh], fill=GOLD)
            draw.text((bars_x + bar_max + 14, y2 - 2), f"{bench_pct:g}%", font=f_bar_note, fill=INK_SOFT)

            y = y2 + bh + 20

        y += 6
        _hr(draw, y)
        y += 40

    # ---- agent notes ----
    notes = brief.get("agent_notes") or []
    if notes:
        _tracked(draw, (MARGIN, y), "AGENT'S NOTES", f_label, GOLD, tracking=2)
        y += 30
        draw.text((MARGIN, y), "My take, in plain terms", font=f_heading, fill=INK)
        y += _line_h(f_heading) + 16

        mark_w = 34
        text_x = MARGIN + mark_w
        text_w = CONTENT_W - mark_w
        for note in notes:
            lines = _wrap(draw, note, f_note, text_w)
            box_h = len(lines) * _line_h(f_note)
            draw.rectangle([MARGIN, y + 2, MARGIN + 22, y + 22], outline=ACCENT, width=2)
            draw.line([(MARGIN + 6, y + 12), (MARGIN + 18, y + 12)], fill=ACCENT, width=2)
            draw.line([(MARGIN + 12, y + 6), (MARGIN + 12, y + 18)], fill=ACCENT, width=2)
            ty = y
            for line in lines:
                draw.text((text_x, ty), line, font=f_note, fill=INK)
                ty += _line_h(f_note)
            y += box_h + 16
        y += 6
        _hr(draw, y)
        y += 40

    # ---- stats ----
    stats = brief.get("stats") or []
    if stats:
        _tracked(draw, (MARGIN, y), "FUND SNAPSHOT", f_label, GOLD, tracking=2)
        y += 30
        draw.text((MARGIN, y), "The numbers behind the story", font=f_heading, fill=INK)
        y += _line_h(f_heading) + 16

        cols = 3
        gap = 20
        cell_w = (CONTENT_W - gap * (cols - 1)) // cols
        cell_h = 96
        for idx, stat in enumerate(stats):
            col = idx % cols
            row = idx // cols
            cx0 = MARGIN + col * (cell_w + gap)
            cy0 = y + row * (cell_h + gap)
            draw.rectangle([cx0, cy0, cx0 + cell_w, cy0 + cell_h], fill=CARD, outline=LINE, width=1)
            draw.text((cx0 + 16, cy0 + 14), str(stat.get("value", "")), font=f_stat_val, fill=INK)
            lab_lines = _wrap(draw, str(stat.get("label", "")), f_stat_lab, cell_w - 32)[:2]
            ly = cy0 + 58
            for ll in lab_lines:
                draw.text((cx0 + 16, ly), ll, font=f_stat_lab, fill=INK_SOFT)
                ly += _line_h(f_stat_lab)
        rows = -(-len(stats) // cols)
        y += rows * (cell_h + gap)
        _hr(draw, y)
        y += 40

    # ---- footer ----
    draw.text((MARGIN, y), signoff_name, font=f_footer_name, fill=INK)
    role_y = y + _line_h(f_footer_name) - 6
    draw.text((MARGIN, role_y), "COMPILED FROM THE FUND HOUSE TALK FOR MY CLIENTS", font=f_footer_role, fill=INK_SOFT)
    y = role_y + 30
    _hr(draw, y)
    y += 20

    as_of = f" ({brief['date_label']})" if brief.get("date_label") else ""
    disclaimer = (
        "For general information only — a summary of a fund house presentation plus the agent's own "
        f"observations, not a recommendation or personalised financial advice. Figures shown are as "
        f"presented{as_of} and will have changed since. Past performance is not indicative of future "
        "results. Speak with your adviser before making any decisions about your own portfolio."
    )
    for line in _wrap(draw, disclaimer, f_disclaimer, CONTENT_W):
        draw.text((MARGIN, y), line, font=f_disclaimer, fill=INK_SOFT)
        y += _line_h(f_disclaimer) - 4
    y += 32

    canvas = canvas.crop((0, 0, WIDTH, y))
    buf_path = "/tmp/_poster_render.png"
    canvas.save(buf_path, format="PNG")
    with open(buf_path, "rb") as f:
        return f.read()
