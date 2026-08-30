"""Fetches a concise, agent-relevant insurance news digest using Claude's
built-in web search tool, so PA can send /news on demand without needing a
separate news API key or subscription - just the ANTHROPIC_API_KEY that's
already configured for the rest of the bot.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from config import settings

logger = logging.getLogger("assistant-bot.news_service")

anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

NEWS_SYSTEM_PROMPT = (
    "You are a research assistant for a Singapore-based insurance agent. When asked for "
    "news, search the web for genuinely recent, relevant developments (published within the "
    "last 1-2 weeks where possible) and write a short digest formatted for a Telegram "
    "message: plain text only, no markdown headers, bold, or asterisks - just numbered "
    "items (1) 2) 3)...), each item 1-3 sentences. Prioritise, in order: (a) Singapore "
    "MAS/PDPC regulatory changes affecting insurance agents, (b) Singapore insurer/product/"
    "market news, (c) broader regional or global insurance industry news if nothing else "
    "notable turned up. Skip anything stale, speculative, or without a clear recent date. "
    "If you can't find anything genuinely new, say so plainly instead of padding with old "
    "material. Keep the whole digest under 200 words."
)


async def get_insurance_news_digest() -> str:
    """Returns a ready-to-send Telegram message string, or raises on failure
    (caller decides how to surface that to the user)."""
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).strftime("%A, %d %B %Y")

    response = await anthropic_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1024,
        system=NEWS_SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today is {today}. Give me today's insurance news digest for a "
                    "Singapore insurance agent."
                ),
            }
        ],
    )

    text_parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    digest = "\n".join(part.strip() for part in text_parts if part.strip()).strip()
    if not digest:
        raise RuntimeError("No digest text came back - the model may not have found anything usable.")

    return f"📰 Insurance News Update ({today})\n\n{digest}"
