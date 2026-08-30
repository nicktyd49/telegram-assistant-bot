"""Fetches a concise, agent-relevant LIFE insurance news digest using
Claude's built-in web search tool, so PA can send /news on demand without
needing a separate news API key or subscription - just the
ANTHROPIC_API_KEY that's already configured for the rest of the bot.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from config import settings

logger = logging.getLogger("assistant-bot.news_service")

anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

NEWS_SYSTEM_PROMPT = (
    "You are a research assistant for a Singapore-based LIFE insurance agent. When asked "
    "for news, search the web for genuinely recent, relevant developments (published within "
    "the last 1-2 weeks where possible) and write a short digest formatted for a Telegram "
    "message read on a phone: plain text only, no markdown headers, bold, or asterisks - "
    "just numbered items (1) 2) 3)...). Put a BLANK LINE between every numbered item so "
    "they don't run together - never put two items back to back with only a single line "
    "break. Each item should be 1-2 short sentences, not a wall of text. Focus specifically "
    "on LIFE insurance - not general/motor/property insurance - prioritised in this order: "
    "(a) Singapore MAS/PDPC/LIA (Life Insurance Association) regulatory changes affecting "
    "life insurance agents, (b) Singapore life insurer news - new product launches, bonus/"
    "dividend rate declarations, payout and claims statistics, market share moves, (c) "
    "broader regional or global life insurance industry news (e.g. Asia life insurance "
    "trends, InsurTech affecting life products) if nothing else notable turned up locally. "
    "Skip anything stale, speculative, or without a clear recent date, and skip general/"
    "motor/property/health-only insurance news unless it's directly relevant to a life "
    "insurance agent's practice. If you can't find anything genuinely new, say so plainly "
    "instead of padding with old material. Keep the whole digest under 200 words."
)

_ITEM_MARKER_RE = re.compile(r"(?<!\n)\n(?=\d+\)\s)")
_EXTRA_BLANKLINES_RE = re.compile(r"\n{3,}")


def _add_spacing(text: str) -> str:
    """Guarantees a blank line before every numbered item and collapses any
    run of 3+ newlines down to a single blank line - makes the message
    readable on a phone even if the model doesn't perfectly follow the
    spacing instruction in the prompt."""
    text = text.replace("\r\n", "\n")
    text = _ITEM_MARKER_RE.sub("\n\n", text)
    text = _EXTRA_BLANKLINES_RE.sub("\n\n", text)
    return text.strip()


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
                    f"Today is {today}. Give me today's life insurance news digest for a "
                    "Singapore life insurance agent."
                ),
            }
        ],
    )

    text_parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    digest = "\n".join(part.strip() for part in text_parts if part.strip()).strip()
    if not digest:
        raise RuntimeError("No digest text came back - the model may not have found anything usable.")

    digest = _add_spacing(digest)
    header = f"📰 Life Insurance News Update ({today})"
    return f"{header}\n\n{digest}"
