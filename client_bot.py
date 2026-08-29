"""client_bot.py - the client-facing Telegram bot: a SEPARATE process/token
from bot.py (Nic's own personal assistant). Two contexts it operates in:

  - A private DM with a client: only works once they've redeemed a one-time
    pairing code from Nic (/client_code in bot.py) via services/client_pairing.py.
    That's the only place a client's own Policy Summary is shown, and it's
    always sent as a PDF (never text) - the trimmed, Policy-Summary-only
    export from services/policy_workbook.get_client_facing_pdf.

  - A shared Telegram group Nic adds his clients to: anyone there can post a
    document/question (relayed straight to Nic) or run /book_meeting to grab
    an open slot on Nic's calendar.

Deliberately a separate bot from bot.py, not a second command set bolted
onto it: bot.py's single ALLOWED_USER_ID gate and shared in-memory state
(keyed by chat_id) were built for one user in one chat. A group has many
users sharing one chat_id, which would corrupt state across different
clients' in-progress flows if it ran on the same process/state.

Setup checklist (see /client_code in bot.py + README):
  1. CLIENT_BOT_TOKEN, CLIENT_BOT_USERNAME env vars set to the new bot's
     credentials (from @BotFather).
  2. In BotFather: /setprivacy -> Disable for this bot, so it actually sees
     ordinary group messages (not just commands) to relay them.
  3. Nic personally sends /start to this bot once in DM, so it's allowed to
     message him proactively (Telegram won't let a bot cold-DM someone who
     has never started a chat with it).
  4. Nic creates the client group and adds this bot to it.
  5. In the group, run /whereami and set CLIENT_GROUP_CHAT_ID to the number
     it replies with (optional - locks relay/booking to just that group;
     without it, any group this bot is added to works).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, time as dt_time
from io import BytesIO
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from services import calendar_service, policy_workbook, client_pairing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("client-bot")

# Same business-hours convention as bot.py's _free_slots - duplicated
# rather than imported, deliberately: this process should not depend on
# bot.py at all (see module docstring on why they're kept separate).
WORK_START_HOUR = 9
WORK_END_HOUR = 21
MEETING_SLOT_MINUTES = 60
BOOKING_LOOKAHEAD_DAYS = 7

MENU_POLICY = "📄 My Policy Summary"
MENU_BOOK = "📅 Book a Meeting"
MENU_HELP = "❓ Help"

# Per-client (keyed by Telegram user id, NOT chat_id - a shared group has
# many users in one chat_id, so keying by chat_id would let one client's
# in-progress booking bleed into another's) in-memory meeting-booking state.
# Lost on restart, which just means an interrupted booking has to be
# restarted - nothing destructive.
pending_meeting: dict[int, dict] = {}


def _client_label(user) -> str:
    """Best-effort human-readable label for a Telegram user, used when
    relaying group messages/booking requests to Nic."""
    if user.username:
        return f"@{user.username}"
    return user.full_name or f"user {user.id}"


def _is_private(update: Update) -> bool:
    return update.effective_chat.type == "private"


def _group_allowed(update: Update) -> bool:
    """True if this group message should be handled at all. Without
    CLIENT_GROUP_CHAT_ID configured, any group this bot has been added to
    works (Nic controls that by only adding it to his one client group);
    once set, it locks relay/booking down to just that group."""
    if update.effective_chat.type not in ("group", "supergroup"):
        return False
    if settings.client_group_chat_id is not None:
        return update.effective_chat.id == settings.client_group_chat_id
    return True


def _paired_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[MENU_POLICY], [MENU_BOOK, MENU_HELP]], resize_keyboard=True)


async def whereami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"This chat's id is: {update.effective_chat.id}\nType: {update.effective_chat.type}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private(update):
        return  # pairing only happens in DM
    user_id = update.effective_user.id

    code = context.args[0] if context.args else None
    if code:
        try:
            client_name = await client_pairing.redeem_code(code, user_id)
        except client_pairing.PairingError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(
            f"You're linked to {client_name}. Use the menu below any time.",
            reply_markup=_paired_menu_keyboard(),
        )
        return

    existing = await client_pairing.get_paired_client(user_id)
    if existing:
        await update.message.reply_text(
            f"Welcome back — you're linked to {existing}.", reply_markup=_paired_menu_keyboard(),
        )
        return

    await update.message.reply_text(
        "Hi! To see your own policy summary here, I need a one-time pairing code from your "
        "agent first — ask them for one, then send it to me here (just the code, or the link "
        "they gave you)."
    )


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM-only. Handles: a bare pairing code typed in, the persistent-menu
    button taps, or anything else falls through to a short help nudge."""
    if not _is_private(update):
        return
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if text == MENU_POLICY:
        await send_policy_summary(update, context)
        return
    if text == MENU_BOOK:
        await start_booking(update, context)
        return
    if text == MENU_HELP:
        await help_command(update, context)
        return

    existing = await client_pairing.get_paired_client(user_id)
    looks_like_code = len(text) == client_pairing.CODE_LENGTH and text.isalnum()
    if not existing and looks_like_code:
        try:
            client_name = await client_pairing.redeem_code(text, user_id)
        except client_pairing.PairingError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(
            f"You're linked to {client_name}. Use the menu below any time.",
            reply_markup=_paired_menu_keyboard(),
        )
        return

    if existing:
        await update.message.reply_text(
            "Use the menu below, or /book_meeting to grab a slot on your agent's calendar.",
            reply_markup=_paired_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "I need a one-time pairing code from your agent before I can show your policy summary — "
            "ask them for one and send it to me here."
        )


