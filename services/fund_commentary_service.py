"""Drafts the ACTION / Market Update / Outlook sections of an ILP Funds
Update report from the fund performance numbers already in hand - same
one-shot Claude-call pattern as services/ig_post_service.py's caption
generation, just text-in/text-out instead of vision.

This has no live market data (no web search, no news feed) - it only ever
sees the numbers it's handed (allocations, gain/loss since last review,
overall account growth), so the prompt deliberately steers it away from
inventing specific market events, headlines, or statistics it has no way
to verify. What comes back is a first draft Nic reviews/edits in chat
before it's written into the report, never written in silently.
"""
from __future__ import annotations

import json
import logging

from config import settings

logger = logging.getLogger("assistant-bot.fund_commentary_service")

DRAFT_COMMENTARY_PROMPT = """You help a Singapore HSBC Life wealth advisor draft short \
client-facing commentary for an "ILP Funds Update" report. You are given only the \
client's policy and fund performance numbers - no live market data or news - so ground \
everything in those numbers and general, non-specific portfolio principles (staying \
diversified, reviewing periodically, not reacting to short-term noise) rather than \
inventing specific market events, headlines, or statistics you cannot verify.

Return JSON only (no markdown code fences, no commentary before or after) with exactly \
these three keys:

"action_notes": one or two sentences of continuous prose (not bullet points) summarizing \
how the portfolio is doing overall and a light-touch next step (e.g. stay the course, \
consider a top-up, review an underperforming fund) - becomes the advisor's dated action \
note.

"market_update": 3-4 short standalone lines separated by \\n (never one wrapped \
paragraph) describing what the numbers show for this portfolio specifically - which \
funds/regions are leading or lagging and by roughly how much - without asserting \
specific external market facts you were not given.

"outlook": 3-4 short standalone lines separated by \\n (never one wrapped paragraph), \
forward-looking but appropriately hedged (no guaranteed returns, no specific \
predictions), tied to this portfolio's current allocation and performance.

Keep the tone professional and client-appropriate throughout - no hype, no promises, no \
unverified claims. Output ONLY the JSON object, nothing else."""


class CommentaryDraftError(RuntimeError):
    """Raised when Claude's draft couldn't be obtained or parsed - the caller falls back
    to asking Nic to type these sections manually rather than blocking the report."""


def _fund_summary_lines(funds: list[dict]) -> str:
    lines = []
    for f in funds:
        change = f.get("change_pct")
        change_str = f"{change:+.0%}" if change is not None else "n/a"
        lines.append(f"- {f['name']}: {f['allocation_pct']:.0f}% allocation, {change_str} since last review")
    return "\n".join(lines)


async def draft_commentary(
    *,
    product: str,
    total_invested: float,
    account_value: float,
    account_value_asof,
    funds: list[dict],
) -> dict:
    """funds: [{"name": str, "allocation_pct": float, "change_pct": float|None}, ...].
    Returns {"action_notes": str, "market_update": str, "outlook": str}. Raises
    CommentaryDraftError on any failure (empty/unparseable response, API error) - never
    returns a partial or guessed result."""
    from assistant import anthropic_client  # local import avoids a cycle at module load

    overall_change = (account_value - total_invested) / total_invested if total_invested else None
    overall_str = f"{overall_change:+.0%}" if overall_change is not None else "n/a"
    summary = (
        f"Product: {product}\n"
        f"Total invested: ${total_invested:,.0f}\n"
        f"Current account value: ${account_value:,.0f} as of {account_value_asof:%d/%m/%Y} "
        f"({overall_str} overall since commencement)\n"
        f"Funds:\n{_fund_summary_lines(funds)}"
    )

    try:
        response = await anthropic_client.messages.create(
            model=settings.anthropic_model,
            max_tokens=768,
            system=DRAFT_COMMENTARY_PROMPT,
            messages=[{"role": "user", "content": summary}],
        )
    except Exception as exc:  # noqa: BLE001 - any API failure falls back to manual entry
        raise CommentaryDraftError(f"Claude call failed: {exc}") from exc

    raw = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    cleaned = raw.strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].lstrip()
    if not cleaned:
        raise CommentaryDraftError("Claude returned an empty response")

    try:
        parsed = json.loads(cleaned)
        result = {
            "action_notes": str(parsed["action_notes"]).strip(),
            "market_update": str(parsed["market_update"]).strip(),
            "outlook": str(parsed["outlook"]).strip(),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("draft_commentary: couldn't parse Claude's response | raw=%s", raw[:500])
        raise CommentaryDraftError(f"Couldn't parse Claude's response: {exc}") from exc

    if not all(result.values()):
        raise CommentaryDraftError("Claude returned one or more empty sections")
    return result
