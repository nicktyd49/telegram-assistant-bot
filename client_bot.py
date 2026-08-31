"""client_bot.py - the client-facing Telegram bot: a SEPARATE process/token
from bot.py (Nic's own personal assistant). Private DM only - no group
features at all (the client group is just a normal Telegram group Nic
broadcasts documents/updates in himself; this bot has no role there).

Three things it does:

  - Book a meeting: ANYONE who messages this bot in DM can check Nic's
    real calendar availability and book a slot directly - deliberately
    NOT gated behind pairing, since booking a meeting isn't sensitive the
    way seeing someone else's policy details would be.

  - Retrieve: a client can pull up their own Policy Summary, always as a
    PDF (never text) - the trimmed, Policy-Summary-only export from
    services/policy_workbook.get_client_facing_pdf. Requires a one-time
    pairing code from Nic first (services/client_pairing.py).

  - Submit: a paired client can send a document (e.g. a newly issued
    policy PDF) straight to Nic, tagged with their real client name.
    Also pairing-gated.

Deliberately a separate bot from bot.py, not a second command set bolted
onto it: bot.py's single ALLOWED_USER_ID gate and shared in-memory state
(keyed by chat_id) were built for one user in one chat, not many different
people each interacting independently.

Setup checklist (see /client_code in bot.py + README):
  1. CLIENT_BOT_TOKEN, CLIENT_BOT_USERNAME env vars set to the new bot's
     credentials (from @BotFather).
  2. Nic personally sends /start to this bot once in DM, so it's allowed to
     message him proactively (Telegram won't let a bot cold-DM someone who
     has never started a chat with it) - needed for submission relays and
     booking notifications.
"""
from __future__ import annotations

import asyncio
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

# Business-hours convention for meeting booking - duplicated from bot.py's
# _free_slots rather than imported, deliberately (see module docstring on
# why this stays a fully separate process).
WORK_START_HOUR = 9
WORK_END_HOUR = 21
MEETING_SLOT_MINUTES = 60
BOOKING_LOOKAHEAD_DAYS = 7

MENU_RETRIEVE = "📄 Retrieve Policy"
MENU_SUMMARY = "📋 Policy Summary"
MENU_SUBMIT = "📤 Submit a Document"
MENU_REFER = "🤝 Refer a Friend"
MENU_BOOK = "📅 Book a Meeting"
MENU_HELP = "❓ Help"

# Every menu button label, used to tell a real menu tap apart from free
# text typed while some other pending state (e.g. a referral) is open.
MENU_TEXTS = {MENU_RETRIEVE, MENU_SUMMARY, MENU_SUBMIT, MENU_REFER, MENU_BOOK, MENU_HELP}

# Meeting-booking state, keyed by Telegram user id (every user books their
# own meeting independently - no chat_id collisions to worry about since
# this is DM-only, but user id is used rather than chat_id/DM id on general
# principle, matching the rest of this bot). Lost on restart, which just
# means an interrupted booking has to be restarted - nothing destructive.
pending_meeting: dict[int, dict] = {}

# Referral state, keyed by Telegram user id: True while waiting for the
# client to reply with a friend's name/number. Not pairing-gated, same
# reasoning as booking - referring a friend isn't sensitive.
pending_referral: dict[int, bool] = {}


def _client_label(user) -> str:
    """Best-effort human-readable label for a Telegram user, used when the
    person isn't (or isn't yet) paired to a client_name - booking and
    submission both need *some* label to show Nic."""
    if user.username:
        return f"@{user.username}"
    return user.full_name or f"user {user.id}"


def _is_private(update: Update) -> bool:
    return update.effective_chat.type == "private"


