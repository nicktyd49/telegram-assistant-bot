# Personal Assistant Telegram Bot

A Telegram bot for an insurance agent (and only them) that:

- Chats using Claude, drafts client messages, general admin support.
- **Summarizes policy documents** — send a PDF, get a client-ready summary back.
- **Schedules appointments** on your real Google Calendar, just by asking in chat
  ("book a call with John Tan next Tuesday 3pm").
- **Logs receipts** — send a photo of one and it's extracted and saved to a Google Sheet.

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

## 4. Set up Google Calendar + Sheets access (one service account, used for both)

This uses a **service account** — a robot Google account you create, which you then invite
into your calendar and spreadsheet like you'd invite a person. No login/consent flow needed,
which matters because the bot runs unattended on a server.

1. Go to https://console.cloud.google.com and create a new project (or use an existing one).
2. In **APIs & Services → Library**, enable:
   - **Google Calendar API**
   - **Google Sheets API**
3. In **APIs & Services → Credentials**, click **Create Credentials → Service account**.
   Give it any name (e.g. "assistant-bot"), skip the optional role/access steps, click Done.
4. Click into the new service account → **Keys** tab → **Add Key → Create new key → JSON**.
   A `.json` file downloads — this is your `GOOGLE_SERVICE_ACCOUNT_JSON` (see step 6 below for
   how to use it). **Keep this file private** — treat it like a password.
5. Open the downloaded JSON file and copy the `client_email` value
   (looks like `assistant-bot@your-project.iam.gserviceaccount.com`).

### Share your calendar with it

1. Open https://calendar.google.com → your calendar's settings (gear icon → Settings) →
   pick your calendar under "Settings for my calendars".
2. Under **Share with specific people**, click **Add people**, paste the service account's
   `client_email`, set permission to **Make changes to events**, click Send.
3. On the same settings page, under **Integrate calendar**, copy the **Calendar ID**
   (usually just your Gmail address) — this is `GOOGLE_CALENDAR_ID`.

### Create and share the receipts spreadsheet

1. Create a new Google Sheet (any name, e.g. "Business Receipts") at https://sheets.google.com.
   You don't need to add headers — the bot creates a "Receipts" tab with headers itself.
2. Click **Share**, paste the same service account `client_email`, set role to **Editor**, send.
3. Copy the spreadsheet ID from its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit` — this is `GOOGLE_SHEET_ID`.

## 5. Run it locally to test

```bash
cd ~/telegram-assistant-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and fill in all the values from steps 1-4
python bot.py
```

Message your bot in Telegram — it should reply. Try:
- Sending a policy PDF
- "What's on my calendar this week?"
- "Book a call with Jane next Thursday at 2pm about her renewal"
- Sending a photo of a receipt
- `/today`, `/help`

Ctrl+C to stop.

## 6. Deploy to Railway (so it runs without your Mac)

1. Push this folder to a new GitHub repo (private is fine).
2. Go to https://railway.app, sign in, **New Project → Deploy from GitHub repo**, pick this repo.
3. In the Railway project's **Variables** tab, add every variable from your local `.env`:
   `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `ALLOWED_USER_ID`, `TIMEZONE`, `DEFAULT_CURRENCY`,
   `GOOGLE_CALENDAR_ID`, `GOOGLE_SHEET_ID`.
   For `GOOGLE_SERVICE_ACCOUNT_JSON`, open the downloaded JSON key file, copy its **entire
   contents**, and paste it as the value of that one variable (Railway's variable editor
   handles multi-line values fine).
4. Railway auto-detects the `Procfile` and runs `python bot.py` as a worker — no public URL
   needed since the bot uses polling, not webhooks.
5. Check the **Deployments** logs for `Bot starting (polling)...` and no "not fully configured"
   warnings, then message it in Telegram from your phone (Mac can be off).

## Usage notes

- **Policy PDFs**: just send the file. If it's a scanned image with no text layer, the bot
  will tell you and ask for a photo instead — photos are read directly by Claude's vision,
  which handles scans fine.
- **Receipts**: send a photo (default) — it's read, logged to the Receipts tab of your sheet,
  and confirmed back to you. If a field is misread, just fix it directly in the sheet, or
  describe the expense in text chat instead ("log $18 Grab ride today") and it'll be logged
  via the chat tool instead.
- **Caption override**: PDFs default to policy summaries and photos default to receipts. Add
  the word "receipt" as a caption on a PDF, or "policy" as a caption on a photo, to flip that.
- **Scheduling**: talk to it naturally — "what's free tomorrow afternoon?", "cancel my 3pm
  with John", "book X for next Tuesday 10am". It resolves relative dates against the
  `TIMEZONE` you set.
- Conversation history (the text chat, not receipts/calendar which live in Google) is
  in-memory per chat and resets whenever the process restarts (e.g. on redeploy).
- Only responds to `ALLOWED_USER_ID` — everyone else is silently ignored.
