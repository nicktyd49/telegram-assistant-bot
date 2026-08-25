# Personal Assistant Telegram Bot

A Telegram bot that chats with you (and only you) using Claude, as a starting point for
a personal-assistant workflow: policy summaries, drafting client messages, and later
scheduling.

## 1. Create the Telegram bot

1. In Telegram, message **@BotFather**.
2. Send `/newbot`, follow the prompts (pick a name and a username ending in `bot`).
3. BotFather gives you a token like `123456789:AAExample...` — save it, this is `TELEGRAM_BOT_TOKEN`.

## 2. Get your Telegram user ID

1. Message **@userinfobot** in Telegram.
2. It replies with your numeric user ID — save it, this is `ALLOWED_USER_ID`.
   (This locks the bot to only respond to you — anyone else who finds your bot is ignored.)

## 3. Get your Anthropic API key

From https://console.anthropic.com — this is `ANTHROPIC_API_KEY`. Billed separately/pay-as-you-go
from your Claude.ai subscription.

## 4. Run it locally to test

```bash
cd ~/telegram-assistant-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and fill in the three values above
python bot.py
```

Message your bot in Telegram — it should reply. Ctrl+C to stop.

## 5. Deploy to Railway (so it runs without your Mac)

1. Push this folder to a new GitHub repo (private is fine).
2. Go to https://railway.app, sign in, **New Project → Deploy from GitHub repo**, pick this repo.
3. In the Railway project's **Variables** tab, add `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`,
   `ALLOWED_USER_ID` (same values as your local `.env`).
4. Railway auto-detects the `Procfile` and runs `python bot.py` as a worker — no public URL needed
   since the bot uses polling, not webhooks.
5. Check the **Deployments** logs for `Bot starting (polling)...` to confirm it's live, then
   message it in Telegram from your phone (Mac can be off).

## Notes / current scope

- Conversation history is in-memory per chat and resets whenever the process restarts
  (e.g. on redeploy). Fine for a personal assistant; not a durable chat log.
- Only responds to `ALLOWED_USER_ID` — everyone else is silently ignored.
- No calendar or file access yet. Those are separate next steps once this loop is working:
  Google Calendar integration for scheduling, and a policy-summary flow (upload a PDF, get a
  client-ready summary back, optionally as an Excel sheet).
