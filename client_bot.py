"""client_bot.py - the client-facing Telegram bot: a SEPARATE process/token
from bot.py (Nic's own personal assistant). Private DM only - no group
features at all. Two things it does, both gated behind a one-time pairing
code from Nic (/client_code in bot.py, via services/client_pairing.py):

  - Retrieve: a client can pull up their own Policy Summary, always as a
    PDF (never text) - the trimmed, Policy-Summary-only export from
    services/policy_workbook.get_client_facing_pdf.

  - Submit: a client can send a document (e.g. a newly issued policy PDF)
    straight to Nic, tagged with their real client name.

The client group Nic uses for broadcasting documents/updates is just a
normal Telegram group he posts in himself - this bot has no role there.
Appointment/meeting booking is handled on Nic's own personal bot, not here.

Deliberately a separate bot from bot.py, not a second command set bolted
onto it: bot.py's single ALLOWED_USER_ID gate and shared in-memory state
(keyed by chat_id) were built for one user in one chat, not many different
clients each pairing their own Telegram account.

Setup checklist (see /client_code in bot.py + README):
  1. CLIENT_BOT_TOKEN, CLIENT_BOT_USERNAME env vars set to the new bot's
     credentials (from @BotFather).
  2. Nic personally sends /start to this bot once in DM, so it's allowed to
     message him proactively (Telegram won't let a bot cold-DM someone who
     has never started a chat with it) - needed for submission relays.
"""
from __future__ import annotations

import logging
from io import BytesIO

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from services import policy_workbook, client_pairing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("client-bot")

MENU_POLICY = "📄 My Policy Summary"
MENU_SUBMIT = "📤 Submit a Document"
MENU_HELP = "❓ Help"


def _client_label(user) -> str:
    """Best-effort human-readable label for a Telegram user, used only as a
    fallback when relaying a submission from someone who hasn't paired yet
    (shouldn't normally happen - submission is paired-only - but keeps the
    message useful instead of crashing if it ever does)."""
    if user.username:
        return f"@{user.username}"
    return user.full_name or f"user {user.id}"


def _is_private(update: Update) -> bool:
    return update.effective_chat.type == "private"


def _paired_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[MENU_POLICY], [MENU_SUBMIT, MENU_HELP]], resize_keyboard=True)


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
        "Hi! To use this bot, I need a one-time pairing code from your agent first — ask them "
        "for one, then send it to me here (just the code, or the link they gave you)."
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
    if text == MENU_SUBMIT:
        await update.message.reply_text("Go ahead and send me the document — just attach it here.")
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
            "Use the menu below.", reply_markup=_paired_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "I need a one-time pairing code from your agent before I can help — "
            "ask them for one and send it to me here."
        )


async def send_policy_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_private(update):
        await update.message.reply_text(
            "I can:\n"
            "- Show your policy summary as a PDF (once you've linked your account with a "
            "pairing code from your agent)\n"
            "- Pass along a document you send me straight to your agent\n\n"
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
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_text))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO) & filters.ChatType.PRIVATE, handle_private_submission,
    ))
    logger.info("Client bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
