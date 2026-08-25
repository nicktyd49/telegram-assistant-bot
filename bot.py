import base64
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, CommandHandler, filters

from config import settings
from assistant import anthropic_client, run_conversation
from services import calendar_service, sheets_service, pdf_utils
from prompts import POLICY_SUMMARY_PROMPT, RECEIPT_EXTRACTION_PROMPT

MAX_HISTORY_MESSAGES = 40
MAX_POLICY_TEXT_CHARS = 15000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("assistant-bot")

# In-memory per-chat conversation history. Resets on process restart.
conversations: dict[int, list[dict]] = {}


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    conversations[update.effective_chat.id] = []
    await update.message.reply_text(
        "Hi! I'm your personal assistant. I can:\n"
        "- Chat, draft client messages, and answer questions\n"
        "- Summarize a policy — just send me the PDF\n"
        "- Schedule/check/cancel appointments on your calendar — just ask\n"
        "- Log receipts — send a photo of one, or tell me the details in chat\n\n"
        "Try /help for more, or /today for today's schedule."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    lines = [
        "*What I can do*",
        "- Send a policy PDF and I'll reply with a client-friendly summary.",
        "- Send a photo of a receipt and I'll log it" + (
            " to your spreadsheet." if settings.sheets_configured else " — not set up yet."
        ),
        "- Ask me to schedule/check/cancel appointments" + (
            " and I'll update your Google Calendar." if settings.calendar_configured else " — not set up yet."
        ),
        "- /today — today's schedule",
        "- /start — reset our conversation",
        "",
        "Tip: caption a PDF or photo with the word \"receipt\" or \"policy\" if I'd otherwise "
        "guess the wrong type (PDFs default to policy summaries, photos default to receipts).",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    if not settings.calendar_configured:
        await update.message.reply_text("Calendar isn't set up yet — see the README to connect it.")
        return
    today = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
    try:
        events = await calendar_service.list_events(today, today)
    except Exception as exc:  # noqa: BLE001
        logger.exception("today_command failed")
        await update.message.reply_text(f"Couldn't load your calendar: {exc}")
        return

    if not events:
        await update.message.reply_text("Nothing on your calendar today.")
        return

    lines = ["*Today's schedule*"]
    for e in events:
        start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "?"))
        time_str = start
        try:
            time_str = datetime.fromisoformat(start).strftime("%H:%M")
        except ValueError:
            pass
        title = e.get("summary", "(no title)")
        lines.append(f"- {time_str} — {title}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    chat_id = update.effective_chat.id
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

    caption = (update.message.caption or "").lower()
    document = update.message.document

    await update.message.chat.send_action("typing")
    tg_file = await context.bot.get_file(document.file_id)
    pdf_bytes = bytes(await tg_file.download_as_bytearray())

    text = pdf_utils.extract_text(pdf_bytes)
    if not text:
        await update.message.reply_text(
            "I couldn't read any text out of that PDF — it looks like a scanned image rather "
            "than a text PDF. Try sending it as a photo instead, or a text-based export."
        )
        return

    if "receipt" in caption:
        await _extract_and_log_receipt_from_text(update, text)
    else:
        await _summarize_policy_from_text(update, text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    caption = (update.message.caption or "").lower()
    photo = update.message.photo[-1]  # largest size

    await update.message.chat.send_action("typing")
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    if "policy" in caption:
        await _summarize_policy_from_image(update, image_b64)
    else:
        await _extract_and_log_receipt_from_image(update, image_b64)


async def _summarize_policy_from_text(update: Update, text: str) -> None:
    truncated = text[:MAX_POLICY_TEXT_CHARS]
    response = await anthropic_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=POLICY_SUMMARY_PROMPT,
        messages=[{"role": "user", "content": f"Policy document text:\n\n{truncated}"}],
    )
    summary = "".join(b.text for b in response.content if b.type == "text").strip()
    await update.message.reply_text(summary or "I couldn't produce a summary from this document.",
                                     parse_mode=ParseMode.MARKDOWN)


async def _summarize_policy_from_image(update: Update, image_b64: str) -> None:
    response = await anthropic_client.messages.create(
        model=settings.anthropic_model,
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


def _parse_receipt_json(raw_text: str) -> dict | None:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Could not parse receipt JSON from model output: %s", raw_text[:300])
        return None


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
        model=settings.anthropic_model,
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
    data = _parse_receipt_json(raw)
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
        model=settings.anthropic_model,
        max_tokens=512,
        system=RECEIPT_EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": f"Receipt document text:\n\n{truncated}"}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    data = _parse_receipt_json(raw)
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
    app.add_handler(MessageHandler(filters.Document.PDF, handle_policy_or_receipt_pdf))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
