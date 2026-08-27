from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timedelta, date, time as dt_time
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.constants import ParseMode
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
from services import calendar_service, sheets_service, pdf_utils, policy_workbook, policy_illustration, onedrive_service
from prompts import POLICY_SUMMARY_PROMPT, RECEIPT_EXTRACTION_PROMPT, POLICY_FIELDS_EXTRACTION_PROMPT

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


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """The persistent row of buttons at the bottom of the chat."""
    return ReplyKeyboardMarkup(
        [[MENU_CALENDAR, MENU_RECEIPT], [MENU_POLICY, MENU_FILE], [MENU_HELP]],
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    conversations[update.effective_chat.id] = []
    pending_policy.pop(update.effective_chat.id, None)
    pending_client_files.pop(update.effective_chat.id, None)
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
        "*What I can do*",
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
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


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


async def _reply_events_for_range(message, start_date: str, end_date: str,
                                   header: str, empty_text: str) -> None:
    if not settings.calendar_configured:
        await message.reply_text("Calendar isn't set up yet — see the README to connect it.")
        return
    try:
        events = await calendar_service.list_events(start_date, end_date)
    except Exception as exc:  # noqa: BLE001
        logger.exception("calendar range fetch failed")
        await message.reply_text(f"Couldn't load your calendar: {exc}")
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


def _free_slots(events: list[dict], day: date) -> list[str]:
    """Gaps of at least 30 minutes between events, clamped to the configured
    business-hours window for that day. All-day events are ignored here since
    they don't have a specific time range to carve out."""
    tz = ZoneInfo(settings.timezone)
    window_start = datetime.combine(day, dt_time(WORK_START_HOUR, 0), tzinfo=tz)
    window_end = datetime.combine(day, dt_time(WORK_END_HOUR, 0), tzinfo=tz)

    busy = []
    for e in events:
        s = e.get("start", {}).get("dateTime")
        en = e.get("end", {}).get("dateTime")
        if not s or not en:
            continue
        try:
            s_dt = datetime.fromisoformat(s)
            e_dt = datetime.fromisoformat(en)
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
    await update.message.reply_text(
        "Send me the policy PDF and I'll extract the details and file it under the right client."
    )


async def _menu_file_client_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a 'just save this to OneDrive' session — no extraction, any
    file type. Asks for the client name first, then collects files until the
    Done button is tapped."""
    chat_id = update.effective_chat.id
    pending_policy.pop(chat_id, None)
    pending_client_files[chat_id] = {"client_name": None, "count": 0}
    await update.message.reply_text("Who are these files for? Send me the client's name.")


# Persistent-keyboard button text -> handler. Checked first in handle_message
# so tapping a button doesn't fall through to the general chat assistant.
MENU_ACTIONS = {
    MENU_CALENDAR: _menu_calendar,
    MENU_RECEIPT: _menu_receipt,
    MENU_POLICY: _menu_policy,
    MENU_FILE: _menu_file_client_items,
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

    await update.message.chat.send_action("typing")
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    if chat_id in pending_client_files and pending_client_files[chat_id].get("client_name"):
        await _file_client_item(update, chat_id, image_bytes, f"{photo.file_unique_id}.jpg")
        return

    caption = (update.message.caption or "").lower()
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    if "policy" in caption:
        await _summarize_policy_from_image(update, image_b64)
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


async def _summarize_policy_from_image(update: Update, image_b64: str) -> None:
    response = await anthropic_client.messages.create(
        model=settings.extraction_model,
        max_tokens=1024,
        system=POLICY_SUMMARY_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                    {"type": "text", "text": "Photo of a policy document — please summarize it."},
                ],
            }
        ],
    )
    summary = "".join(b.text for b in response.content if b.type == "text").strip()
    await update.message.reply_text(summary or "I couldn't produce a summary from this image.",
                                     parse_mode=ParseMode.MARKDOWN)


def _parse_json_block(raw_text: str) -> dict | None:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
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
        f"*Policy logged for {client_name}*",
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


async def _finish_policy_summary(
    message, client_name: str, fields: dict,
    pdf_bytes: bytes | None = None, pdf_filename: str | None = None,
) -> None:
    try:
        xlsx_path, policy_count, gap_notes = await asyncio.to_thread(
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

    if pdf_bytes is not None:
        try:
            await _save_original_pdf(client_name, pdf_filename, pdf_bytes)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to archive original policy PDF")
            # Non-fatal — same reasoning as the illustration sheet above.

    reply = _format_policy_reply(client_name, fields, policy_count)
    if gap_notes:
        reply += "\n\n*Worth a look:*\n" + "\n".join(f"- {n}" for n in gap_notes)
    await message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    with open(xlsx_path, "rb") as f:
        await message.reply_document(
            document=f,
            filename=xlsx_path.name,
            caption=f"Updated policy summary + illustration for {client_name}.",
        )


async def _log_parsed_receipt(update: Update, data: dict) -> None:
    vendor = data.get("vendor") or "Unknown vendor"
    amount = data.get("amount")
    date = data.get("date") or datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
    currency = data.get("currency") or settings.default_currency
    category = data.get("category") or "Other"
    notes = data.get("notes")

    if amount is None:
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
    await _log_parsed_receipt(update, data)


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


def main() -> None:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("undo", undo_command))
    app.add_handler(CommandHandler("onedrive_setup", onedrive_setup_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(calendar_callback, pattern=r"^cal:"))
    app.add_handler(CallbackQueryHandler(policy_client_callback, pattern=r"^polc:"))
    app.add_handler(CallbackQueryHandler(file_done_callback, pattern=r"^filedone$"))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_policy_or_receipt_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.PDF, handle_generic_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