async def send_policy_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private(update):
        await update.message.reply_text("For privacy, policy summaries are only sent in a private chat with me.")
        return
    user_id = update.effective_user.id
    client_name = await client_pairing.get_paired_client(user_id)
    if not client_name:
        await update.message.reply_text(
            "I don't have you linked to a client yet — ask your agent for a one-time pairing code."
        )
        return

    await update.message.reply_chat_action("upload_document")
    try:
        pdf_bytes = await policy_workbook.get_client_facing_pdf(client_name)
    except policy_workbook.PolicyWorkbookError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build client-facing PDF for %s", client_name)
        await update.message.reply_text(f"Sorry, I couldn't pull up your policy summary right now: {exc}")
        return

    safe_name = "".join(c for c in client_name if c not in '<>:"/\\|?*').strip()
    await update.message.reply_document(
        document=BytesIO(pdf_bytes),
        filename=f"{safe_name} - Policy Summary.pdf",
        caption=f"Your current policy summary, prepared by {settings.agent_name}.",
    )


# ---------------------------------------------------------------------------
# Group relay: any text/document/photo posted in the client group gets
# forwarded straight to Nic, tagged with whichever client sent it (if
# they're paired) or their Telegram handle otherwise.
# ---------------------------------------------------------------------------

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _group_allowed(update):
        return
    message = update.message
    if message is None or message.from_user is None or message.from_user.is_bot:
        return

    client_name = await client_pairing.get_paired_client(message.from_user.id)
    label = client_name or _client_label(message.from_user)

    try:
        if message.document:
            await context.bot.send_document(
                chat_id=settings.allowed_user_id,
                document=message.document.file_id,
                caption=f"📎 From {label} in the client group"
                + (f":\n{message.caption}" if message.caption else ""),
            )
        elif message.photo:
            await context.bot.send_photo(
                chat_id=settings.allowed_user_id,
                photo=message.photo[-1].file_id,
                caption=f"📷 From {label} in the client group"
                + (f":\n{message.caption}" if message.caption else ""),
            )
        elif message.text:
            await context.bot.send_message(
                chat_id=settings.allowed_user_id,
                text=f"💬 {label} in the client group:\n{message.text}",
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to relay group message from %s to Nic", label)
        # Don't error out in the group chat itself - the client shouldn't
        # see internal relay failures. Nic just won't get this one; it's
        # still sitting in the group for him to see manually.


# ---------------------------------------------------------------------------
# Meeting booking - pick a day, pick a free slot, book it on Nic's calendar.
# ---------------------------------------------------------------------------

def _free_slots(events: list[dict], day: date) -> list[tuple[datetime, datetime]]:
    """Same gap-finding logic as bot.py's _free_slots (duplicated
    deliberately - see module docstring), plus chopping each gap into fixed
    MEETING_SLOT_MINUTES chunks a client can actually pick as one button."""
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

    gaps = []
    cursor = window_start
    for s_dt, e_dt in busy:
        if s_dt > cursor:
            gaps.append((cursor, s_dt))
        cursor = max(cursor, e_dt)
    if cursor < window_end:
        gaps.append((cursor, window_end))

    slot_len = timedelta(minutes=MEETING_SLOT_MINUTES)
    slots = []
    for gap_start, gap_end in gaps:
        cursor = gap_start
        while cursor + slot_len <= gap_end:
            slots.append((cursor, cursor + slot_len))
            cursor += slot_len
    return slots


async def start_booking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not settings.calendar_configured:
        await update.message.reply_text(
            "Meeting booking isn't set up yet — ask your agent to connect their calendar."
        )
        return
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    buttons = []
    for i in range(1, BOOKING_LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=i)
        if day.weekday() >= 5:  # skip Sat/Sun
            continue
        buttons.append([InlineKeyboardButton(day.strftime("%a %d %b"), callback_data=f"mday:{day.isoformat()}")])
    pending_meeting[update.effective_user.id] = {"step": "choose_day"}
    await update.message.reply_text("Which day works for you?", reply_markup=InlineKeyboardMarkup(buttons))


async def book_meeting_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not (_is_private(update) or _group_allowed(update)):
        return
    await start_booking(update, context)


async def meeting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    session = pending_meeting.get(user_id)
    if session is None:
        await query.answer("This booking request has expired — run /book_meeting again.", show_alert=True)
        return
    await query.answer()

    data = query.data
    if data.startswith("mday:"):
        day_iso = data.split(":", 1)[1]
        day = date.fromisoformat(day_iso)
        try:
            events = await calendar_service.list_events(day_iso, day_iso)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to load calendar for booking")
            await query.edit_message_text(f"Couldn't check the calendar: {exc}")
            pending_meeting.pop(user_id, None)
            return
        slots = _free_slots(events, day)
        if not slots:
            await query.edit_message_text(
                f"No free slots on {day.strftime('%a %d %b')} — try another day with /book_meeting."
            )
            pending_meeting.pop(user_id, None)
            return
        buttons = [
            [InlineKeyboardButton(
                f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}",
                # "|" separator, not ":" - isoformat() timestamps are full of
                # colons themselves (time AND the UTC offset), which broke a
                # naive split(":", N) parse of this callback_data.
                callback_data=f"mslot:{s.isoformat()}|{e.isoformat()}",
            )]
            for s, e in slots
        ]
        session["step"] = "choose_slot"
        session["day"] = day_iso
        await query.edit_message_text(
            f"Free slots on {day.strftime('%a %d %b')}:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if data.startswith("mslot:"):
        start_iso, end_iso = data[len("mslot:"):].split("|", 1)
        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        client_name = await client_pairing.get_paired_client(user_id)
        label = client_name or _client_label(query.from_user)

        try:
            await calendar_service.create_event(
                title=f"Meeting with {label} (booked via bot)",
                start_time=start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                end_time=end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                description="Booked via the client bot's /book_meeting.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to create calendar event for booking")
            await query.edit_message_text(f"Couldn't book that slot: {exc}")
            pending_meeting.pop(user_id, None)
            return

        pending_meeting.pop(user_id, None)
        when = f"{start_dt.strftime('%a %d %b, %H:%M')}–{end_dt.strftime('%H:%M')}"
        await query.edit_message_text(f"Booked — {when}. Your agent's been notified.")
        try:
            await context.bot.send_message(
                chat_id=settings.allowed_user_id,
                text=f"📅 {label} booked a meeting via the client bot: {when}",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to notify Nic of new booking")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_private(update):
        await update.message.reply_text(
            "I can:\n"
            "- Show your policy summary as a PDF (once you've linked your account with a "
            "pairing code from your agent)\n"
            "- Book a meeting slot on your agent's calendar — /book_meeting\n\n"
            "In the shared group, just post a document or question and it'll reach your agent directly."
        )
    else:
        await update.message.reply_text(
            "Post a document or question here any time — it goes straight to your agent.\n"
            "Use /book_meeting to grab an open slot on their calendar."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing an update", exc_info=context.error)


def main() -> None:
    if not settings.client_bot_configured:
        raise RuntimeError(
            "CLIENT_BOT_TOKEN isn't set — this is a separate token from TELEGRAM_BOT_TOKEN, "
            "get one from @BotFather."
        )

    app = Application.builder().token(settings.client_bot_token).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whereami", whereami_command))
    app.add_handler(CommandHandler("book_meeting", book_meeting_command))
    app.add_handler(CallbackQueryHandler(meeting_callback, pattern=r"^m(day|slot):"))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_text))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.Document.ALL | filters.PHOTO) & filters.ChatType.GROUPS & ~filters.COMMAND,
        handle_group_message,
    ))
    logger.info("Client bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
