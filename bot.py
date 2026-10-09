from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import re
import traceback
from datetime import datetime, timedelta, date, time as dt_time, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import telegram.error
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, BotCommand, InputMediaPhoto
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from assistant import anthropic_client, run_conversation
from services import calendar_service, sheets_service, pdf_utils, policy_workbook, policy_illustration, onedrive_service, action_plan, client_pairing, news_service, poster_service, ig_post_service, fund_price_service, fund_update_workbook
from prompts import RECEIPT_EXTRACTION_PROMPT, POLICY_FIELDS_EXTRACTION_PROMPT

MAX_HISTORY_MESSAGES = 40
MAX_POLICY_TEXT_CHARS = 15000

# Business-hours window used for "free time" calculations — not exposed as a
# setting anywhere yet, just a reasonable default.
WORK_START_HOUR = 9
WORK_END_HOUR = 21

# Persistent reply-keyboard button labels (bottom of the chat, always visible).
MENU_CALENDAR = "📅 Calendar"
MENU_RECEIPT = "🧾 Log Receipt"
MENU_POLICY = "📄 Policy Summary"
MENU_FILE = "🗂 File Client Items"
MENU_HELP = "❓ Help"
MENU_NEWS = "📰 News"
MENU_POSTER = "📊 Market Poster"
MENU_IG = "📸 IG Post"
MENU_FUND_UPDATE = "📈 Fund Update"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("assistant-bot")

# In-memory per-chat conversation history. Resets on process restart.
conversations: dict[int, list[dict]] = {}

# In-memory per-chat "waiting for client name" state for policy PDFs where we
# couldn't figure out who the policy belongs to. Maps chat_id -> extracted fields.
pending_policy: dict[int, dict] = {}

# In-memory per-chat "File Client Items" session state. Maps chat_id -> {
# "client_name": str | None, "count": int }. client_name is None while we're
# still waiting on the name; once set, any document/photo that arrives is
# saved straight to that client's OneDrive folder (no extraction, just a
# plain file drop) until the session is closed via the Done button.
pending_client_files: dict[int, dict] = {}

# In-memory per-chat "Policy Summary" batch session state. Maps chat_id ->
# {client_name: {"count", "action_items", "xlsx_path"}}. While a session is
# active (started via the Policy Summary button), each PDF still gets saved
# to the workbook immediately - only the Telegram reply changes, from a full
# reply+document every single time to one short "logged" line, so sending a
# stack of policies back-to-back doesn't spam the chat. The full summary and
# updated file(s) go out once, when the Done button is tapped.
pending_policy_session: dict[int, dict[str, dict]] = {}

# In-memory per-chat "Market Poster" note-collection session state. Maps
# chat_id -> {"parts": [...]} where parts is an ordered list of the raw
# text/photo pieces Nic sends (see poster_service.extract_poster_content).
# pending_poster_preview holds the rendered PNG + brief for the just-built
# poster, keyed by chat_id, until Nic approves or discards it.
pending_poster_session: dict[int, dict] = {}
pending_poster_preview: dict[int, dict] = {}

# In-memory per-chat "IG Post" session state - same shape as the poster
# session above (collects text notes + a photo, in any order). Building
# requires at least one photo among the parts. pending_ig_preview holds
# the edited photo bytes, caption, and the raw inputs (so 🔁 Regenerate can
# ask Claude for a fresh caption without needing the photo resent).
pending_ig_session: dict[int, dict] = {}
pending_ig_preview: dict[int, dict] = {}

# In-memory per-chat "Fund Update" session state (ILP Funds Update report).
# Maps chat_id -> {
#   "step": str,                # which question we're waiting on, see handle_message
#   "data": {...},              # client_name, product, policy_number, commencement_date,
#                               # total_invested, account_value, account_value_asof,
#                               # ref_illustration
#   "funds": [{"name","code","currency","allocation_pct"}, ...],
#   "pending_fund" / "pending_fund_name": the fund currently mid-entry, if any
#   "remarks": [str, ...] | absent
# }
# Fund prices are fetched live from HSBC only once the whole session is
# complete (see _build_fund_update) — nothing is fetched question-by-question.
pending_fund_update_session: dict[int, dict] = {}


def _trim_history(history: list[dict]) -> None:
    """Drops old turns from the front, but only ever starting the kept
    history at a plain user text message — never mid tool-call exchange,
    which the Anthropic API would reject."""
    while len(history) > MAX_HISTORY_MESSAGES:
        history.pop(0)
        while history and not (history[0]["role"] == "user" and isinstance(history[0]["content"], str)):
            history.pop(0)


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    if user is None or user.id != settings.allowed_user_id:
        if user is not None:
            logger.warning("Ignored message from unauthorized user_id=%s username=%s", user.id, user.username)
        return False
    return True


def _parse_date(text: str) -> date | None:
    """Accepts DD/MM/YYYY, DD-MM-YYYY, or the word 'today' — used throughout
    the Fund Update wizard, which asks for dates in Nic's usual DD/MM/YYYY."""
    text = text.strip()
    if text.lower() == "today":
        return date.today()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(text: str) -> float | None:
    cleaned = text.strip().replace(",", "").replace("$", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """The persistent row of buttons at the bottom of the chat."""
    return ReplyKeyboardMarkup(
        [[MENU_CALENDAR, MENU_RECEIPT], [MENU_POLICY, MENU_FILE], [MENU_NEWS, MENU_POSTER],
         [MENU_IG, MENU_FUND_UPDATE], [MENU_HELP]],
        resize_keyboard=True,
    )


def calendar_inline_keyboard() -> InlineKeyboardMarkup:
    """Sub-menu shown after tapping the Calendar button."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ New Appointment", callback_data="cal:new")],
            [InlineKeyboardButton("Today", callback_data="cal:today"),
             InlineKeyboardButton("Tomorrow", callback_data="cal:tomorrow")],
            [InlineKeyboardButton("This Week", callback_data="cal:week")],
            [InlineKeyboardButton("Free Today", callback_data="cal:free_today"),
             InlineKeyboardButton("Free Tomorrow", callback_data="cal:free_tomorrow")],
        ]
    )


def _done_filing_keyboard() -> InlineKeyboardMarkup:
    """Shown while a 'File Client Items' session is active."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done Filing", callback_data="filedone")]])


def _done_policy_keyboard() -> InlineKeyboardMarkup:
    """Shown while a batch 'Policy Summary' session is active."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="policydone")]])


def _done_poster_keyboard() -> InlineKeyboardMarkup:
    """Shown while a 'Market Poster' note-collection session is active."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Build Poster", callback_data="posterdone")]])


def _poster_review_keyboard() -> InlineKeyboardMarkup:
    """Shown under the generated poster preview, before it goes to clients."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Post to client group", callback_data="posterpost"),
         InlineKeyboardButton("🗑 Discard", callback_data="posterdiscard")],
    ])


def _done_ig_keyboard() -> InlineKeyboardMarkup:
    """Shown while an 'IG Post' note-collection session is active."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Create Post", callback_data="igdone")]])


def _ig_review_keyboard() -> InlineKeyboardMarkup:
    """Shown under the edited photo + caption - there's no direct Instagram
    publish integration, so this is a review/redo step, not a send step."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Regenerate Caption", callback_data="igregen"),
         InlineKeyboardButton("🗑 Discard", callback_data="igdiscard")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    conversations[update.effective_chat.id] = []
    pending_policy.pop(update.effective_chat.id, None)
    pending_client_files.pop(update.effective_chat.id, None)
    pending_policy_session.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        "Hi! I'm your personal assistant. I can:\n"
        "- Chat, draft client messages, and answer questions\n"
        "- Summarize a policy — just send me the PDF\n"
        "- Schedule/check/cancel appointments on your calendar — just ask\n"
        "- Log receipts — send a photo of one, or tell me the details in chat\n\n"
        "Try /help for more, or /today for today's schedule. Or just tap a button below "
        "instead of typing.",
        reply_markup=main_menu_keyboard(),
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await update.message.reply_text(
        "Here's what I can do — tap a button below.", reply_markup=main_menu_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    lines = [
        "What I can do:",
        "- Send a policy PDF and I'll extract the details, log them to that client's policy "
        "summary spreadsheet, and send you back the updated file.",
        "- Send a photo of a receipt and I'll log it" + (
            " to your spreadsheet." if settings.sheets_configured else " — not set up yet."
        ),
        "- Ask me to schedule/check/cancel appointments" + (
            " and I'll update your Google Calendar." if settings.calendar_configured else " — not set up yet."
        ),
        "- /today — today's schedule",
        "- /undo — remove the most recently logged receipt (in case of a misread)",
        "- /client <name> — pull up a client policy summary in chat (numbers, coverage, gap notes) and get the PDF",
        "- /client_code <name> — generate a one-time pairing code so a client can link the client bot to their own policy summary",
        "- /news — on-demand insurance news digest (Singapore-focused)",
        "- 📊 Market Poster (button below) — turn your fund house talk notes into a "
        "client-ready poster, with a preview to approve before it goes to your client group",
        "- 📸 IG Post (button below) — send photo(s) and I'll crop/enhance them for Instagram "
        "(picking the best 5-10 as a carousel if you send a lot) and write one caption — no "
        "auto-posting, just save the photo(s) and copy the caption yourself (no Instagram "
        "connection from here).",
        "- 📈 Fund Update (button below) - walks you through a client's ILP policy and "
        "fund allocations, fetches live fund prices from HSBC, and builds + saves the "
        "ILP Funds Update report to OneDrive.",
        "- /onedrive_setup — connect OneDrive so client files and archived PDFs are backed up" + (
            " (already connected)" if settings.onedrive_token_cache else ""
        ),
        "- 🗂 File Client Items (button below) — just save any file(s) for a client to OneDrive, "
        "no extraction, no spreadsheet — for anything that isn't a policy PDF or a receipt.",
        "- /menu — show the tap-to-use buttons again",
        "- /start — reset our conversation",
        "",
        "Tip: caption a policy PDF with the client's name if I might not catch it correctly "
        "from the document, or caption it \"receipt\" if it's actually a receipt.",
    ]
    await update.message.reply_text("\n".join(lines))


def _format_event_lines(events: list[dict]) -> list[str]:
    lines = []
    for e in events:
        start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "?"))
        time_str = start
        try:
            time_str = datetime.fromisoformat(start).strftime("%H:%M")
        except ValueError:
            pass
        title = e.get("summary", "(no title)")
        lines.append(f"- {time_str} — {title}")
    return lines


def _format_events_grouped_by_day(events: list[dict], start_date: str, end_date: str) -> list[str]:
    """Buckets events under a bold heading for each calendar day in
    [start_date, end_date] - so a multi-day range (e.g. "This Week") reads
    as a week laid out day by day, instead of one flat list with no sense
    of which events fall on which day. Days with nothing scheduled still
    get a heading, so the empty stretches of the week are visible too."""
    by_day: dict[str, list[dict]] = {}
    for e in events:
        start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "?"))
        try:
            day_key = datetime.fromisoformat(start).date().isoformat()
        except ValueError:
            day_key = start[:10]
        by_day.setdefault(day_key, []).append(e)

    start_d = date.fromisoformat(start_date)
    end_d = date.fromisoformat(end_date)

    lines: list[str] = []
    d = start_d
    while d <= end_d:
        day_events = by_day.get(d.isoformat(), [])
        lines.append(f"\n*{d.strftime('%A, %-d %b')}*")
        if day_events:
            day_events_sorted = sorted(
                day_events,
                key=lambda e: e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")),
            )
            lines.extend(_format_event_lines(day_events_sorted))
        else:
            lines.append("- Nothing scheduled")
        d += timedelta(days=1)
    return lines


async def _reply_events_for_range(message, start_date: str, end_date: str,
                                   header: str, empty_text: str,
                                   group_by_day: bool = False) -> None:
    if not settings.calendar_configured:
        await message.reply_text("Calendar isn't set up yet — see the README to connect it.")
        return
    try:
        events = await calendar_service.list_events(start_date, end_date)
    except Exception as exc:  # noqa: BLE001
        logger.exception("calendar range fetch failed")
        await message.reply_text(f"Couldn't load your calendar: {exc}")
        return

    if group_by_day:
        # Show every day in the range (even empty ones) so the whole week's
        # shape is visible, rather than bailing out to empty_text just
        # because a couple of days have nothing on them.
        lines = [header] + _format_events_grouped_by_day(events, start_date, end_date)
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    if not events:
        await message.reply_text(empty_text)
        return

    lines = [header] + _format_event_lines(events)
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def _week_range(today: date) -> tuple[str, str]:
    start = today - timedelta(days=today.weekday())  # Monday
    end = start + timedelta(days=6)  # Sunday
    return start.isoformat(), end.isoformat()


# Minimum gap Nic wants left free between any two appointments, to account
# for travel time between them. Applied as a full MEETING_BUFFER_MINUTES
# pad on BOTH sides of every existing appointment (not split/halved) - a
# freshly-offered slot has no padding of its own yet, so only padding the
# existing appointment fully on both sides guarantees the real gap to it
# is never less than MEETING_BUFFER_MINUTES.
MEETING_BUFFER_MINUTES = 60


def _free_slots(events: list[dict], day: date) -> list[str]:
    """Gaps of at least 30 minutes between events, clamped to the configured
    business-hours window for that day. All-day events are ignored here since
    they don't have a specific time range to carve out. Each event is padded
    by half of MEETING_BUFFER_MINUTES on both sides before gaps are computed,
    so a slot right up against an existing appointment is never offered -
    there's always at least MEETING_BUFFER_MINUTES of travel time between
    any two bookings."""
    tz = ZoneInfo(settings.timezone)
    window_start = datetime.combine(day, dt_time(WORK_START_HOUR, 0), tzinfo=tz)
    window_end = datetime.combine(day, dt_time(WORK_END_HOUR, 0), tzinfo=tz)
    buffer = timedelta(minutes=MEETING_BUFFER_MINUTES)

    busy = []
    for e in events:
        s = e.get("start", {}).get("dateTime")
        en = e.get("end", {}).get("dateTime")
        if not s or not en:
            continue
        try:
            s_dt = datetime.fromisoformat(s) - buffer
            e_dt = datetime.fromisoformat(en) + buffer
        except ValueError:
            continue
        s_dt = max(s_dt, window_start)
        e_dt = min(e_dt, window_end)
        if e_dt > s_dt:
            busy.append((s_dt, e_dt))
    busy.sort()

    slots = []
    cursor = window_start
    for s_dt, e_dt in busy:
        if s_dt > cursor:
            slots.append((cursor, s_dt))
        cursor = max(cursor, e_dt)
    if cursor < window_end:
        slots.append((cursor, window_end))

    return [
        f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
        for s, e in slots
        if (e - s) >= timedelta(minutes=30)
    ]


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    today = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
    await _reply_events_for_range(
        update.message, today, today, "*Today's schedule*", "Nothing on your calendar today."
    )


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Removes the most recently logged receipt row (whether it came in via
    photo, PDF, or chat text) - the fix for a misread that already got
    saved, without needing to open the spreadsheet by hand."""
    if not _is_allowed(update):
        return
    try:
        removed = await sheets_service.delete_last_receipt()
    except sheets_service.NoReceiptToUndo:
        await update.message.reply_text("There's nothing to undo — no receipts logged yet.")
        return
    except sheets_service.SheetsNotConfigured:
        await update.message.reply_text("Receipt logging isn't set up yet, so there's nothing to undo.")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to undo last receipt")
        await update.message.reply_text(f"Couldn't undo that: {exc}")
        return

    category_suffix = f" ({removed['category']})" if removed.get("category") else ""
    await update.message.reply_text(
        f"Removed: {removed['vendor']}, {removed['currency']} {removed['amount']}, "
        f"{removed['date']}{category_suffix}\n\nSend the correct details and I'll log it fresh."
    )


