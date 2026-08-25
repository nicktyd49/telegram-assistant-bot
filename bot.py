import logging
import os

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, CommandHandler, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

MAX_HISTORY_MESSAGES = 20

SYSTEM_PROMPT = """You are a personal assistant for an insurance agent/broker, reachable via Telegram.
You help with things like: summarizing insurance policy documents in client-friendly language,
drafting messages to clients, and general day-to-day admin support.
Be concise and practical — this is a chat interface, not a document editor.
If asked to do something you cannot actually do (e.g. book a real calendar event, access a file
you have not been shown), say so plainly rather than pretending to have done it."""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("assistant-bot")

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# In-memory per-chat conversation history. Resets on process restart.
conversations: dict[int, list[dict]] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    conversations[update.effective_chat.id] = []
    await update.message.reply_text(
        "Hi! I'm your personal assistant. Send me a message to get started."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id != ALLOWED_USER_ID:
        logger.warning("Ignored message from unauthorized user_id=%s username=%s", user.id, user.username)
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text
    history = conversations.setdefault(chat_id, [])

    history.append({"role": "user", "content": user_text})
    history[:] = history[-MAX_HISTORY_MESSAGES:]

    await update.message.chat.send_action("typing")

    try:
        response = await anthropic_client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=history,
        )
        reply_text = "".join(block.text for block in response.content if block.type == "text")
    except Exception:
        logger.exception("Anthropic API call failed")
        await update.message.reply_text("Something went wrong on my end — try again in a moment.")
        return

    history.append({"role": "assistant", "content": reply_text})
    await update.message.reply_text(reply_text)


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
