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
import uuid
from datetime import date, datetime, timedelta, time as dt_time
from io import BytesIO
from zoneinfo import ZoneInfo

from telegram import ChatMember, Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from services import calendar_service, policy_workbook, policy_extraction, client_pairing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("client-bot")

# Business-hours convention for meeting booking - duplicated from bot.py's
# _free_slots rather than imported, deliberately (see module docstring on
# why this stays a fully separate process).
WORK_START_HOUR = 9
WORK_END_HOUR = 21
MEETING_SLOT_MINUTES = 60
BOOKING_LOOKAHEAD_DAYS = 7

# Minimum gap Nic wants left free between any two appointments, to account
# for travel time between them. Applied as a full MEETING_BUFFER_MINUTES
# pad on BOTH sides of every existing appointment (not split/halved) in
# _free_slots() below - a freshly-offered slot has no padding of its own
# yet, so only padding the existing appointment fully on both sides
# guarantees the real gap to it is never less than MEETING_BUFFER_MINUTES.
MEETING_BUFFER_MINUTES = 60

MENU_RETRIEVE = "📄 Retrieve Policy"
MENU_SUMMARY = "📋 Policy Summary"
MENU_SUBMIT = "📤 Submit a Document"
MENU_REFER = "🤝 Refer a Friend"
MENU_INVITE = "🔗 Invite Friends"
MENU_BOOK = "📅 Book a Meeting"
MENU_HELP = "❓ Help"

# Every menu button label, used to tell a real menu tap apart from free
# text typed while some other pending state (e.g. a referral) is open.
MENU_TEXTS = {MENU_RETRIEVE, MENU_SUMMARY, MENU_SUBMIT, MENU_REFER, MENU_INVITE, MENU_BOOK, MENU_HELP}

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

# Document-submission state, keyed by Telegram user id: holds whatever of
# {file, Name/DOB details} has arrived so far, since a client may attach
# the document and reply with their details in either order. Only ever
# created for paired clients (see handle_private_submission /
# handle_private_text's MENU_SUBMIT branch) - unlike booking/referral this
# stays pairing-gated the whole way through.
pending_submission: dict[int, dict] = {}

# Per-client Wealth Circle invite links, keyed by client_name. A repeat tap
# of Invite Friends reuses the same link instead of minting a new one every
# time. Lost on restart like the rest of this bot's state - the next tap
# after a restart just mints a fresh link, which is harmless (just one more
# named link sitting in the channel's admin panel).
client_invite_links: dict[str, str] = {}

# Auto-extraction approval state, keyed by a short random token (Telegram
# callback_data has a size limit, so this is an indirection rather than
# putting client_name straight in the button). Set when a paired client's
# submitted document is auto-extracted and Nic is shown a "Send to client"
# button; cleared once he taps it. Lost on restart like the rest of this
# bot's pending state - worst case Nic has to resubmit/re-forward the
# document, nothing destructive.
pending_client_approval: dict[str, str] = {}


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
            [[MENU_RETRIEVE, MENU_SUMMARY], [MENU_SUBMIT, MENU_REFER], [MENU_INVITE, MENU_BOOK], [MENU_HELP]],
            resize_keyboard=True,
        )
    # Booking and referring a friend don't need pairing. Retrieve Policy and
    # Policy Summary are shown too even though both need pairing - tapping
    # either before pairing just explains that a code is needed
    # (send_policy_pdf / send_policy_summary_text already handle that),
    # rather than hiding the buttons and leaving a client with no way to
    # discover the features exist at all. Submit a Document and Invite
    # Friends stay hidden pre-pairing - unlike the two read-only buttons
    # above, sending Nic an unsolicited document, or minting a named invite
    # link tied to an unknown identity, isn't something to invite before he
    # knows who it's from.
    return ReplyKeyboardMarkup(
        [[MENU_RETRIEVE, MENU_SUMMARY], [MENU_REFER], [MENU_BOOK], [MENU_HELP]], resize_keyboard=True,
    )


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

    awaiting = await client_pairing.get_awaiting_name(user_id)
    if awaiting:
        referred_by = awaiting.get("referred_by")
        intro = f"Welcome! You joined via {referred_by}'s invite. " if referred_by else "Welcome! "
        await update.message.reply_text(
            intro + "To get you set up, what's your full name? This is how I'll file your policy records."
        )
        return

    await update.message.reply_text(
        "Hi! You can book a meeting any time — no code needed. To see your policy summary or "
        "send a document, I'll need a one-time pairing code from your agent first.",
        reply_markup=_menu_keyboard(paired=False),
    )