def _fmt_money(value, decimals: int = 2):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return f"${value:,.{decimals}f}"
    return str(value)


def _format_client_summary(summary: dict) -> str:
    lines = [summary["client_name"]]
    if summary.get("date_of_birth"):
        lines.append(f"DOB: {summary['date_of_birth']}")

    policies = summary.get("policies") or []
    if not policies:
        lines.append("")
        lines.append("No policies logged yet.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"{len(policies)} polic{'y' if len(policies) == 1 else 'ies'}:")
    for p in policies:
        company = p.get("company") or "?"
        plan = p.get("plan_type") or "Plan"
        lines.append(f"\n• {company} — {plan}")
        if p.get("policy_no"):
            lines.append(f"  {p['policy_no']}")
        if p.get("payment_date"):
            lines.append(f"  {p['payment_date']}")
        premium_cash = _fmt_money(p.get("premium_cash"))
        premium_cpf = _fmt_money(p.get("premium_cpf"))
        premium_bits = [f"{v} cash" if k == "cash" else f"{v} CPF"
                         for k, v in (("cash", premium_cash), ("cpf", premium_cpf)) if v]
        if premium_bits:
            lines.append(f"  Premium: {' + '.join(premium_bits)}/yr")
        death_cov = _fmt_money(p.get("death_coverage"), 0)
        ci_cov = _fmt_money(p.get("ci_coverage"), 0)
        coverage_bits = [f"Death {death_cov}" if death_cov else None, f"CI {ci_cov}" if ci_cov else None]
        coverage_bits = [b for b in coverage_bits if b]
        if coverage_bits:
            lines.append(f"  Coverage: {', '.join(coverage_bits)}")
        if p.get("remarks"):
            lines.append(f"  Note: {p['remarks']}")

    totals = summary.get("totals") or {}
    total_cash = _fmt_money(totals.get("premium_cash"))
    total_cpf = _fmt_money(totals.get("premium_cpf"))
    total_bits = [f"{v} cash" if k == "cash" else f"{v} CPF"
                   for k, v in (("cash", total_cash), ("cpf", total_cpf)) if v]
    if total_bits:
        lines.append(f"\nTotal annual premium: {' + '.join(total_bits)}")

    if summary.get("action_items"):
        lines.append("\n⚠️ Next action:")
        for item in summary["action_items"]:
            lines.append(f"- {item['action']}")

    return "\n".join(lines)


async def client_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /client_code <name> - generates a one-time pairing code for the
    # client-facing bot (client_bot.py), so a client can link their own
    # Telegram account to their client_name and privately pull up their own
    # Policy Summary without ever typing anything sensitive like an NRIC.
    if not _is_allowed(update):
        return
    if not settings.onedrive_configured:
        await update.message.reply_text(
            "OneDrive isn't connected yet, so I can't look up client files - run /onedrive_setup first."
        )
        return

    try:
        all_clients = await policy_workbook.list_client_names()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to list clients from OneDrive")
        await update.message.reply_text(f"Couldn't reach OneDrive to look up clients: {exc}")
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("Usage: /client_code <name>")
        return

    query_lower = query.lower()
    matches = [c for c in all_clients if query_lower in c.lower()]
    if not matches:
        suggestions = difflib.get_close_matches(query, all_clients, n=3)
        msg = f'No client found matching "{query}".'
        if suggestions:
            msg += "\n\nDid you mean:\n" + "\n".join(f"- {s}" for s in suggestions)
        await update.message.reply_text(msg)
        return
    if len(matches) > 1:
        await update.message.reply_text(
            "That matches more than one client - which one?\n\n" + "\n".join(f"- {m}" for m in matches)
        )
        return

    client_name = matches[0]
    try:
        code = await client_pairing.create_code(client_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to create pairing code for %s", client_name)
        await update.message.reply_text(f"Couldn't generate a pairing code: {exc}")
        return

    reply = f"Pairing code for {client_name}: {code}\nValid for 48 hours, one-time use.\n\n"
    if settings.client_bot_username:
        reply += f"Send your client this link:\nhttps://t.me/{settings.client_bot_username}?start={code}"
    else:
        reply += (
            "Send your client this code plus the client bot's username. "
            "(Set CLIENT_BOT_USERNAME to get a ready-to-send link here instead.)"
        )
    await update.message.reply_text(reply)


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /news - on-demand insurance news digest, using Claude's built-in web
    # search tool (no separate news API/subscription needed). Kept simple:
    # no caching, no schedule - just fetches fresh each time it's asked for.
    if not _is_allowed(update):
        return
    await update.message.reply_chat_action("typing")
    try:
        digest = await news_service.get_insurance_news_digest()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build insurance news digest")
        await update.message.reply_text(f"Couldn't pull a news digest right now: {exc}")
        return
    await update.message.reply_text(digest)


async def _build_poster(message, chat_id: int) -> None:
    """Shared by the 'done' text command and the ✅ Build Poster button —
    extracts the structured brief from whatever's been collected, renders
    it, and sends a preview with approve/discard buttons. `message` is any
    telegram.Message to reply from (the user's message, or the message a
    button was attached to)."""
    state = pending_poster_session.pop(chat_id, None)
    parts = state["parts"] if state else []
    if not parts:
        await message.reply_text(
            "Nothing to build a poster from — send some notes or slide photos first.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await message.chat.send_action("upload_photo")
    try:
        brief = await poster_service.extract_poster_content(parts)
        png_bytes = poster_service.render_poster_image(brief, signoff_name=settings.agent_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build market poster")
        await message.reply_text(f"Couldn't build the poster: {exc}", reply_markup=main_menu_keyboard())
        return

    pending_poster_preview[chat_id] = {"png": png_bytes, "brief": brief}
    await message.reply_photo(
        photo=BytesIO(png_bytes),
        filename="market_outlook.png",
        caption="Here's the preview — post this to your client group, or discard it.",
        reply_markup=_poster_review_keyboard(),
    )


async def poster_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    await _build_poster(query.message, update.effective_chat.id)


async def poster_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    chat_id = update.effective_chat.id
    pending = pending_poster_preview.pop(chat_id, None)
    if not pending:
        await query.message.reply_text("That preview has expired — build the poster again first.")
        return
    if not settings.client_group_chat_id:
        await query.message.reply_text(
            "The client group isn't connected yet — add me to that Telegram group, then send "
            "/groupid in the group and set CLIENT_GROUP_CHAT_ID to that number on Railway. "
            "The poster above is still there, so you can forward it yourself for now."
        )
        return
    try:
        await context.bot.send_photo(
            chat_id=settings.client_group_chat_id,
            photo=BytesIO(pending["png"]),
            filename="market_outlook.png",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to post poster to client group")
        await query.message.reply_text(f"Couldn't post to the client group: {exc}")
        return
    await query.message.reply_text("Posted to your client group ✅", reply_markup=main_menu_keyboard())


async def poster_discard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    pending_poster_preview.pop(update.effective_chat.id, None)
    await query.message.reply_text("Discarded — nothing was sent.", reply_markup=main_menu_keyboard())


async def _build_ig_post(message, chat_id: int) -> None:
    """Shared by the 'done' text command and the ✅ Create Post button - takes
    whatever's been collected (must include at least one photo). If no notes
    have been typed in yet, asks "What's this post about?" once and waits
    for a reply (or "skip") instead of building blind - see the
    ig_state["asked_topic"] handling in handle_message. 5 or fewer photos
    are used as-is (a single-photo post if just 1, otherwise a carousel of
    all of them); more than 5 and Claude narrows them down to the best
    5-10 for a carousel first (ig_post_service.select_carousel_photos)
    rather than just grabbing them all. Each selected photo gets edited, one
    caption is written covering the whole set, and both come back with a
    review keyboard. There's no auto-publish step - Instagram has no
    connector wired into this bot, so this is a copy/download-and-post-it-
    yourself hand-off, same spirit as the poster preview but without the
    'post to client group' button."""
    state = pending_ig_session.get(chat_id)
    parts = state["parts"] if state else []
    image_parts = [p for p in parts if p["type"] == "image"]
    if not image_parts:
        pending_ig_session.pop(chat_id, None)
        await message.reply_text(
            "Nothing to work with yet - send the photo(s) you want to post first.",
            reply_markup=main_menu_keyboard(),
        )
        return

    has_notes = any(p["type"] == "text" for p in parts)
    if not has_notes and not state.get("asked_topic"):
        state["asked_topic"] = True
        await message.reply_text(
            "What's this post about? Send a line or two and I'll write the caption around it "
            "- or type \"skip\" to have me work from the photo(s) alone."
        )
        return
    pending_ig_session.pop(chat_id, None)

    pick_note = ""
    if len(image_parts) <= 5:
        selected = list(range(len(image_parts)))
    else:
        await message.chat.send_action("typing")
        try:
            selected, reason = await ig_post_service.select_carousel_photos(image_parts)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to pick carousel photos - falling back to the first ones sent")
            selected, reason = list(range(min(10, len(image_parts)))), ""
        pick_note = (
            f"Picked {len(selected)} of {len(image_parts)} photos for the carousel"
            + (f" — {reason}" if reason else "") + ".\n"
        )

    raw_photo_bytes = [base64.b64decode(image_parts[i]["data"]) for i in selected]
    notes = "\n".join(p["text"] for p in parts if p["type"] == "text")

    await message.chat.send_action("upload_photo")
    try:
        edited_bytes_list = [ig_post_service.edit_photo(b) for b in raw_photo_bytes]
        caption = await ig_post_service.generate_caption(raw_photo_bytes, notes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build IG post")
        await message.reply_text(f"Couldn't put that post together: {exc}", reply_markup=main_menu_keyboard())
        return

    pending_ig_preview[chat_id] = {
        "edited": edited_bytes_list, "caption": caption, "photo_bytes": raw_photo_bytes, "notes": notes,
    }

    if len(edited_bytes_list) == 1:
        await message.reply_photo(
            photo=BytesIO(edited_bytes_list[0]),
            filename="ig_post.jpg",
            caption=f"{pick_note}Here's the edited photo, cropped and enhanced for Instagram.",
        )
    else:
        media = [
            InputMediaPhoto(
                media=BytesIO(b),
                filename=f"ig_post_{i + 1}.jpg",
                caption=(
                    f"{pick_note}Here's the carousel, cropped and enhanced for Instagram - swipe through."
                    if i == 0 else None
                ),
            )
            for i, b in enumerate(edited_bytes_list)
        ]
        await message.reply_media_group(media=media)

    await message.reply_text(
        f"{caption}\n\n—\nCaption above - copy it, save the photo(s), and post both yourself. "
        "Want something changed? Just tell me (e.g. \"make it shorter\"), or tap 🔁 Regenerate "
        "for a fresh take. No direct Instagram connection from here.",
        reply_markup=_ig_review_keyboard(),
    )


async def ig_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    await _build_ig_post(query.message, update.effective_chat.id)


async def ig_regen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    chat_id = update.effective_chat.id
    pending = pending_ig_preview.get(chat_id)
    if not pending:
        await query.message.reply_text("That session's expired - start a new IG Post first.")
        return
    await query.message.chat.send_action("typing")
    try:
        caption = await ig_post_service.generate_caption(pending["photo_bytes"], pending["notes"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to regenerate IG caption")
        await query.message.reply_text(f"Couldn't get a new caption: {exc}")
        return
    pending["caption"] = caption
    await query.message.reply_text(
        f"{caption}\n\n—\nCaption above - copy it, save the photo(s), and post both yourself. "
        "Want something changed? Just tell me, or tap 🔁 Regenerate again for another fresh take.",
        reply_markup=_ig_review_keyboard(),
    )


async def ig_discard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    pending_ig_preview.pop(update.effective_chat.id, None)
    await query.message.reply_text("Discarded.", reply_markup=main_menu_keyboard())


async def groupid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run this inside the client group/channel (after adding the bot to it)
    to get its chat ID for CLIENT_GROUP_CHAT_ID on Railway — that's the only
    thing connecting /poster's 'Post to client group' button to an actual
    destination.

    Channel posts are handled as a special case: the Bot API never attaches
    a user identity to a channel post (Telegram hides the author), so the
    normal _is_allowed() check would silently reject it. Since the bot can
    only be an admin of channels Nic himself added it to, any channel_post
    reaching this handler is inherently trusted."""
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return
    if chat.type != ChatType.CHANNEL and not _is_allowed(update):
        return
    await message.reply_text(
        f"This chat's ID is: {chat.id}\n\nSet CLIENT_GROUP_CHAT_ID to this on Railway."
    )


async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /client <name> - pulls up a client's policy summary numbers straight in
    # chat, so Nic does not need to open OneDrive/Excel on his phone mid-call
    # just to answer "what's my coverage again".
    if not _is_allowed(update):
        return
    if not settings.onedrive_configured:
        await update.message.reply_text(
            "OneDrive isn't connected yet, so I can't look up client files — run /onedrive_setup first."
        )
        return

    try:
        all_clients = await policy_workbook.list_client_names()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to list clients from OneDrive")
        await update.message.reply_text(f"Couldn't reach OneDrive to look up clients: {exc}")
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        if not all_clients:
            await update.message.reply_text("No client folders found on OneDrive yet.")
            return
        await update.message.reply_text(
            "Usage: /client <name>\n\nClients on file:\n" + "\n".join(f"- {c}" for c in all_clients)
        )
        return

    query_lower = query.lower()
    matches = [c for c in all_clients if query_lower in c.lower()]
    if not matches:
        suggestions = difflib.get_close_matches(query, all_clients, n=3)
        msg = f'No client found matching "{query}".'
        if suggestions:
            msg += "\n\nDid you mean:\n" + "\n".join(f"- {s}" for s in suggestions)
        await update.message.reply_text(msg)
        return
    if len(matches) > 1:
        await update.message.reply_text(
            "That matches more than one client — which one?\n\n" + "\n".join(f"- {m}" for m in matches)
        )
        return

    client_name = matches[0]
    try:
        summary = await asyncio.to_thread(policy_workbook.get_client_summary, client_name)
    except policy_workbook.PolicyWorkbookError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load client summary for %s", client_name)
        await update.message.reply_text(f"Couldn't load {client_name}'s policy summary: {exc}")
        return

    await update.message.reply_text(_format_client_summary(summary))

    await update.message.reply_chat_action("upload_document")
    try:
        pdf_bytes = await policy_workbook.get_client_facing_pdf(client_name)
    except policy_workbook.PolicyWorkbookError as exc:
        await update.message.reply_text(f"(Couldn't attach the PDF: {exc})")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build client-facing PDF for %s", client_name)
        await update.message.reply_text(f"(Couldn't attach the PDF: {exc})")
        return

    safe_name = "".join(c for c in client_name if c not in '<>:"/\\|?*').strip()
    await update.message.reply_document(
        document=BytesIO(pdf_bytes),
        filename=f"{safe_name} - Policy Summary.pdf",
        caption=f"{client_name}'s policy summary PDF.",
    )


async def onedrive_setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs Microsoft's OAuth device-code flow live (this has to happen on
    Railway — see services/onedrive_service.py for why), walks Nic through
    signing in from his phone/laptop browser, then hands back the resulting
    token cache to paste into Railway as ONEDRIVE_TOKEN_CACHE. After that,
    client workbooks and archived policy PDFs sync to OneDrive automatically
    on every policy summary."""
    if not _is_allowed(update):
        return
    if not settings.onedrive_client_id:
        await update.message.reply_text(
            "OneDrive isn't set up yet — ONEDRIVE_CLIENT_ID needs to be added to Railway first."
        )
        return

    try:
        flow = await asyncio.to_thread(onedrive_service.start_device_flow)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to start OneDrive device flow")
        await update.message.reply_text(f"Couldn't start OneDrive sign-in: {exc}")
        return

    await update.message.reply_text(
        "To connect OneDrive:\n\n"
        f"1. Open {flow['verification_uri']}\n"
        f"2. Enter this code: `{flow['user_code']}`\n"
        "3. Sign in with your Microsoft account and approve access.\n\n"
        "I'll message you again once you're done (you have about 15 minutes).",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        cache_str = await asyncio.to_thread(onedrive_service.complete_device_flow, flow)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OneDrive device flow did not complete")
        await update.message.reply_text(f"OneDrive sign-in didn't complete: {exc}")
        return

    await update.message.reply_text(
        "You're signed in! Last step — open the file below, copy everything in it, and paste "
        "it as the ONEDRIVE_TOKEN_CACHE variable in Railway (Variables tab → New Variable). "
        "Railway will redeploy automatically and OneDrive syncing will be live — no need to "
        "run this again unless I tell you OneDrive's connection expired."
    )
    cache_bytes = cache_str.encode("utf-8")
    await update.message.reply_document(
        document=BytesIO(cache_bytes), filename="onedrive_token_cache.txt"
    )


async def calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()

    action = query.data.split(":", 1)[1] if ":" in query.data else query.data
    today = datetime.now(ZoneInfo(settings.timezone)).date()

    if action == "new":
        if not settings.calendar_configured:
            await query.message.reply_text("Calendar isn't set up yet — see the README to connect it.")
            return
        await query.message.reply_text(
            "Tell me what to schedule, in plain language — e.g. \"Meeting with John Tan "
            "tomorrow 3-4pm\" or \"Client call next Tuesday at 10am\" — and I'll add it to "
            "your calendar."
        )
    elif action == "today":
        await _reply_events_for_range(
            query.message, today.isoformat(), today.isoformat(),
            "*Today's schedule*", "Nothing on your calendar today.",
        )
    elif action == "tomorrow":
        tmr = today + timedelta(days=1)
        await _reply_events_for_range(
            query.message, tmr.isoformat(), tmr.isoformat(),
            "*Tomorrow's schedule*", "Nothing on your calendar tomorrow.",
        )
    elif action == "week":
        start, end = _week_range(today)
        await _reply_events_for_range(
            query.message, start, end, "*This week*", "Nothing on your calendar this week.",
            group_by_day=True,
        )
    elif action in ("free_today", "free_tomorrow"):
        day = today if action == "free_today" else today + timedelta(days=1)
        label = "today" if action == "free_today" else "tomorrow"
        if not settings.calendar_configured:
            await query.message.reply_text("Calendar isn't set up yet — see the README to connect it.")
            return
        try:
            events = await calendar_service.list_events(day.isoformat(), day.isoformat())
        except Exception as exc:  # noqa: BLE001
            logger.exception("free slot fetch failed")
            await query.message.reply_text(f"Couldn't load your calendar: {exc}")
            return
        slots = _free_slots(events, day)
        window = f"{WORK_START_HOUR:02d}:00–{WORK_END_HOUR:02d}:00"
        if not slots:
            await query.message.reply_text(f"No free slots {label} between {window}.")
        else:
            await query.message.reply_text(
                f"*Free {label} ({window})*\n" + "\n".join(f"- {s}" for s in slots),
                parse_mode=ParseMode.MARKDOWN,
            )


async def _menu_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "What would you like to do?", reply_markup=calendar_inline_keyboard()
    )


async def _menu_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a photo of the receipt, or just type the details — e.g. \"$18 Grab ride today\"."
    )


async def _menu_policy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a batch Policy Summary session - each PDF sent from here is
    filed immediately, but the full reply + updated file only goes out once,
    when Done is tapped, so sending several policies in a row doesn't spam
    the chat with a resend after every single one."""
    chat_id = update.effective_chat.id
    pending_client_files.pop(chat_id, None)
    pending_policy_session[chat_id] = {}
    await update.message.reply_text(
        "Send me the policy PDFs, one at a time - I'll file each one under the right "
        "client as it comes in. Tap ✅ Done when you're finished and I'll send the full "
        "summary and updated file(s).",
        reply_markup=_done_policy_keyboard(),
    )


async def _menu_file_client_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a 'just save this to OneDrive' session — no extraction, any
    file type. Asks for the client name first, then collects files until the
    Done button is tapped."""
    chat_id = update.effective_chat.id
    pending_policy.pop(chat_id, None)
    pending_policy_session.pop(chat_id, None)
    pending_client_files[chat_id] = {"client_name": None, "count": 0}
    await update.message.reply_text("Who are these files for? Send me the client's name.")


async def _menu_poster(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a 'Market Poster' session — collects typed notes and/or photos
    of fund house slides until Done is tapped, then turns them into a
    client-ready poster image for review before it goes to the client group."""
    chat_id = update.effective_chat.id
    pending_policy.pop(chat_id, None)
    pending_policy_session.pop(chat_id, None)
    pending_client_files.pop(chat_id, None)
    pending_poster_preview.pop(chat_id, None)
    pending_poster_session[chat_id] = {"parts": []}
    await update.message.reply_text(
        "Send me your notes from the fund house talk — typed notes, photos of the slides, "
        "whatever you've got, in any order. Tap ✅ Build Poster below when you're done.",
        reply_markup=_done_poster_keyboard(),
    )


async def _menu_ig(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts an 'IG Post' session - collects photos and optional notes
    until Done is tapped, then crops/enhances the photo(s) and writes an
    Instagram caption for Nic to copy and post himself. 5 or fewer photos
    are used as-is; more than that and Claude narrows it down to a 5-10
    photo carousel."""
    chat_id = update.effective_chat.id
    pending_policy.pop(chat_id, None)
    pending_policy_session.pop(chat_id, None)
    pending_client_files.pop(chat_id, None)
    pending_poster_session.pop(chat_id, None)
    pending_ig_preview.pop(chat_id, None)
    pending_ig_session[chat_id] = {"parts": [], "asked_topic": False}
    await update.message.reply_text(
        "Send me the photo(s) you want to post. Send 5 or fewer and I'll use all of them; "
        "send more and I'll pick the best 5-10 for a carousel. Tell me what it's about now if "
        "you like, or I'll ask before writing the caption. Tap ✅ Create Post below when you're "
        "done with photos.",
        reply_markup=_done_ig_keyboard(),
    )


def _done_fund_keyboard() -> InlineKeyboardMarkup:
    """Shown once at least one fund has been added to an active Fund Update session."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done Adding Funds", callback_data="funddone")]])


def _existing_is_usable(existing: dict | None) -> bool:
    """True if fund_update_workbook.load_existing() returned enough fields
    to actually offer a reuse — guards against a corrupted/manually-edited
    report where some fields failed to parse back out."""
    if not existing:
        return False
    return bool(
        existing.get("product")
        and existing.get("policy_number")
        and existing.get("commencement_date")
        and existing.get("total_invested") is not None
        and existing.get("funds")
    )


async def _advance_reuse_queue(message, chat_id: int) -> None:
    """Drains pending_fund_update_session[chat_id]["reuse_fund_queue"] one
    fund at a time, asking for a security code wherever resolve_fund()
    couldn't find a directory match for a reused fund (e.g. its entry was
    lost to a Railway redeploy). Once drained, moves on to asking for the
    current account value."""
    state = pending_fund_update_session.get(chat_id)
    if not state:
        return
    queue = state.get("reuse_fund_queue") or []
    if queue:
        nxt = queue.pop(0)
        state["reuse_pending_fund"] = nxt
        state["step"] = "awaiting_fund_code_reuse"
        await message.reply_text(
            f"I don't have '{nxt['name']}' in my fund directory anymore. Reply with its security code "
            "and currency from fundprices.insurance.hsbc.com.sg, like `F0GBR04K8L USD`, or type skip to "
            "leave this fund out."
        )
        return
    state["step"] = "account_value"
    await message.reply_text(f"Reusing {len(state['funds'])} fund(s). Current account value?")


async def _menu_fund_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts an 'ILP Funds Update' session - walks through the policy
    details and fund allocations in chat, fetches live NAV from HSBC for
    commencement / 1st-anniversary / today, then asks for the ACTION note
    and the three Initial Objective / Market Update / Outlook sections
    (each skippable) so the report comes back fully written instead of
    needing to be finished by hand in Excel. Builds the report and saves it
    straight to OneDrive under Client/<name>/, same as Policy Summary does."""
    chat_id = update.effective_chat.id
    pending_policy.pop(chat_id, None)
    pending_policy_session.pop(chat_id, None)
    pending_client_files.pop(chat_id, None)
    pending_poster_session.pop(chat_id, None)
    pending_poster_preview.pop(chat_id, None)
    pending_ig_session.pop(chat_id, None)
    pending_ig_preview.pop(chat_id, None)
    pending_fund_update_session[chat_id] = {"step": "client_name", "data": {}, "funds": []}
    await update.message.reply_text("Who's this ILP Funds Update for? Send the client's name.")


async def _finish_fund_list(message, chat_id: int) -> None:
    state = pending_fund_update_session.get(chat_id)
    if not state or not state.get("funds"):
        await message.reply_text("Add at least one fund before finishing — send a fund name.")
        return
    state["step"] = "remarks"
    names = ", ".join(f["name"] for f in state["funds"])
    await message.reply_text(
        f"Got {len(state['funds'])} fund(s): {names}.\n\n"
        "Any remarks/notes to attach per fund, in the same order? Send them separated by commas "
        "or on separate lines, or type skip."
    )


async def fund_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    await _finish_fund_list(query.message, update.effective_chat.id)


async def _build_fund_update(message, chat_id: int) -> None:
    """Fetches live prices for every fund in the session, builds the
    workbook, saves it to OneDrive, and sends it back on Telegram. Pops the
    session either way so a failure doesn't leave the chat stuck."""
    state = pending_fund_update_session.pop(chat_id, None)
    if not state:
        return
    data = state["data"]
    funds_in = state["funds"]
    remarks = state.get("remarks") or []

    await message.chat.send_action("typing")
    await message.reply_text(f"Fetching live prices for {len(funds_in)} fund(s) from HSBC — one moment...")

    commencement = data["commencement_date"]
    try:
        anniversary = date(commencement.year + 1, commencement.month, commencement.day)
    except ValueError:
        anniversary = commencement + timedelta(days=365)
    current = date.today()

    fund_rows = []
    errors = []
    for i, f in enumerate(funds_in):
        try:
            prices = fund_price_service.fetch_prices(f["code"], f["currency"], [commencement, anniversary, current])
        except fund_price_service.FundPriceError as exc:
            errors.append(f"{f['name']}: {exc}")
            continue
        display_name = f["name"] if "(" in f["name"] else f"{f['name']} ({f['currency']})"
        fund_rows.append(fund_update_workbook.FundRow(
            name=display_name,
            allocation_pct=f["allocation_pct"],
            price_commencement=prices[commencement].value,
            price_anniversary=prices[anniversary].value,
            price_current=prices[current].value,
            remark=remarks[i] if i < len(remarks) else None,
        ))

    if errors:
        await message.reply_text("Some funds couldn't be priced:\n" + "\n".join(errors))
    if not fund_rows:
        await message.reply_text("Couldn't build the report — no fund prices came back.", reply_markup=main_menu_keyboard())
        return

    wb_data = fund_update_workbook.FundUpdateData(
        client_name=data["client_name"],
        product=data["product"],
        policy_number=data["policy_number"],
        commencement_date=commencement,
        anniversary_date=anniversary,
        current_date=current,
        total_invested=data["total_invested"],
        account_value=data["account_value"],
        account_value_asof=data["account_value_asof"],
        funds=fund_rows,
        ref_illustration=data.get("ref_illustration"),
        action_notes=data.get("action_notes"),
        initial_objective=data.get("initial_objective"),
        market_update=data.get("market_update"),
        outlook=data.get("outlook"),
    )
    xlsx_bytes = fund_update_workbook.build_fund_update_workbook(wb_data)
    filename = fund_update_workbook._filename(data["client_name"])

    saved_note = ""
    try:
        filename = await fund_update_workbook.save_to_onedrive(data["client_name"], wb_data, xlsx_bytes)
        saved_note = f"\n\nSaved to OneDrive under Client/{data['client_name']}/{filename}."
    except onedrive_service.OneDriveNotConfigured:
        saved_note = "\n\n(OneDrive isn't set up yet — run /onedrive_setup to have future reports saved there automatically.)"
    except Exception:
        logger.exception("Failed to save fund update workbook to OneDrive")
        saved_note = "\n\n(Couldn't save to OneDrive this time — here's the file directly.)"

    await message.reply_document(
        document=BytesIO(xlsx_bytes),
        filename=filename,
        caption=f"ILP Funds Update — {data['client_name']}{saved_note}",
        reply_markup=main_menu_keyboard(),
    )

    # Offer to go straight into the next policy for the same client instead
    # of making him retype the name and sit through the reuse prompt again
    # — the name is the only thing genuinely shared between two policies.
    pending_fund_update_session[chat_id] = {
        "step": "confirm_another_policy",
        "data": {"client_name": data["client_name"]},
        "funds": [],
    }
    await message.reply_text(f"Add another policy for {data['client_name']}? (yes/no)")


# Persistent-keyboard button text -> handler. Checked first in handle_message
# so tapping a button doesn't fall through to the general chat assistant.
MENU_ACTIONS = {
    MENU_CALENDAR: _menu_calendar,
    MENU_RECEIPT: _menu_receipt,
    MENU_POLICY: _menu_policy,
    MENU_FILE: _menu_file_client_items,
    MENU_NEWS: news_command,
    MENU_POSTER: _menu_poster,
    MENU_IG: _menu_ig,
    MENU_FUND_UPDATE: _menu_fund_update,
    MENU_HELP: help_command,
}


def _recent_clients(limit: int = 8) -> list[str]:
    """Client names with an existing Policy Summary workbook, most recently
    updated first — used to offer tappable suggestions instead of typing."""
    if not policy_workbook.CLIENT_DIR.exists():
        return []
    files = sorted(
        policy_workbook.CLIENT_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    prefix = policy_workbook.WORKBOOK_FILENAME_PREFIX
    names = []
    for f in files[:limit]:
        stem = f.stem
        names.append(stem[len(prefix):] if stem.startswith(prefix) else stem)
    return names


def _client_picker_keyboard(names: list[str]) -> InlineKeyboardMarkup | None:
    if not names:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(name, callback_data=f"polc:{name[:55]}")] for name in names])


async def policy_client_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()

    chat_id = update.effective_chat.id
    client_name = query.data.split(":", 1)[1] if ":" in query.data else ""
    pending = pending_policy.pop(chat_id, None)
    if pending is None or not client_name:
        await query.message.reply_text("That selection has expired — please resend the policy PDF.")
        return
    await _finish_policy_summary(
        query.message, client_name, pending["fields"],
        pdf_bytes=pending.get("pdf_bytes"), pdf_filename=pending.get("pdf_filename"),
    )


async def _close_filing_session(update: Update, chat_id: int, edit: bool = False) -> None:
    state = pending_client_files.pop(chat_id, None)
    if not state or not state.get("client_name"):
        msg = "Nothing to finish — no filing session is active."
    else:
        count = state.get("count", 0)
        name = state["client_name"]
        msg = (
            f"No files were sent for {name}, so nothing was filed."
            if count == 0
            else f"Done — filed {count} file(s) under {name} in OneDrive."
        )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(msg)
    else:
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard())


async def file_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    await _close_filing_session(update, update.effective_chat.id, edit=True)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    chat_id = update.effective_chat.id
    text_raw = update.message.text or ""

    if text_raw in MENU_ACTIONS:
        pending_policy.pop(chat_id, None)
        if text_raw != MENU_FILE:
            pending_client_files.pop(chat_id, None)
        if text_raw != MENU_POSTER:
            pending_poster_session.pop(chat_id, None)
            pending_poster_preview.pop(chat_id, None)
        if text_raw != MENU_IG:
            pending_ig_session.pop(chat_id, None)
            pending_ig_preview.pop(chat_id, None)
        if text_raw != MENU_FUND_UPDATE:
            pending_fund_update_session.pop(chat_id, None)
        await MENU_ACTIONS[text_raw](update, context)
        return

    if chat_id in pending_policy:
        pending = pending_policy.pop(chat_id)
        client_name = (update.message.text or "").strip()
        if not client_name:
            pending_policy[chat_id] = pending
            await update.message.reply_text("I still need a name to file this under — who is this policy for?")
            return
        await _finish_policy_summary(
            update.message, client_name, pending["fields"],
            pdf_bytes=pending.get("pdf_bytes"), pdf_filename=pending.get("pdf_filename"),
        )
        return

    if chat_id in pending_client_files:
        state = pending_client_files[chat_id]
        if state["client_name"] is None:
            name = text_raw.strip()
            if not name:
                await update.message.reply_text("I still need a name — who are these files for?")
                return
            state["client_name"] = name
            await update.message.reply_text(
                f"Got it — filing under {name}. Send me the files now (any type — documents, "
                "photos, whatever). Tap ✅ Done Filing below when you're finished.",
                reply_markup=_done_filing_keyboard(),
            )
            return
        if text_raw.strip().lower() in {"done", "finish"}:
            await _close_filing_session(update, chat_id)
            return
        await update.message.reply_text(
            f"Still filing under {state['client_name']} ({state.get('count', 0)} file(s) so far). "
            "Send more files, or tap ✅ Done Filing to finish.",
            reply_markup=_done_filing_keyboard(),
        )
        return

    if chat_id in pending_poster_session:
        if text_raw.strip().lower() in {"done", "finish"}:
            await _build_poster(update.message, chat_id)
            return
        pending_poster_session[chat_id]["parts"].append({"type": "text", "text": text_raw})
        count = len(pending_poster_session[chat_id]["parts"])
        await update.message.reply_text(
            f"Got it ({count} item(s) so far). Send more notes/photos, or tap ✅ Build Poster to finish.",
            reply_markup=_done_poster_keyboard(),
        )
        return

    if chat_id in pending_ig_session:
        text_clean = text_raw.strip()
        ig_state = pending_ig_session[chat_id]
        ig_has_notes = any(p["type"] == "text" for p in ig_state["parts"])
        if text_clean.lower() in {"done", "finish"}:
            await _build_ig_post(update.message, chat_id)
            return
        if ig_state.get("asked_topic") and not ig_has_notes:
            # This is the answer to "What's this post about?" - use it (unless skipped)
            # and build right away instead of waiting for another Done tap.
            if text_clean.lower() != "skip":
                ig_state["parts"].append({"type": "text", "text": text_raw})
            await _build_ig_post(update.message, chat_id)
            return
        ig_state["parts"].append({"type": "text", "text": text_raw})
        count = len(ig_state["parts"])
        await update.message.reply_text(
            f"Got it ({count} item(s) so far). Send the photo (if not sent yet), or tap ✅ Create Post to finish.",
            reply_markup=_done_ig_keyboard(),
        )
        return

    if chat_id in pending_ig_preview:
        # A caption is already sitting in review - any plain text here is feedback on it
        # ("make it shorter", "less salesy", "mention the venue") rather than a fresh topic,
        # so revise the existing caption instead of rerolling one from scratch.
        pending = pending_ig_preview[chat_id]
        await update.message.chat.send_action("typing")
        try:
            revised = await ig_post_service.revise_caption(
                pending["caption"], text_raw, pending["photo_bytes"], pending["notes"]
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to revise IG caption")
            await update.message.reply_text(f"Couldn't apply that change: {exc}")
            return
        pending["caption"] = revised
        await update.message.reply_text(
            f"{revised}\n\n—\nCaption above - copy it, save the photo(s), and post both yourself. "
            "Want another change? Just tell me, or tap 🔁 Regenerate for a fresh take.",
            reply_markup=_ig_review_keyboard(),
        )
        return

    if chat_id in pending_fund_update_session:
        state = pending_fund_update_session[chat_id]
        step = state["step"]
        text = text_raw.strip()

        if step == "confirm_another_policy":
            answer = text.lower()
            if answer in {"yes", "y", "yeah", "yep", "sure", "ok", "okay"}:
                state["step"] = "product"
                await update.message.reply_text("What's the policy product? (e.g. HSBCLife Wealth Voyage)")
                return
            if answer in {"no", "n", "nope"}:
                pending_fund_update_session.pop(chat_id, None)
                await update.message.reply_text("Okay, all done!", reply_markup=main_menu_keyboard())
                return
            await update.message.reply_text("Reply yes or no — add another policy for this client?")
            return

        if step == "client_name":
            if not text:
                await update.message.reply_text("Who's this fund update for? Send the client's name.")
                return
            state["data"]["client_name"] = text

            existing = None
            try:
                existing = await fund_update_workbook.load_existing(text)
            except Exception:
                logger.exception("Failed to look up existing fund update report for %s", text)
            if _existing_is_usable(existing):
                state["existing"] = existing
                state["step"] = "confirm_reuse"
                funds = existing["funds"]
                fund_lines = "\n".join(f"  - {f['name']} ({f['allocation_pct']:.0f}%)" for f in funds)
                await update.message.reply_text(
                    f"Found an existing ILP Funds Update report for {text}:\n"
                    f"Product: {existing['product']}\n"
                    f"Policy #: {existing['policy_number']}\n"
                    f"Commencement: {existing['commencement_date']:%d/%m/%Y}\n"
                    f"Total invested: ${existing['total_invested']:,.0f}\n"
                    f"Funds:\n{fund_lines}\n\n"
                    "Reuse these and just update the current account value? (yes/no)"
                )
                return

            state["step"] = "product"
            await update.message.reply_text("What's the policy product? (e.g. HSBCLife Wealth Voyage)")
            return

        if step == "confirm_reuse":
            answer = text.lower()
            if answer in {"yes", "y", "yeah", "yep", "sure", "ok", "okay"}:
                existing = state.pop("existing")
                state["data"]["product"] = existing["product"]
                state["data"]["policy_number"] = existing["policy_number"]
                state["data"]["commencement_date"] = existing["commencement_date"]
                state["data"]["total_invested"] = existing["total_invested"]
                state["data"]["ref_illustration"] = existing.get("ref_illustration")
                state["data"]["_reusing"] = True

                resolved_funds = []
                reuse_queue = []
                for f in existing["funds"]:
                    resolved = fund_price_service.resolve_fund(f["name"])
                    if resolved:
                        resolved_funds.append({
                            "name": resolved.display_name,
                            "code": resolved.code,
                            "currency": resolved.currency,
                            "allocation_pct": f["allocation_pct"],
                        })
                    else:
                        reuse_queue.append(f)
                state["funds"] = resolved_funds
                state["reuse_fund_queue"] = reuse_queue

                if reuse_queue:
                    await _advance_reuse_queue(update.message, chat_id)
                else:
                    state["step"] = "account_value"
                    await update.message.reply_text(
                        f"Reusing {len(resolved_funds)} fund(s). Current account value?"
                    )
                return

            if answer in {"no", "n", "nope"}:
                state.pop("existing", None)
                state["step"] = "product"
                await update.message.reply_text("Okay, starting fresh. What's the policy product? (e.g. HSBCLife Wealth Voyage)")
                return

            await update.message.reply_text("Reply yes or no — reuse the existing details?")
            return

        if step == "awaiting_fund_code_reuse":
            if text.lower() == "skip":
                state.pop("reuse_pending_fund", None)
                await _advance_reuse_queue(update.message, chat_id)
                return
            parts = text.split()
            if len(parts) != 2:
                await update.message.reply_text(
                    "That didn't look right — reply with just the code and currency, like `F0GBR04K8L USD`."
                )
                return
            code, currency = parts
            pending = state.pop("reuse_pending_fund", None)
            if pending is None:
                await _advance_reuse_queue(update.message, chat_id)
                return
            fund_price_service.remember_fund(pending["name"], code, currency)
            state["funds"].append({
                "name": pending["name"],
                "code": code,
                "currency": currency.upper(),
                "allocation_pct": pending["allocation_pct"],
            })
            await update.message.reply_text(f"Got it — saved {pending['name']} ({currency.upper()}) at {pending['allocation_pct']:.0f}%.")
            await _advance_reuse_queue(update.message, chat_id)
            return

        if step == "product":
            state["data"]["product"] = text or "—"
            state["step"] = "policy_number"
            await update.message.reply_text("Policy number?")
            return

        if step == "policy_number":
            state["data"]["policy_number"] = text or "—"
            state["step"] = "commencement_date"
            await update.message.reply_text("Commencement date? (DD/MM/YYYY)")
            return

        if step == "commencement_date":
            d = _parse_date(text)
            if d is None:
                await update.message.reply_text("Couldn't read that date — please send it as DD/MM/YYYY.")
                return
            state["data"]["commencement_date"] = d
            state["step"] = "total_invested"
            await update.message.reply_text("Total amount invested? (just the number, e.g. 9600)")
            return

        if step == "total_invested":
            v = _parse_amount(text)
            if v is None:
                await update.message.reply_text("Couldn't read that as a number — how much was invested in total?")
                return
            state["data"]["total_invested"] = v
            state["step"] = "account_value"
            await update.message.reply_text("Current account value?")
            return

        if step == "account_value":
            v = _parse_amount(text)
            if v is None:
                await update.message.reply_text("Couldn't read that as a number — what's the current account value?")
                return
            state["data"]["account_value"] = v
            state["step"] = "account_value_date"
            await update.message.reply_text("As of what date? (DD/MM/YYYY, or send 'today')")
            return

        if step == "account_value_date":
            d = _parse_date(text)
            if d is None:
                await update.message.reply_text("Couldn't read that date — please send it as DD/MM/YYYY, or 'today'.")
                return
            state["data"]["account_value_asof"] = d
            if state["data"].get("_reusing"):
                state["step"] = "fund_name"
                names = ", ".join(f["name"] for f in state["funds"]) or "none yet"
                await update.message.reply_text(
                    f"Current funds: {names}.\n\n"
                    "Send another fund name to add one, tap Done if the allocation is unchanged, "
                    "or type done."
                , reply_markup=_done_fund_keyboard())
                return
            state["step"] = "ref_illustration"
            await update.message.reply_text(
                "Ref policy illustration value, if you have one? (e.g. \"$13,911 (8% IRR)\"), or type skip."
            )
            return

        if step == "ref_illustration":
            state["data"]["ref_illustration"] = None if text.lower() == "skip" else text
            state["step"] = "fund_name"
            await update.message.reply_text(
                "Now the funds. Send the first fund's name (e.g. BlackRock World Healthscience (USD))."
            )
            return

        if step == "fund_name":
            if text.lower() in {"done", "finish"}:
                await _finish_fund_list(update.message, chat_id)
                return
            if text.lower() in {"undo", "undo last", "remove last"}:
                if state["funds"]:
                    removed = state["funds"].pop()
                    await update.message.reply_text(
                        f"Removed {removed['name']} ({removed['allocation_pct']:.0f}%). "
                        "Send the next fund's name, or type done if that's all."
                    )
                else:
                    await update.message.reply_text("No funds added yet — nothing to undo.")
                return
            if not text:
                await update.message.reply_text("Send a fund name, or type done if you're finished.")
                return
            resolved = fund_price_service.resolve_fund(text)
            if resolved:
                state["pending_fund"] = {
                    "name": resolved.display_name, "code": resolved.code, "currency": resolved.currency,
                }
                state["step"] = "confirm_fund_match"
                await update.message.reply_text(
                    f"Found it — {resolved.display_name} ({resolved.currency}). Is that the right fund? (yes/no)"
                )
            else:
                state["pending_fund_name"] = text
                state["step"] = "awaiting_fund_code"
                await update.message.reply_text(
                    f"I don't have '{text}' in my fund directory yet. Reply with its security code and "
                    "currency from the fund's page on fundprices.insurance.hsbc.com.sg, like "
                    "`F0GBR04K8L USD`, or type skip to leave this fund out."
                )
            return

        if step == "confirm_fund_match":
            answer = text.lower()
            if answer in {"yes", "y", "yeah", "yep", "correct", "right"}:
                state["step"] = "fund_allocation"
                await update.message.reply_text("Great — what's its allocation %? (e.g. 25)")
                return
            if answer in {"no", "n", "nope", "wrong"}:
                state.pop("pending_fund", None)
                state["step"] = "fund_name"
                await update.message.reply_text(
                    "Okay, scratch that match. Send the fund name again — try matching the exact name "
                    "from fundprices.insurance.hsbc.com.sg (e.g. include the fund house or currency) so I "
                    "pick the right one, or type done if that's all."
                )
                return
            await update.message.reply_text("Reply yes or no — is that the right fund?")
            return

        if step == "awaiting_fund_code":
            if text.lower() == "skip":
                state.pop("pending_fund_name", None)
                state["step"] = "fund_name"
                await update.message.reply_text("Okay, skipped. Send the next fund's name, or type done if that's all.")
                return
            parts = text.split()
            if len(parts) != 2:
                await update.message.reply_text(
                    "That didn't look right — reply with just the code and currency, like `F0GBR04K8L USD`."
                )
                return
            code, currency = parts
            fund_name = state.pop("pending_fund_name", text)
            fund_price_service.remember_fund(fund_name, code, currency)
            state["pending_fund"] = {"name": fund_name, "code": code, "currency": currency.upper()}
            state["step"] = "fund_allocation"
            await update.message.reply_text(
                f"Got it — saved {fund_name} ({currency.upper()}) for future reports. What's its allocation %?"
            )
            return

        if step == "fund_allocation":
            alloc = _parse_amount(text)
            if alloc is None:
                await update.message.reply_text("Couldn't read that as a number — what's this fund's allocation %?")
                return
            pending_fund = state.pop("pending_fund", None)
            if pending_fund is None:
                state["step"] = "fund_name"
                await update.message.reply_text("Something went wrong — send the fund name again.")
                return
            pending_fund["allocation_pct"] = alloc
            state["funds"].append(pending_fund)
            state["step"] = "fund_name"
            total_alloc = sum(f["allocation_pct"] for f in state["funds"])
            await update.message.reply_text(
                f"Added {pending_fund['name']} at {alloc:.0f}% ({total_alloc:.0f}% allocated so far). "
                "Send the next fund's name, or tap Done if that's all.",
                reply_markup=_done_fund_keyboard(),
            )
            return

        if step == "remarks":
            if text.lower() != "skip":
                remark_list = [r.strip() for r in re.split(r"[,\n]", text) if r.strip()]
                state["remarks"] = remark_list
            state["step"] = "action_notes"
            await update.message.reply_text(
                "Any notes for the ACTION line (after today's date)? Or type skip."
            )
            return

        if step == "action_notes":
            if text.lower() != "skip":
                state["data"]["action_notes"] = text
            state["step"] = "initial_objective"
            await update.message.reply_text(
                "Initial objective of this investment? Or type skip."
            )
            return

        if step == "initial_objective":
            if text.lower() != "skip":
                state["data"]["initial_objective"] = text
            state["step"] = "market_update"
            await update.message.reply_text(
                "Market update for the last 12 months? Or type skip."
            )
            return

        if step == "market_update":
            if text.lower() != "skip":
                state["data"]["market_update"] = text
            state["step"] = "outlook"
            await update.message.reply_text(
                "Outlook for the next 12 months? Or type skip."
            )
            return

        if step == "outlook":
            if text.lower() != "skip":
                state["data"]["outlook"] = text
            await _build_fund_update(update.message, chat_id)
            return

        return

    user_text = update.message.text
    history = conversations.setdefault(chat_id, [])

    history.append({"role": "user", "content": user_text})
    _trim_history(history)

    await update.message.chat.send_action("typing")

    try:
        reply_text = await run_conversation(history)
    except Exception:
        logger.exception("Assistant conversation failed")
        await update.message.reply_text("Something went wrong on my end — try again in a moment.")
        return

    await update.message.reply_text(reply_text or "(no reply)")


async def handle_policy_or_receipt_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    chat_id = update.effective_chat.id
    document = update.message.document

    await update.message.chat.send_action("typing")
    tg_file = await context.bot.get_file(document.file_id)
    pdf_bytes = bytes(await tg_file.download_as_bytearray())

    if chat_id in pending_client_files and pending_client_files[chat_id].get("client_name"):
        await _file_client_item(update, chat_id, pdf_bytes, document.file_name)
        return

    caption_raw = (update.message.caption or "").strip()
    caption = caption_raw.lower()

    text = pdf_utils.extract_text(pdf_bytes)
    if not text:
        await update.message.reply_text(
            "I couldn't read any text out of that PDF — it looks like a scanned image rather "
            "than a text PDF. Try sending it as a photo instead, or a text-based export."
        )
        return

    if "receipt" in caption:
        await _extract_and_log_receipt_from_text(update, text)
        return

    client_override = None
    if caption_raw and caption != "policy":
        client_override = caption_raw

    await _extract_and_fill_policy_summary(
        update, text, client_name_override=client_override,
        pdf_bytes=pdf_bytes, pdf_filename=document.file_name,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]  # largest size

    try:
        await update.message.chat.send_action("typing")
    except telegram.error.RetryAfter:
        pass  # Telegram flood control - harmless to skip the typing indicator
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    if chat_id in pending_client_files and pending_client_files[chat_id].get("client_name"):
        await _file_client_item(update, chat_id, image_bytes, f"{photo.file_unique_id}.jpg")
        return

    if chat_id in pending_poster_session:
        image_b64_poster = base64.b64encode(image_bytes).decode("ascii")
        pending_poster_session[chat_id]["parts"].append(
            {"type": "image", "media_type": "image/jpeg", "data": image_b64_poster}
        )
        count = len(pending_poster_session[chat_id]["parts"])
        await update.message.reply_text(
            f"Got it ({count} item(s) so far). Send more notes/photos, or tap ✅ Build Poster to finish.",
            reply_markup=_done_poster_keyboard(),
        )
        return

    if chat_id in pending_ig_session:
        image_b64_ig = base64.b64encode(image_bytes).decode("ascii")
        pending_ig_session[chat_id]["parts"].append(
            {"type": "image", "media_type": "image/jpeg", "data": image_b64_ig}
        )
        # No per-photo reply - sending many photos in a row used to fire one
        # "Got it..." message each, which could trip Telegram's flood control
        # on a fast multi-photo send. Just collect silently; the original
        # prompt's ✅ Create Post button (or typing "done") finishes the session.
        return

    caption_raw = (update.message.caption or "").strip()
    caption = caption_raw.lower()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    if "policy" in caption:
        is_policy = True
    elif "receipt" in caption:
        is_policy = False
    else:
        # No caption to go on. This used to fall straight through to
        # "receipt" - which is exactly how a photographed policy summary
        # sent with no caption got silently logged as a receipt with
        # garbage fields. Ask Claude to actually look at the photo instead
        # of guessing blind.
        is_policy = await _classify_photo_as_policy(image_b64)

    if is_policy:
        client_override = caption_raw if (caption_raw and caption != "policy") else None
        await _extract_and_fill_policy_summary_from_image(
            update, image_b64, client_name_override=client_override,
        )
    else:
        await _extract_and_log_receipt_from_image(update, image_b64)


async def handle_generic_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any document that isn't a PDF (Word docs, images-as-file, zips, etc).
    Outside a 'File Client Items' session there's nothing useful to do with
    these, so we just point Nic at that button."""
    if not _is_allowed(update):
        return

    chat_id = update.effective_chat.id
    document = update.message.document

    if chat_id in pending_client_files and pending_client_files[chat_id].get("client_name"):
        await update.message.chat.send_action("typing")
        tg_file = await context.bot.get_file(document.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        await _file_client_item(update, chat_id, file_bytes, document.file_name)
        return

    await update.message.reply_text(
        "I can only read PDFs and photos for policy/receipt logging. To just save a file for a "
        "client, tap 🗂 File Client Items first."
    )


async def _classify_photo_as_policy(image_b64: str) -> bool:
    """Called only when a photo arrives with no "policy"/"receipt" caption
    to go on. Asks Claude to actually look at the image and classify it,
    rather than defaulting blind to receipt (see handle_photo)."""
    try:
        response = await anthropic_client.messages.create(
            model=settings.extraction_model,
            max_tokens=8,
            system=(
                "You classify a photographed document. Reply with exactly one word, "
                "nothing else: 'policy' if it's an insurance policy document, illustration, "
                "or policy summary sheet; 'receipt' if it's a purchase receipt, invoice, or "
                "payment slip. If genuinely unsure, reply 'receipt'."
            ),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                        {"type": "text", "text": "policy or receipt?"},
                    ],
                }
            ],
        )
        answer = "".join(b.text for b in response.content if b.type == "text").strip().lower()
        return "policy" in answer
    except Exception:  # noqa: BLE001
        logger.exception("Photo classification call failed - defaulting to receipt")
        return False


async def _extract_and_fill_policy_summary_from_image(
    update: Update, image_b64: str, client_name_override: str | None = None,
) -> None:
    """Photo equivalent of _extract_and_fill_policy_summary: pulls the same
    structured fields straight out of the image (instead of PDF text) and
    files them the same way — added to the client's workbook, illustration/
    action-plan sheets rebuilt, and a confirmation + workbook PDF sent back.
    Previously a photo just got a one-off text summary in chat with nothing
    saved anywhere, which is not what "log this policy" should do just
    because it arrived as a photo instead of a PDF."""
    try:
        response = await anthropic_client.messages.create(
            model=settings.extraction_model,
            max_tokens=768,
            system=POLICY_FIELDS_EXTRACTION_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                        {"type": "text", "text": "Photo of a policy document — extract the fields as JSON."},
                    ],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001
        # Belt-and-suspenders: the global error_handler would also catch this,
        # but a client/Nic staring at silence while the alert makes its way
        # through is exactly the "bot goes quiet with no error" failure mode
        # this whole error-handling effort was about. Reply here directly too.
        logger.exception("Policy extraction call failed for a photo")
        await update.message.reply_text(
            f"Something went wrong reading that photo: {exc}. Try again, or send it as a PDF instead."
        )
        return
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    fields = _parse_json_block(raw)
    if fields is None:
        await update.message.reply_text(
            "I couldn't extract structured fields from that photo — try a clearer shot, or "
            "send it as a PDF instead."
        )
        return

    client_name = (client_name_override or fields.get("client_name") or "").strip()
    if client_name:
        client_name = await _canonicalize_client_name(client_name, update.effective_chat.id)
    if not client_name:
        pending_policy[update.effective_chat.id] = {"fields": fields, "pdf_bytes": None, "pdf_filename": None}
        recent = _recent_clients()
        keyboard = _client_picker_keyboard(recent)
        prompt = "I couldn't find the client's name in this photo — who is this policy for? "
        prompt += (
            "Tap an existing client below, or just reply with a name."
            if keyboard else
            "Just reply with their name and I'll file it under them."
        )
        await update.message.reply_text(prompt, reply_markup=keyboard)
        return

    await _finish_policy_summary(update.message, client_name, fields)


def _parse_json_block(raw_text: str) -> dict | None:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # The model sometimes wraps the JSON in an explanatory sentence (e.g. "This is a
    # photo of X, not a receipt" alongside the null-fields JSON) - pull out just the
    # {...} block rather than giving up on the whole response.
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("Could not parse JSON from model output: %s", raw_text[:300])
    return None


def _fmt_amount(value) -> str:
    if isinstance(value, (int, float)):
        return f"{settings.default_currency} {value:,.0f}"
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "—"


def _format_policy_reply(client_name: str, fields: dict, policy_count: int) -> str:
    lines = [
        f"Policy logged for {client_name}",
        f"({policy_count} polic{'y' if policy_count == 1 else 'ies'} now on file for this client)",
        "",
        f"Company: {fields.get('company') or '—'}",
        f"Policy No: {fields.get('policy_no') or '—'}",
        f"Plan Type: {fields.get('plan_type') or '—'}",
        f"Payment Date: {fields.get('payment_date') or '—'}",
        f"Premium (Cash): {_fmt_amount(fields.get('premium_annual_cash'))}",
        f"Premium (CPF): {_fmt_amount(fields.get('premium_annual_cpf'))}",
        f"Payment Frequency: {fields.get('payment_frequency') or '—'}",
        f"Mode of Payment: {fields.get('mode_of_payment') or '—'}",
        "",
        f"Death Coverage: {_fmt_amount(fields.get('total_death_coverage'))}",
        f"Permanent Disability: {_fmt_amount(fields.get('total_permanent_disability_coverage'))}",
        f"Critical Illness: {_fmt_amount(fields.get('critical_illness_coverage'))}",
        f"Early Stage Illness: {_fmt_amount(fields.get('early_stage_illness_coverage'))}",
        f"Disability Income (Per Mth): {_fmt_amount(fields.get('disability_income_per_month'))}",
        f"Accident (Lump Sum): {_fmt_amount(fields.get('total_accident_lump_sum'))}",
        f"Accident (Medical Reimbursement): {_fmt_amount(fields.get('total_accident_medical_reimbursement'))}",
    ]
    if fields.get("remarks"):
        lines += ["", f"Remarks: {fields['remarks']}"]
    return "\n".join(lines)


def _normalize_name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


async def _canonicalize_client_name(candidate: str, chat_id: int) -> str:
    """Matches a freshly-extracted client name against names already known
    for this client, case/whitespace-insensitively, and returns the KNOWN
    spelling instead of the new variant when one matches.

    Without this, two photos of the same multi-page policy that OCR'd the
    client's name as e.g. "Tan Wei Ming" and "tan wei ming" (or with an
    extra space) would silently become two different clients — two
    different OneDrive folders/workbooks, and (within one batch session)
    two separate compiled PDFs sent instead of one. Checks the clients
    already touched in this batch session first (cheap, in-memory), then
    falls back to the full OneDrive client list (best-effort — a failure
    here just means no cross-check, not a broken filing)."""
    candidate = candidate.strip()
    if not candidate:
        return candidate
    key = _normalize_name_key(candidate)

    session = pending_policy_session.get(chat_id)
    if session:
        for known in session.keys():
            if _normalize_name_key(known) == key:
                return known

    try:
        all_clients = await policy_workbook.list_client_names()
    except Exception:  # noqa: BLE001
        logger.exception("Couldn't fetch client list for name canonicalization — proceeding without it")
        return candidate
    for known in all_clients:
        if _normalize_name_key(known) == key:
            return known
    return candidate


async def _extract_and_fill_policy_summary(
    update: Update, text: str, client_name_override: str | None = None,
    pdf_bytes: bytes | None = None, pdf_filename: str | None = None,
) -> None:
    truncated = text[:MAX_POLICY_TEXT_CHARS]
    response = await anthropic_client.messages.create(
        model=settings.extraction_model,
        max_tokens=768,
        system=POLICY_FIELDS_EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": f"Policy document text:\n\n{truncated}"}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    fields = _parse_json_block(raw)
    if fields is None:
        await update.message.reply_text(
            "I couldn't extract structured fields from that PDF — try sending it again, or "
            "let me know if the format looks unusual."
        )
        return

    client_name = (client_name_override or fields.get("client_name") or "").strip()
    if client_name:
        client_name = await _canonicalize_client_name(client_name, update.effective_chat.id)
    if not client_name:
        pending_policy[update.effective_chat.id] = {
            "fields": fields, "pdf_bytes": pdf_bytes, "pdf_filename": pdf_filename,
        }
        recent = _recent_clients()
        keyboard = _client_picker_keyboard(recent)
        prompt = "I couldn't find the client's name in this document — who is this policy for? "
        prompt += (
            "Tap an existing client below, or just reply with a name."
            if keyboard else
            "Just reply with their name and I'll file it under them."
        )
        await update.message.reply_text(prompt, reply_markup=keyboard)
        return

    await _finish_policy_summary(update.message, client_name, fields, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename)


def _safe_component(text: str | None, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", (text or "").strip()).strip("_")
    return cleaned or fallback


async def _file_client_item(update: Update, chat_id: int, file_bytes: bytes, suggested_filename: str | None) -> None:
    """Uploads one file straight to OneDrive under the active 'File Client
    Items' session's client folder — no extraction, just a plain archive
    drop. Only called when pending_client_files[chat_id] already has a
    client_name set."""
    state = pending_client_files[chat_id]
    client_name = state["client_name"]

    if not settings.onedrive_configured:
        pending_client_files.pop(chat_id, None)
        await update.message.reply_text(
            "OneDrive isn't connected yet, so I can't file this — run /onedrive_setup first, "
            "then start over with 🗂 File Client Items."
        )
        return

    safe_client = policy_workbook._onedrive_safe_name(client_name, "Unknown_Client")
    filename = policy_workbook._onedrive_safe_name(suggested_filename) if suggested_filename else None
    if not filename:
        timestamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M%S")
        filename = f"file_{timestamp}"
    remote_path = f"{policy_workbook.ONEDRIVE_WORKBOOK_FOLDER}/{safe_client}/{filename}"

    try:
        await onedrive_service.upload_bytes(remote_path, file_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to file client item to OneDrive")
        await update.message.reply_text(f"Couldn't save that file to OneDrive: {exc}")
        return

    state["count"] = state.get("count", 0) + 1
    logger.info("Filed client item to OneDrive: %s", remote_path)
    await update.message.reply_text(
        f"Saved ({state['count']} so far for {client_name}). Send more, or tap ✅ Done Filing.",
        reply_markup=_done_filing_keyboard(),
    )


async def _save_original_pdf(client_name: str, filename: str | None, pdf_bytes: bytes) -> None:
    """Archives the original policy PDF so the agent can pull up the source
    document later without hunting through Telegram history. When OneDrive
    is configured, this uploads there (the durable copy — survives a
    redeploy). Otherwise it falls back to POLICY_PDF_STORAGE_DIR, a local
    path — handy for local dev, but on Railway that disk is wiped on every
    redeploy, so this path is only really a fallback. No-op if neither is
    configured."""
    stem = _safe_component(Path(filename).stem if filename else None, "policy")
    timestamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M%S")
    client_dir_name = policy_workbook._onedrive_safe_name(client_name, "Unknown_Client")
    dest_name = f"{stem}_{timestamp}.pdf"

    if settings.onedrive_configured:
        remote_path = f"{policy_workbook.ONEDRIVE_WORKBOOK_FOLDER}/{client_dir_name}/{dest_name}"
        await onedrive_service.upload_bytes(remote_path, pdf_bytes)
        logger.info("Archived original policy PDF to OneDrive: %s", remote_path)
        return

    if not settings.policy_pdf_storage_dir:
        return
    base = Path(settings.policy_pdf_storage_dir).expanduser()
    client_dir = base / client_dir_name
    client_dir.mkdir(parents=True, exist_ok=True)
    dest = client_dir / dest_name
    dest.write_bytes(pdf_bytes)
    logger.info("Archived original policy PDF to %s", dest)


async def _send_workbook_as_pdf(target, client_name: str, xlsx_path: Path, caption: str) -> None:
    """Sends the client's workbook as a PDF - a true Graph/Excel export of
    the real file (services/policy_workbook.get_workbook_pdf), not a
    recreation - so what Nic gets on Telegram always looks exactly like the
    actual spreadsheet. Falls back to the .xlsx itself if OneDrive isn't
    configured or the conversion fails, so filing a policy never breaks
    just because the PDF export had an issue."""
    if settings.onedrive_configured:
        try:
            pdf_bytes = await policy_workbook.get_workbook_pdf(client_name, xlsx_path)
            await target.reply_document(
                document=BytesIO(pdf_bytes),
                filename=f"{xlsx_path.stem}.pdf",
                caption=caption,
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("Failed to convert workbook to PDF - falling back to the .xlsx file")
    with open(xlsx_path, "rb") as f:
        await target.reply_document(document=f, filename=xlsx_path.name, caption=caption)


async def _finish_policy_summary(
    message, client_name: str, fields: dict,
    pdf_bytes: bytes | None = None, pdf_filename: str | None = None,
) -> None:
    try:
        xlsx_path, policy_count, _gap_notes = await asyncio.to_thread(
            policy_workbook.add_policy_row, client_name, fields
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to update policy summary workbook")
        await message.reply_text(
            f"I extracted the details but couldn't save them to the spreadsheet: {exc}"
        )
        return

    try:
        await asyncio.to_thread(policy_illustration.rebuild_illustration_sheet, client_name)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to rebuild policy illustration sheet")
        # Non-fatal — the Policy Summary row is already saved; the illustration
        # tab just won't be refreshed this time.

    action_items = []
    try:
        action_items = await asyncio.to_thread(action_plan.rebuild_action_plan_sheet, client_name)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to rebuild action plan sheet")
        # Non-fatal, same reasoning as the illustration sheet above — Nic just
        # won't get a next-action sheet/message refreshed this time.

    if pdf_bytes is not None:
        try:
            await _save_original_pdf(client_name, pdf_filename, pdf_bytes)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to archive original policy PDF")
            # Non-fatal — same reasoning as the illustration sheet above.

    chat_id = message.chat_id
    session = pending_policy_session.get(chat_id)
    if session is not None:
        # Batch mode (started via the Policy Summary button) — save the
        # latest state for this client and stay silent, so a stack of PDFs
        # doesn't produce a reply after each one. Errors above (extraction/
        # save failures) still get their own message either way; this only
        # skips the success acknowledgment. Nic sees the Done button on the
        # message he got when he started the session.
        session[client_name] = {
            "count": policy_count, "action_items": action_items, "xlsx_path": xlsx_path,
        }
        return

    reply = _format_policy_reply(client_name, fields, policy_count)
    if action_items:
        reply += f"\n\nNext action for {client_name}:\n" + "\n".join(
            f"- {i['action']}" for i in action_items
        )
    # Plain text on purpose — fields/remarks come from freeform PDF text and
    # can contain a stray underscore/asterisk, which crashes legacy Markdown
    # parsing outright (this is the same bug class the /help crash was).
    await message.reply_text(reply)
    await _send_workbook_as_pdf(
        message, client_name, xlsx_path,
        caption=f"Updated policy summary + illustration for {client_name}.",
    )


async def _close_policy_session(update: Update, chat_id: int, edit: bool = False) -> None:
    session = pending_policy_session.pop(chat_id, None)
    total = sum(info["count"] for info in session.values()) if session else 0
    if not session or total == 0:
        msg = "Nothing to finish — no policies were filed this session."
    else:
        names = ", ".join(session.keys())
        msg = f"Done — {total} polic{'y' if total == 1 else 'ies'} filed for {names}."

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(msg)
        target = update.callback_query.message
    else:
        await update.message.reply_text(msg, reply_markup=main_menu_keyboard())
        target = update.message

    if not session or total == 0:
        return

    # One wrap-up per client touched this session: next action + the final
    # workbook, in place of the per-PDF resend batch mode skipped.
    for client_name, info in session.items():
        action_items = info.get("action_items") or []
        reply = f"{client_name} — {info['count']} polic{'y' if info['count'] == 1 else 'ies'} on file."
        if action_items:
            reply += "\n\nNext action:\n" + "\n".join(f"- {i['action']}" for i in action_items)
        await target.reply_text(reply)
        xlsx_path = info.get("xlsx_path")
        if xlsx_path and xlsx_path.exists():
            await _send_workbook_as_pdf(
                target, client_name, xlsx_path,
                caption=f"Updated policy summary + illustration for {client_name}.",
            )


async def policy_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    await _close_policy_session(update, update.effective_chat.id, edit=True)


async def _log_parsed_receipt(update: Update, data: dict, from_photo: bool = False) -> None:
    vendor = data.get("vendor") or "Unknown vendor"
    amount = data.get("amount")
    date = data.get("date") or datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
    currency = data.get("currency") or settings.default_currency
    category = data.get("category") or "Other"
    notes = data.get("notes")

    if amount is None:
        if from_photo and not data.get("vendor") and not data.get("date"):
            # Every field came back empty - this almost certainly isn't a receipt at all,
            # not just a hard-to-read one. The most common reason a random photo lands
            # here is it was meant for another photo feature (IG Post) but that session
            # was never started first, so a bare photo defaults to receipt logging.
            await update.message.reply_text(
                "That doesn't look like a receipt to me, so there's nothing to log. If you "
                "meant this for Instagram, tap 📸 IG Post (or send /igpost) first, "
                "then resend the photo."
            )
            return
        await update.message.reply_text(
            "I couldn't confidently read an amount off that receipt — could you tell me the "
            "amount (and vendor/date if I got those wrong) in a text message and I'll log it?"
        )
        return

    if not settings.sheets_configured:
        await update.message.reply_text(
            f"I read this receipt as: {vendor}, {currency} {amount}, {date} ({category}) — but "
            "receipt logging isn't set up yet, so I haven't saved it anywhere. See the README."
        )
        return

    try:
        await sheets_service.append_receipt(date=date, vendor=vendor, amount=float(amount),
                                             currency=currency, category=category, notes=notes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to log receipt")
        await update.message.reply_text(f"Couldn't save that receipt: {exc}")
        return

    notes_line = f"\nNotes: {notes}" if notes else ""
    await update.message.reply_text(
        f"Logged: {vendor} — {currency} {amount} on {date} ({category}).{notes_line}\n"
        "Edit the sheet directly if anything's off."
    )


async def _extract_and_log_receipt_from_image(update: Update, image_b64: str) -> None:
    response = await anthropic_client.messages.create(
        model=settings.extraction_model,
        max_tokens=512,
        system=RECEIPT_EXTRACTION_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": "Extract the receipt fields as JSON."},
                ],
            }
        ],
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    data = _parse_json_block(raw)
    if data is None:
        await update.message.reply_text(
            "I couldn't read that receipt clearly enough — could you tell me the vendor, "
            "amount, and date in a text message instead?"
        )
        return
    await _log_parsed_receipt(update, data, from_photo=True)


async def _extract_and_log_receipt_from_text(update: Update, text: str) -> None:
    truncated = text[:MAX_POLICY_TEXT_CHARS]
    response = await anthropic_client.messages.create(
        model=settings.extraction_model,
        max_tokens=512,
        system=RECEIPT_EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": f"Receipt document text:\n\n{truncated}"}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    data = _parse_json_block(raw)
    if data is None:
        await update.message.reply_text(
            "I couldn't parse that receipt clearly enough — could you tell me the vendor, "
            "amount, and date in a text message instead?"
        )
        return
    await _log_parsed_receipt(update, data)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches any exception a handler raises that would otherwise just get
    logged and silently swallowed (this is how the /help crash and the
    illustration-sheet OneDrive sync bug both went unnoticed until someone
    happened to check Railway's logs) — logs it properly AND pings Nic
    directly in Telegram so a broken feature doesn't sit silently broken.

    Exception: telegram.error.Conflict during getUpdates. This fires on every
    redeploy (the old container briefly overlaps with the new one, both
    polling at once) and PTB retries it internally within seconds on its
    own — it's not something Nic can act on, so it's logged but not sent."""
    if isinstance(context.error, telegram.error.Conflict):
        logger.info("Transient getUpdates conflict (likely an overlapping redeploy) — PTB will retry on its own")
        return
    if isinstance(context.error, telegram.error.RetryAfter):
        logger.warning("Telegram flood control hit (retry_after=%s) — not re-notifying, it's not actionable per-occurrence", context.error.retry_after)
        return
    if isinstance(context.error, telegram.error.BadRequest) and "Query is too old" in str(context.error):
        # A button tap whose callback query Telegram invalidated before the bot
        # answered it — almost always because the container was mid-redeploy
        # (same class of transient issue as the Conflict case above). Not
        # actionable beyond "tap it again", so log but don't alert.
        logger.info("Stale callback query (likely tapped during a redeploy) — not re-notifying")
        return
    logger.error("Unhandled exception while processing an update", exc_info=context.error)
    try:
        trace = "".join(
            traceback.format_exception(type(context.error), context.error, context.error.__traceback__)
        )
    except Exception:  # noqa: BLE001
        trace = str(context.error)
    last_line = next((ln for ln in reversed(trace.strip().splitlines())), str(context.error))
    try:
        await context.bot.send_message(
            chat_id=settings.allowed_user_id,
            text=f"\u26a0\ufe0f Something broke behind the scenes: {last_line}\n\n"
            "Whatever you just did probably didn't go through. Check Railway logs for the "
            "full details if it keeps happening.",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to notify about the above error")


# In-memory only — resets on redeploy, which just means a fresh baseline
# (no retroactive notifications), not duplicates. See _check_calendar_invites.
_invite_watch_state: dict = {"last_checked": None, "notified_ids": set()}

CALENDAR_INVITE_POLL_SECONDS = 600


async def _check_calendar_invites(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs on a timer (see main()). Polls the calendar for events created
    or changed since the last check and pings Nic about any where he isn't
    the organizer — i.e. someone invited him. First run after a (re)start
    just establishes the baseline instead of notifying about the calendar's
    entire existing history."""
    if not settings.calendar_configured:
        return

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    last_checked = _invite_watch_state["last_checked"]
    if last_checked is None:
        _invite_watch_state["last_checked"] = now_iso
        return

    try:
        events = await calendar_service.list_updated_events(last_checked)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to poll calendar for new invites")
        return
    _invite_watch_state["last_checked"] = now_iso

    notified_ids = _invite_watch_state["notified_ids"]
    for event in events:
        event_id = event.get("id")
        if not event_id or event_id in notified_ids:
            continue
        notified_ids.add(event_id)
        if event.get("status") == "cancelled":
            continue
        organizer = event.get("organizer", {})
        if organizer.get("self"):
            continue  # Nic created this himself, not an invite from someone else

        summary = event.get("summary") or "(no title)"
        organizer_name = organizer.get("displayName") or organizer.get("email") or "someone"
        start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date") or "?"
        try:
            start = datetime.fromisoformat(start).strftime("%a %d %b, %H:%M")
        except ValueError:
            pass  # all-day event date string, or unparsable — show as-is
        await context.bot.send_message(
            chat_id=settings.allowed_user_id,
            text=f"\U0001F4C5 New calendar invite from {organizer_name}: {summary}\n{start}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("\u2705 Accept", callback_data=f"accept_invite:{event_id}")]]
            ),
        )

    # Keep this from growing forever across a long-running process.
    if len(notified_ids) > 1000:
        _invite_watch_state["notified_ids"] = set(list(notified_ids)[-500:])


async def accept_invite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return
    await query.answer()
    event_id = query.data.split(":", 1)[1]
    try:
        await calendar_service.accept_event(event_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to accept calendar invite %s", event_id)
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=settings.allowed_user_id,
            text=f"Couldn't accept that invite: {exc}",
        )
        return
    original = query.message.text or ""
    await query.edit_message_text(f"{original}\n\n\u2705 Accepted")


async def _post_init(app: Application) -> None:
    # Populates the "/" slash-command menu in Telegram's UI (the small icon
    # next to the text box) - purely cosmetic/discoverability, the commands
    # themselves work via CommandHandler regardless of this.
    await app.bot.set_my_commands([
        BotCommand("today", "Today's schedule"),
        BotCommand("news", "On-demand insurance news digest"),
        BotCommand("poster", "Build a market outlook poster from your notes"),
        BotCommand("igpost", "Edit photo(s) and write a caption for Instagram"),
        BotCommand("groupid", "Get this chat's ID (run inside your client group)"),
        BotCommand("client", "Look up a client's policy summary"),
        BotCommand("client_code", "Generate a client pairing code"),
        BotCommand("undo", "Remove the most recently logged receipt"),
        BotCommand("onedrive_setup", "Connect OneDrive"),
        BotCommand("menu", "Show the tap-to-use buttons"),
        BotCommand("help", "What I can do"),
    ])


def main() -> None:
    app = Application.builder().token(settings.telegram_bot_token).post_init(_post_init).build()
    app.add_error_handler(error_handler)
    if app.job_queue is not None:
        app.job_queue.run_repeating(
            _check_calendar_invites, interval=CALENDAR_INVITE_POLL_SECONDS, first=60
        )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("undo", undo_command))
    app.add_handler(CommandHandler("onedrive_setup", onedrive_setup_command))
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CommandHandler("client_code", client_code_command))
    app.add_handler(CommandHandler("news", news_command))
    app.add_handler(CommandHandler("poster", _menu_poster))
    app.add_handler(CommandHandler("igpost", _menu_ig))
    app.add_handler(CommandHandler("fundupdate", _menu_fund_update))
    app.add_handler(CommandHandler("groupid", groupid_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(calendar_callback, pattern=r"^cal:"))
    app.add_handler(CallbackQueryHandler(accept_invite_callback, pattern=r"^accept_invite:"))
    app.add_handler(CallbackQueryHandler(policy_client_callback, pattern=r"^polc:"))
    app.add_handler(CallbackQueryHandler(file_done_callback, pattern=r"^filedone$"))
    app.add_handler(CallbackQueryHandler(policy_done_callback, pattern=r"^policydone$"))
    app.add_handler(CallbackQueryHandler(poster_done_callback, pattern=r"^posterdone$"))
    app.add_handler(CallbackQueryHandler(poster_post_callback, pattern=r"^posterpost$"))
    app.add_handler(CallbackQueryHandler(poster_discard_callback, pattern=r"^posterdiscard$"))
    app.add_handler(CallbackQueryHandler(ig_done_callback, pattern=r"^igdone$"))
    app.add_handler(CallbackQueryHandler(ig_regen_callback, pattern=r"^igregen$"))
    app.add_handler(CallbackQueryHandler(ig_discard_callback, pattern=r"^igdiscard$"))
    app.add_handler(CallbackQueryHandler(fund_done_callback, pattern=r"^funddone$"))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_policy_or_receipt_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.PDF, handle_generic_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