def _menu_keyboard(paired: bool) -> ReplyKeyboardMarkup:
    if paired:
        return ReplyKeyboardMarkup(
            [[MENU_RETRIEVE, MENU_SUMMARY], [MENU_SUBMIT, MENU_REFER], [MENU_BOOK, MENU_HELP]],
            resize_keyboard=True,
        )
    # Booking and referring a friend don't need pairing - show them even
    # before/without one.
    return ReplyKeyboardMarkup([[MENU_REFER], [MENU_BOOK], [MENU_HELP]], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private(update):
        return  # this bot only operates in private DM
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
            reply_markup=_menu_keyboard(paired=True),
        )
        return

    existing = await client_pairing.get_paired_client(user_id)
    if existing:
        await update.message.reply_text(
            f"Welcome back — you're linked to {existing}.", reply_markup=_menu_keyboard(paired=True),
        )
        return

    await update.message.reply_text(
        "Hi! You can book a meeting any time — no code needed. To see your policy summary or "
        "send a document, I'll need a one-time pairing code from your agent first.",
        reply_markup=_menu_keyboard(paired=False),
    )


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM-only. Handles: a bare pairing code typed in, the persistent-menu
    button taps, or anything else falls through to a short help nudge."""
    if not _is_private(update):
        return
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # A pending referral takes priority over everything except tapping a
    # real menu button (so a client can back out of it by just tapping
    # elsewhere instead of getting stuck).
    if user_id in pending_referral and text not in MENU_TEXTS:
        await _handle_referral_reply(update, context, text)
        return

    if text == MENU_BOOK:
        await start_booking(update, context)
        return
    if text == MENU_REFER:
        await start_referral(update, context)
        return

    existing = await client_pairing.get_paired_client(user_id)

    if text == MENU_RETRIEVE:
        await send_policy_pdf(update, context)
        return
    if text == MENU_SUMMARY:
        await send_policy_summary_text(update, context)
        return
    if text == MENU_SUBMIT:
        if existing:
            await update.message.reply_text("Go ahead and send me the document — just attach it here.")
        else:
            await update.message.reply_text(
                "I need a one-time pairing code from your agent before I can pass along documents."
            )
        return
    if text == MENU_HELP:
        await help_command(update, context)
        return

    looks_like_code = len(text) == client_pairing.CODE_LENGTH and text.isalnum()
    if not existing and looks_like_code:
        try:
            client_name = await client_pairing.redeem_code(text, user_id)
        except client_pairing.PairingError as exc:
            await update.message.reply_text(str(exc))
            return
        await update.message.reply_text(
            f"You're linked to {client_name}. Use the menu below any time.",
            reply_markup=_menu_keyboard(paired=True),
        )
        return

    await update.message.reply_text("Use the menu below.", reply_markup=_menu_keyboard(paired=bool(existing)))


async def send_policy_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private(update):
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


def _fmt_money(value, decimals: int = 2):
    # Duplicated from bot.py's own _fmt_money rather than imported - see the
    # module docstring on why this file stays fully separate from bot.py.
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return f"${value:,.{decimals}f}"
    return str(value)


def _format_client_facing_summary(summary: dict) -> str:
    # A trimmed version of bot.py's _format_client_summary: same policy/
    # coverage/premium numbers, but deliberately WITHOUT action_items -
    # those are Nic's own sales-facing next-step notes ("recommend adding
    # CI cover"), not copy that should go straight to the client.
    lines = [summary["client_name"]]
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

    totals = summary.get("totals") or {}
    total_cash = _fmt_money(totals.get("premium_cash"))
    total_cpf = _fmt_money(totals.get("premium_cpf"))
    total_bits = [f"{v} cash" if k == "cash" else f"{v} CPF"
                   for k, v in (("cash", total_cash), ("cpf", total_cpf)) if v]
    if total_bits:
        lines.append(f"\nTotal annual premium: {' + '.join(total_bits)}")

    return "\n".join(lines)


async def send_policy_summary_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # The fast, no-file version of send_policy_pdf: same underlying data
    # (services/policy_workbook.get_client_summary), just formatted as a
    # quick chat message instead of a PDF attachment.
    if not _is_private(update):
        return
    user_id = update.effective_user.id
    client_name = await client_pairing.get_paired_client(user_id)
    if not client_name:
        await update.message.reply_text(
            "I don't have you linked to a client yet — ask your agent for a one-time pairing code."
        )
        return

    try:
        summary = await asyncio.to_thread(policy_workbook.get_client_summary, client_name)
    except policy_workbook.PolicyWorkbookError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load client summary for %s", client_name)
        await update.message.reply_text(f"Sorry, I couldn't pull up your policy summary right now: {exc}")
        return

    await update.message.reply_text(_format_client_facing_summary(summary))


# ---------------------------------------------------------------------------
# Refer a friend - not pairing-gated (same reasoning as booking: passing
# along a friend's name isn't sensitive the way seeing someone else's policy
# would be). Two ways to refer: forward the bot directly, or reply here with
# a friend's name/number and it's relayed straight to Nic.
# ---------------------------------------------------------------------------

async def start_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_private(update):
        return
    user_id = update.effective_user.id
    pending_referral[user_id] = True
    lines = [
        "Know someone who could use a policy review?",
        "",
        f"Just reply here with their name and phone number, and I'll pass it straight to {settings.agent_name}.",
    ]
    if settings.client_bot_username:
        lines.append(f"\nOr forward them this bot directly: @{settings.client_bot_username}")
    await update.message.reply_text("\n".join(lines))


async def refer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_referral(update, context)


async def _handle_referral_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    user_id = update.effective_user.id
    pending_referral.pop(user_id, None)
    client_name = await client_pairing.get_paired_client(user_id)
    label = client_name or _client_label(update.effective_user)

    try:
        await context.bot.send_message(
            chat_id=settings.allowed_user_id,
            text=f"🤝 {label} referred a friend via the client bot:\n{text}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to relay referral from %s to Nic", label)
        await update.message.reply_text(f"Sorry, that didn't go through: {exc}. Please try again in a moment.")
        return

    await update.message.reply_text(f"Thanks! I've passed that along to {settings.agent_name}.")


async def handle_private_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM-only, paired clients only: relays a document/photo straight to Nic,
    tagged with the client's real name, and confirms to the client it went
    through. Nic reviews/files it himself the same way he already does
    (his own bot's PDF-logging flow) - this just gets it to him."""
    if not _is_private(update):
        return
    message = update.message
    user_id = update.effective_user.id
    client_name = await client_pairing.get_paired_client(user_id)
    if not client_name:
        await message.reply_text(
            "I need a one-time pairing code from your agent before I can pass this along — "
            "ask them for one and send it to me here."
        )
        return

    label = client_name or _client_label(update.effective_user)
    try:
        if message.document:
            await context.bot.send_document(
                chat_id=settings.allowed_user_id,
                document=message.document.file_id,
                caption=f"📎 {label} submitted a document via the client bot"
                + (f":\n{message.caption}" if message.caption else ""),
            )
        elif message.photo:
            await context.bot.send_photo(
                chat_id=settings.allowed_user_id,
                photo=message.photo[-1].file_id,
                caption=f"📷 {label} submitted a photo via the client bot"
                + (f":\n{message.caption}" if message.caption else ""),
            )
        else:
            return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to relay submission from %s to Nic", label)
        await message.reply_text(f"Sorry, that didn't go through: {exc}. Please try again in a moment.")
        return

    await message.reply_text(f"Sent to {settings.agent_name} — thanks!")