async def _handle_new_client_name(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, awaiting: dict,
) -> None:
    """Handles a free-text reply from a Telegram user who joined the Wealth
    Circle channel via a named Invite Friends link and is now being asked
    for their full name (see start() / handle_private_text()). Validates
    against existing client names first - so a typo or a joke reply can
    never silently attach itself to a real client's existing OneDrive
    folder - then creates a blank workbook for them, pairs them directly
    (no code needed), and lets Nic know."""
    full_name = text.strip()
    if len(full_name) < 2:
        await update.message.reply_text(
            "Could you send your full name, please? This is how I'll file your policy records."
        )
        return

    existing_names = await policy_workbook.list_client_names()
    if any(full_name.lower() == n.lower() for n in existing_names):
        await update.message.reply_text(
            f"{settings.agent_name} already has a client on file under that exact name — to avoid mixing "
            f"up records, please double-check the spelling, or let {settings.agent_name} know directly."
        )
        return

    try:
        await asyncio.to_thread(policy_workbook.create_blank_client, full_name)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to create blank OneDrive workbook for new referred client %s", full_name)
        await update.message.reply_text(
            "Sorry, something went wrong setting up your records — please try again in a moment."
        )
        return

    await client_pairing.pair_directly(user_id, full_name)
    await client_pairing.clear_awaiting_name(user_id)

    await update.message.reply_text(
        f"Thanks, {full_name} — you're all set! Use the menu below any time.",
        reply_markup=_menu_keyboard(paired=True),
    )

    referred_by = awaiting.get("referred_by")
    note = f" (referred by {referred_by})" if referred_by else ""
    try:
        await context.bot.send_message(
            chat_id=settings.allowed_user_id,
            text=f"✅ New client auto-paired: {full_name}{note}\nA blank policy folder has been created on OneDrive.",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not notify Nic about new auto-paired client %s", full_name)


async def handle_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM-only. Handles: a bare pairing code typed in, the persistent-menu
    button taps, or anything else falls through to a short help nudge."""
    if not _is_private(update):
        return
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # A brand-new referred joiner giving their full name takes top priority
    # - they aren't paired yet, so none of the other branches below apply
    # to them anyway, and this needs to resolve before they can do anything
    # else with the bot.
    if text not in MENU_TEXTS:
        awaiting = await client_pairing.get_awaiting_name(user_id)
        if awaiting:
            await _handle_new_client_name(update, context, user_id, text, awaiting)
            return

    # A pending referral takes priority over everything except tapping a
    # real menu button (so a client can back out of it by just tapping
    # elsewhere instead of getting stuck).
    if user_id in pending_referral and text not in MENU_TEXTS:
        await _handle_referral_reply(update, context, text)
        return

    if user_id in pending_submission and text not in MENU_TEXTS:
        await _handle_submission_details(update, context, text)
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
            pending_submission[user_id] = {"details": None, "file": None}
            await update.message.reply_text(
                "Before you send it — for your privacy (PDPA), please crop out or black out your personal "
                "details (e.g. NRIC number, address, signature) from the document first.\n\n"
                "That's also why I need your full Name and Date of Birth here as text separately, so it "
                "doesn't hold up your policy summary. You can send the document and your details in any "
                f"order — I'll pass everything to {settings.agent_name} once I have both."
            )
        else:
            await update.message.reply_text(
                "I need a one-time pairing code from your agent before I can pass along documents."
            )
        return
    if text == MENU_INVITE:
        await send_invite_link(update, context)
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


async def send_invite_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM-only, paired clients only: mints (or reuses) a personal invite
    link to Nic's client channel, named after the client, with join
    requests turned on. That does two things at once: friends who tap it
    need Nic's approval before they're actually let in, and because the
    link is named per-client, he can tell who invited someone when he
    reviews that approval - without a single shared link that can't be
    attributed to anyone."""
    if not _is_private(update):
        return
    user_id = update.effective_user.id
    client_name = await client_pairing.get_paired_client(user_id)
    if not client_name:
        await update.message.reply_text(
            "I need a one-time pairing code from your agent before I can give you an invite link."
        )
        return

    if not settings.client_group_chat_id:
        logger.error("Invite Friends tapped but CLIENT_GROUP_CHAT_ID isn't configured.")
        await update.message.reply_text("Invite links aren't set up yet — let your agent know.")
        return

    link = client_invite_links.get(client_name)
    if not link:
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=settings.client_group_chat_id,
                name=client_name[:32],
                creates_join_request=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to create an invite link for %s", client_name)
            await update.message.reply_text(f"Sorry, that didn't work: {exc}. Please try again in a moment.")
            return
        link = invite.invite_link
        client_invite_links[client_name] = link

    await update.message.reply_text(
        "Here's your personal invite link — feel free to share it with friends who might be interested:\n\n"
        f"{link}\n\n"
        f"When someone taps it, they'll need {settings.agent_name}'s approval to join (usually quick), "
        "and since this link is yours, he'll know it came through you."
    )


async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_invite_link(update, context)


async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM-only, Nic only: forward any message from a channel or group here
    to get its chat ID back. This is the easiest way to find
    CLIENT_GROUP_CHAT_ID (needed for Invite Friends, and for PA's own
    "post to client group" button) without relying on a command posted
    inside the chat itself."""
    if not _is_private(update):
        return
    if update.effective_user.id != settings.allowed_user_id:
        return
    origin = update.message.forward_origin
    chat = getattr(origin, "chat", None) if origin else None
    if chat is None:
        return
    await update.message.reply_text(
        f"That's from: {chat.title or chat.id}\nChat ID: {chat.id}\n\n"
        "Set CLIENT_GROUP_CHAT_ID to this on Railway to enable Invite Friends."
    )


async def handle_private_submission(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM-only, paired clients only: a client can attach a document/photo
    here at any time, whether or not they tapped Submit a Document first.
    Holds it in pending_submission until their Name/DOB details are also
    collected (see _handle_submission_details), then relays both to Nic
    together via _finish_submission — clients are asked to crop/black out
    their personal details from the document itself (PDPA), so this is what
    supplies reliable Name/DOB text instead of Nic having to chase it down
    separately."""
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

    if message.document:
        file_info = {
            "kind": "document", "file_id": message.document.file_id, "caption": message.caption,
            "filename": message.document.file_name,
        }
    elif message.photo:
        file_info = {
            "kind": "photo", "file_id": message.photo[-1].file_id, "caption": message.caption,
            "filename": None,
        }
    else:
        return

    entry = pending_submission.setdefault(user_id, {"details": None, "file": None})
    entry["file"] = file_info

    if entry["details"]:
        await _finish_submission(update, context, user_id, client_name)
        return

    await message.reply_text(
        "Got the document — one more thing: please reply here with your full Name and Date of Birth "
        "so it doesn't hold up your policy summary. And if you haven't already, please make sure your "
        "personal details (e.g. NRIC number, address) were cropped or blacked out of what you just sent."
    )


async def _handle_submission_details(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Handles a free-text reply while pending_submission is open for this
    user — treated as their Name/DOB. Finishes the submission immediately
    if the document already came in, otherwise just holds the details and
    waits for it."""
    user_id = update.effective_user.id
    client_name = await client_pairing.get_paired_client(user_id)
    if not client_name:
        pending_submission.pop(user_id, None)
        return

    entry = pending_submission.setdefault(user_id, {"details": None, "file": None})
    entry["details"] = text

    if entry["file"]:
        await _finish_submission(update, context, user_id, client_name)
        return

    await update.message.reply_text(
        "Got it, thanks. Go ahead and attach the document whenever you're ready — "
        "just make sure your personal details (e.g. NRIC number, address) are cropped or blacked out first."
    )


async def _finish_submission(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, client_name: str) -> None:
    """Sends the held file to Nic with the client's typed Name/DOB folded
    into the caption, then clears the pending state and confirms to the
    client it went through."""
    entry = pending_submission.pop(user_id, None)
    if not entry or not entry.get("file"):
        return

    file_info = entry["file"]
    details = entry.get("details")
    label = client_name or _client_label(update.effective_user)
    kind_label = "document" if file_info["kind"] == "document" else "photo"
    kind_emoji = "📎" if file_info["kind"] == "document" else "📷"

    caption_lines = [f"{kind_emoji} {label} submitted a {kind_label} via the client bot"]
    if details:
        caption_lines.append(f"Name/DOB provided: {details}")
    if file_info.get("caption"):
        caption_lines.append(file_info["caption"])
    caption = "\n".join(caption_lines)

    try:
        if file_info["kind"] == "document":
            await context.bot.send_document(
                chat_id=settings.allowed_user_id,
                document=file_info["file_id"],
                caption=caption,
            )
        else:
            await context.bot.send_photo(
                chat_id=settings.allowed_user_id,
                photo=file_info["file_id"],
                caption=caption,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to relay submission from %s to Nic", label)
        await update.message.reply_text(f"Sorry, that didn't go through: {exc}. Please try again in a moment.")
        return

    await update.message.reply_text(f"Sent to {settings.agent_name} — thanks!")

    if file_info["kind"] == "document":
        await _try_auto_extract(context, client_name, file_info)


async def _try_auto_extract(context: ContextTypes.DEFAULT_TYPE, client_name: str, file_info: dict) -> None:
    """Best-effort: downloads the just-submitted document, runs it through
    the same Claude extraction Nic's own bot uses (services/
    policy_extraction.py), saves it into the client's workbook, and sends
    Nic a preview of the finished policy summary with a one-tap "Send to
    client" button - so a routine submission doesn't need Nic to manually
    re-forward the document into his own bot first. He still reviews and
    approves before the client sees anything; nothing here auto-sends.

    Silent no-op on any failure (e.g. a scanned PDF with no text layer) -
    the plain relay above already went through either way, so Nic can
    still process the document by hand exactly as before."""
    try:
        tg_file = await context.bot.get_file(file_info["file_id"])
        pdf_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:  # noqa: BLE001
        logger.exception("Could not download submitted document for auto-extraction")
        return

    try:
        fields = await policy_extraction.extract_policy_fields(pdf_bytes)
    except policy_extraction.ExtractionError:
        logger.info("Auto-extraction skipped for %s's submission - not a readable text PDF", client_name)
        return
    except Exception:  # noqa: BLE001
        logger.exception("Auto-extraction failed for %s's submission", client_name)
        return

    try:
        await policy_extraction.save_extracted_policy(
            client_name, fields, pdf_bytes=pdf_bytes, pdf_filename=file_info.get("filename"),
        )
        preview_bytes = await policy_workbook.get_client_facing_pdf(client_name)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to save extracted policy or build preview for %s", client_name)
        try:
            await context.bot.send_message(
                chat_id=settings.allowed_user_id,
                text=(
                    f"⚠️ Auto-extracted {client_name}'s submission but couldn't save it or build the "
                    "summary — please file it manually via your own bot."
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not even notify Nic about the auto-extraction failure for %s", client_name)
        return

    token = uuid.uuid4().hex[:10]
    pending_client_approval[token] = client_name
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"✅ Send to {client_name}", callback_data=f"approve_send:{token}")]]
    )
    try:
        await context.bot.send_document(
            chat_id=settings.allowed_user_id,
            document=BytesIO(preview_bytes),
            filename=f"{client_name} - Policy Summary.pdf",
            caption=(
                f"Auto-extracted and saved {client_name}'s policy summary from their submission.\n"
                "Review it, then tap below to send it to them."
            ),
            reply_markup=keyboard,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not send auto-extraction preview + approval button to Nic for %s", client_name)


async def handle_approve_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nic-only: fires when he taps the "✅ Send to {client}" button on an
    auto-extracted policy summary preview. Delivers the client-facing PDF
    to that client via this same bot's existing chat with them - this is
    the one and only place a client actually receives the auto-generated
    summary; nothing sends it to them automatically."""
    query = update.callback_query
    if update.effective_user.id != settings.allowed_user_id:
        await query.answer("Only Nic can approve this.", show_alert=True)
        return

    token = query.data.split(":", 1)[1]
    client_name = pending_client_approval.pop(token, None)
    if not client_name:
        await query.answer("This approval has expired — resend the document if you need to try again.", show_alert=True)
        return

    telegram_user_id = await client_pairing.get_telegram_id_for_client(client_name)
    if telegram_user_id is None:
        await query.answer(f"Couldn't find {client_name}'s Telegram chat — send it manually.", show_alert=True)
        return

    try:
        pdf_bytes = await policy_workbook.get_client_facing_pdf(client_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build client-facing PDF for %s at send time", client_name)
        await query.answer(f"Couldn't build the PDF: {exc}", show_alert=True)
        return

    try:
        await context.bot.send_document(
            chat_id=telegram_user_id,
            document=BytesIO(pdf_bytes),
            filename=f"{client_name} - Policy Summary.pdf",
            caption=f"Here's your updated policy summary, prepared by {settings.agent_name}.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to deliver approved policy summary to %s", client_name)
        await query.answer(f"Couldn't send it to the client: {exc}", show_alert=True)
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await query.answer("Sent!")
    await query.message.reply_text(f"✅ Sent to {client_name}.")


async def handle_channel_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires when someone's membership status in the Wealth Circle channel
    changes - including an approved join request. If they joined via one
    of the named Invite Friends links (services/client_pairing referral
    links) and aren't already a paired client, marks them as awaiting a
    name so the next time they message this bot (e.g. via a pinned
    channel message pointing at it) it can ask for their full name and
    auto-pair them straight away - see start()/handle_private_text().

    Deliberately does NOT try to DM the new joiner directly here: Telegram
    does not let a bot message someone who has never started a chat with
    it, and someone who just joined via a channel invite link usually
    hasn't. The awaiting-name flag is durable (stored via
    client_pairing.py, not in-memory) precisely so it survives until
    whenever they do first message the bot."""
    cm = update.chat_member
    if cm is None or settings.client_group_chat_id is None:
        return
    if cm.chat.id != settings.client_group_chat_id:
        return

    old_status = cm.old_chat_member.status
    new_status = cm.new_chat_member.status
    if new_status != ChatMember.MEMBER or old_status == ChatMember.MEMBER:
        return  # only a fresh join, not e.g. an admin-permission change

    user = cm.new_chat_member.user
    if user.is_bot:
        return

    invite_link = cm.invite_link
    referred_by = invite_link.name if invite_link and invite_link.name else None
    if not referred_by:
        return  # joined some other way (not a named Invite Friends link) - nothing to auto-pair

    already_paired = await client_pairing.get_paired_client(user.id)
    if already_paired:
        return  # already a client somehow - leave their existing pairing alone

    await client_pairing.mark_awaiting_name(user.id, referred_by)

    try:
        await context.bot.send_message(
            chat_id=settings.allowed_user_id,
            text=(
                f"🆕 Someone joined Wealth Circle via {referred_by}'s invite link. "
                "I'll auto-pair them once they message me and give their full name."
            ),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not notify Nic about new referral join via %s's link", referred_by)


# ---------------------------------------------------------------------------
# Meeting booking - pick a day, pick a free slot, book it on Nic's calendar.
# Open to everyone in DM - deliberately NOT pairing-gated.
# ---------------------------------------------------------------------------

def _free_slots(events: list[dict], day: date) -> list[tuple[datetime, datetime]]:
    """Gaps of at least MEETING_SLOT_MINUTES between events, clamped to the
    business-hours window for that day, chopped into fixed-length chunks a
    client can pick as one button. Each event is padded by the full
    MEETING_BUFFER_MINUTES on both sides first, so an offered slot is
    always at least MEETING_BUFFER_MINUTES of travel time away from any
    existing appointment."""
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
            "- Refer a friend — no pairing needed, just /refer\n"
            "- Give you a personal invite link to share with friends (needs pairing), just /invite\n\n"
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
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CallbackQueryHandler(meeting_callback, pattern=r"^m(day|slot):"))
    app.add_handler(CallbackQueryHandler(handle_approve_send, pattern=r"^approve_send:"))
    app.add_handler(ChatMemberHandler(handle_channel_join, chat_member_types=ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_forwarded_message))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_text))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO) & filters.ChatType.PRIVATE, handle_private_submission,
    ))
    logger.info("Client bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
