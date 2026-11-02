"""Generates a full client follow-up playbook (status summary, next follow-up
date, drafted message, and reply-scenario guidance) from whatever Nic sends in
a Follow-Up Playbook session — pasted/forwarded conversation text, and/or
screenshots of the chat with a prospect/client.

Mirrors the same one-shot vision+text Claude call style as
ig_post_service.generate_caption and the receipt/policy extraction prompts
elsewhere in this bot, but asks for strict JSON (like
ig_post_service.select_carousel_photos) since the result needs to be parsed
into separate message/calendar-event pieces rather than just shown as text.
"""
from __future__ import annotations

import json
import logging

from config import settings

logger = logging.getLogger("assistant-bot.playbook_service")

PLAYBOOK_SYSTEM_PROMPT = """You help a Singapore wealth/financial advisor (life insurance, \
investment-linked policies) plan their follow-up with a prospect or existing client, based on \
whatever they send you — pasted or forwarded chat text, and/or screenshots of a WhatsApp/email \
conversation with that person.

You will be told today's date and the client/prospect's name. From the conversation given:

1. Reconstruct the timeline: what the advisor sent, when, what the prospect said back (if \
anything), and how much time has passed since the last message either side sent. Note any soft \
commitments the prospect made ("will read through", "let me check", "will get back to you").

2. Work out the next follow-up date. Use the advisor's own spacing between past follow-ups in \
this thread as the baseline cadence if it's visible (commonly 3-5 days) — don't invent a generic \
rule if the actual pattern is visible. Don't suggest following up the same day unless the \
prospect has gone unusually quiet for how high-stakes this is. If a reasonable follow-up point \
has already passed relative to today, pick today or the next business day instead of a date in \
the past. Always give a real calendar date (YYYY-MM-DD), reasoned from today's date.

3. Draft the follow-up message the advisor should send. Identify what's actually likely \
stalling the prospect (a hard choice between options, not having time to read something long, \
not understanding a term, going quiet after a soft commitment) and address it directly rather \
than writing a generic "just checking in". Prefer giving the prospect an easy next step (e.g. \
offering a short call) over repeating an open-ended question that's already gone unanswered. \
Low-pressure tone, never pushy. Write it the way a real person texts, not corporate copy.

4. Build a short reply-scenario playbook: how the advisor should respond depending on what the \
prospect says back. Always include scenarios for: they agree to a call or meeting; they ask a \
question over text instead; they say they need more time; they go quiet again with no reply; \
and they decline/aren't interested. Add or adapt scenarios if something about this specific \
situation calls for it. Each scenario's guidance should be 1-3 sentences, concrete and specific \
to this conversation — not generic sales advice.

Reply with STRICT JSON only — no markdown, no commentary outside the JSON, no code fences:

{
  "status_summary": "<2-4 sentences: what's happened in the thread so far and where it stands>",
  "next_followup_date": "<YYYY-MM-DD>",
  "next_followup_reason": "<one sentence: why this date>",
  "message": "<the drafted follow-up message, ready to send as-is>",
  "scenarios": [
    {"trigger": "<short label, e.g. 'They agree to a call'>", "guidance": "<1-3 sentences>"}
  ]
}

If the conversation given doesn't actually contain enough to work with (no prior message from \
the advisor, or no sense of timing at all), still do your best with what's there rather than \
refusing — use today's date as a reasonable starting point and say so plainly in \
status_summary."""


def _build_user_content(client_name: str, notes_text: str, image_parts: list[dict], today_str: str) -> list[dict]:
    content: list[dict] = [
        {
            "type": "text",
            "text": f"Today's date: {today_str}\nProspect/client name: {client_name}\n\n"
                    + (notes_text if notes_text else "(No pasted text — see attached screenshot(s) below.)"),
        }
    ]
    for part in image_parts:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": part["media_type"], "data": part["data"]},
        })
    return content


async def generate_playbook(
    client_name: str, notes_text: str, image_parts: list[dict], today_str: str,
) -> dict:
    """Returns the parsed JSON dict described in PLAYBOOK_SYSTEM_PROMPT. Raises
    on an API error or if the response can't be parsed as the expected JSON —
    callers should catch and show a plain error rather than a half-built
    playbook, since a wrong follow-up date is worse than no playbook."""
    from assistant import anthropic_client  # local import avoids a cycle at module load

    response = await anthropic_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=2048,
        system=PLAYBOOK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_content(client_name, notes_text, image_parts, today_str)}],
    )
    raw = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    data = json.loads(raw)
    if not isinstance(data, dict) or "next_followup_date" not in data or "message" not in data:
        raise ValueError(f"Unexpected playbook response shape: {raw[:200]}")
    data.setdefault("status_summary", "")
    data.setdefault("next_followup_reason", "")
    data.setdefault("scenarios", [])
    return data


def format_playbook_text(client_name: str, data: dict) -> str:
    """Plain-text rendering shared by the Telegram reply and the calendar
    event description — same structured layout used when this was first put
    together by hand in chat (status line, MESSAGE TO SEND, HOW TO REPLY
    sections)."""
    lines = [
        f"FOLLOW UP WITH: {client_name}",
        "",
        data.get("status_summary", "").strip(),
        "",
        "--- MESSAGE TO SEND ---",
        data.get("message", "").strip(),
        "",
        "--- HOW TO REPLY, DEPENDING ON WHAT THEY SAY ---",
    ]
    for scenario in data.get("scenarios", []):
        trigger = (scenario.get("trigger") or "").strip()
        guidance = (scenario.get("guidance") or "").strip()
        if not trigger and not guidance:
            continue
        lines.append("")
        lines.append(f"If {trigger[0].lower() + trigger[1:] if trigger else '...'}:")
        lines.append(guidance)
    return "\n".join(lines).strip()