# ---------------------------------------------------------------------------
# Meeting booking - pick a day, pick a free slot, book it on Nic's calendar.
# Open to everyone in DM - deliberately NOT pairing-gated.
# ---------------------------------------------------------------------------

def _free_slots(events: list[dict], day: date) -> list[tuple[datetime, datetime]]:
    """Gaps of at least MEETING_SLOT_MINUTES between events, clamped to the
    business-hours window for that day, chopped into fixed-length chunks a
    client can pick as one button."""
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
    if not _is_private(update):
        return
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
                # colons themselves (time AND the UTC offset).
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
            "- Book a meeting on your agent's calendar — no pairing needed, just /book_meeting\n"
            "- Show a quick text summary of your policies (needs a pairing code from your agent)\n"
            "- Send your full policy summary as a PDF (also needs pairing)\n"
            "- Pass along a document you send me straight to your agent (also needs pairing)\n"
            "- Refer a friend — no pairing needed, just /refer\n\n"
            "Use the menu below any time."
        )
    else:
        await update.message.reply_text("This bot only works in a private chat — please message me directly.")


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
    app.add_handler(CommandHandler("book_meeting", book_meeting_command))
    app.add_handler(CommandHandler("refer", refer_command))
    app.add_handler(CallbackQueryHandler(meeting_callback, pattern=r"^m(day|slot):"))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_text))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO) & filters.ChatType.PRIVATE, handle_private_submission,
    ))
    logger.info("Client bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
