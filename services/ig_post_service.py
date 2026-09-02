"""Turns a photo (plus optional notes) from a /igpost session into an
Instagram-ready square image and caption.

Editing is deliberately simple and deterministic - center-crop to a square,
resize, autocontrast, a light color/contrast/sharpness lift - all done with
Pillow, the same toolkit poster_service already uses for the Market Poster
feature. No AI photo retouching (blemish removal, background swaps, etc.) -
that would need a separate paid image-editing API this bot doesn't have.

When more than 5 photos are sent in a session, a separate Claude vision call
(select_carousel_photos) narrows them down to the best 5-10 for an Instagram
carousel post before editing/captioning; 5 or fewer are used as-is. Either
way, editing and captioning happen the same one-shot-vision style as
poster_service.extract_poster_content and the receipt/policy extraction
prompts elsewhere in this bot - captioning covers the whole selected set in
a single call so one caption fits the carousel as a whole.
"""
from __future__ import annotations

import base64
import io
import json
import logging

from PIL import Image, ImageEnhance, ImageOps

from config import settings

logger = logging.getLogger("assistant-bot.ig_post_service")

# Instagram's classic square feed format - the safest universal default
# since it also displays fine in most other placements.
IG_SIZE = 1080

IG_CAPTION_PROMPT = """You write Instagram captions for a Singapore life insurance and financial \
advisory agent, in their own casual-but-professional voice - not corporate marketing copy.

Given one photo, or a set of photos meant as an Instagram carousel, plus the agent's notes \
(notes may be empty - work from the photo(s) alone if so), write ONE Instagram caption that \
covers the whole post - if it's a carousel, write for the set as a whole rather than \
describing each photo in turn:

- Open with a short, scroll-stopping hook line (not a generic greeting).
- 2-4 short lines/paragraphs of body copy - conversational, plain language, no jargon.
- End with a light call-to-action (e.g. inviting a DM or comment) where it fits naturally -
  skip it if the post is personal/lifestyle rather than professional.
- Finish with 5-10 relevant hashtags on their own line (mix of broad and niche - e.g.
  #SingaporeInsurance style tags alongside broader ones).
- Plain text only - Instagram doesn't render markdown, so no asterisks, no headers, no tables.
- If the notes or photo touch specific products, returns, or guarantees, keep language general
  and compliant - no promised returns, no "guaranteed" claims, nothing that reads as personalized
  financial advice to whoever's scrolling. Stay factual and let the CTA invite a real
  conversation instead.

Output ONLY the caption text - no preamble, no explanation, no quotes around it."""


SELECT_CAROUSEL_PROMPT = """You are choosing which of several candidate photos an \
insurance/financial advisor sent are best suited for an Instagram carousel post (multiple \
photos swiped through under one caption).

Pick between 5 and 10 photos - as many as are genuinely good, but never fewer than 5 unless \
there simply aren't 5 decent candidates, and never more than 10 (Instagram's own carousel \
limit). Judge by ordinary Instagram-post criteria: sharp focus, good lighting/exposure, \
flattering framing and composition, a clean/professional-looking background, and general \
social-media appeal. Drop near-duplicate or redundant shots (e.g. two almost-identical angles \
of the same moment) in favor of variety. Ignore file order when judging - judge each photo on \
its own merits - but return your picks in a sensible viewing order for a carousel (strongest \
photo first).

You will be shown each photo labeled "Photo 1", "Photo 2", etc., in that order. Reply with \
STRICT JSON only - no markdown, no commentary outside the JSON:

{"indices": [<1-based ints of the chosen photos, in the order they should appear>], \
"reason": "<one short sentence on the overall picks - e.g. what got dropped and why>"}"""


def _even_sample(n: int, k: int) -> list[int]:
    """k indices spread evenly across range(n) (always including the first and last),
    used as select_carousel_photos' fallback so a failed AI pick still gives a
    representative spread across everything sent instead of just the first few."""
    if n <= k:
        return list(range(n))
    if k <= 1:
        return [0]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


async def select_carousel_photos(image_parts: list[dict]) -> tuple[list[int], str]:
    """Given more than 5 candidate photos (each a {"media_type", "data"} dict,
    base64-encoded - the same shape used in a session's collected parts), asks Claude to
    pick 5-10 of them for an Instagram carousel post. Returns (0-based indices into
    image_parts, in carousel order, a short reason). Falls back to an even spread across
    everything sent (not just the first few - see _even_sample) with no reason if
    Claude's answer can't be parsed, or if parsing succeeds but yields nothing usable -
    never raises, since a failed pick shouldn't block the post.
    """
    from assistant import anthropic_client  # local import avoids a cycle at module load

    content: list[dict] = []
    for i, part in enumerate(image_parts, start=1):
        content.append({"type": "text", "text": f"Photo {i}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": part["media_type"], "data": part["data"]},
        })
    content.append({"type": "text", "text": "Which photos should make the carousel? Answer with the JSON only."})

    fallback = _even_sample(len(image_parts), min(10, len(image_parts)))
    # Generous headroom: ranking/deduping a big batch (many photos sent at once) needs
    # more room to reason through than a 1-of-N pick did, and a too-tight cap here was
    # observed to cut the response off before any JSON came out at all (empty response
    # text, not just malformed JSON) - which silently fell back to the first few photos.
    response = await anthropic_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=1536,
        system=SELECT_CAROUSEL_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    cleaned = raw.strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].lstrip()
    try:
        if not cleaned:
            raise ValueError("empty response text")
        parsed = json.loads(cleaned)
        indices = [int(i) - 1 for i in parsed["indices"]]
        reason = str(parsed.get("reason", "")).strip()
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        logger.warning(
            "select_carousel_photos: couldn't parse Claude's picks (stop_reason=%s, %d content block(s)), "
            "defaulting to an even spread across all %d sent | raw=%s",
            getattr(response, "stop_reason", "?"), len(response.content), len(image_parts), raw[:300],
        )
        return fallback, ""

    seen: set[int] = set()
    deduped: list[int] = []
    for i in indices:
        if 0 <= i < len(image_parts) and i not in seen:
            seen.add(i)
            deduped.append(i)
    if not deduped:
        return fallback, reason
    return deduped[:10], reason


def edit_photo(image_bytes: bytes) -> bytes:
    """Center-crops to a square, resizes to IG_SIZE, and applies a light,
    fixed enhance (autocontrast + a modest color/contrast/sharpness lift).
    Returns JPEG bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)  # respect the phone camera's orientation tag
    img = img.convert("RGB")

    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((IG_SIZE, IG_SIZE), Image.LANCZOS)

    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Color(img).enhance(1.12)
    img = ImageEnhance.Contrast(img).enhance(1.06)
    img = ImageEnhance.Sharpness(img).enhance(1.15)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


REVISE_CAPTION_PROMPT = """You previously wrote an Instagram caption for this post. The agent \
now wants a specific change made to it. Revise the caption to satisfy their request while \
keeping everything else about it intact - same overall structure (a hook line, short body \
paragraphs, an optional call-to-action, 5-10 hashtags on their own line), same facts, same \
voice - unless the requested change itself calls for altering one of those.

Output ONLY the revised caption text - no preamble, no explanation, no quotes around it."""


async def revise_caption(caption: str, feedback: str, image_bytes_list: list[bytes], notes: str) -> str:
    """One Claude vision call that edits an EXISTING caption per the agent's specific
    feedback (e.g. "make it shorter", "less salesy", "mention the venue name") rather than
    rerolling a brand new one from scratch the way generate_caption/🔁 Regenerate does."""
    from assistant import anthropic_client  # local import avoids a cycle at module load

    notes = (notes or "").strip()
    content: list[dict] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(b).decode("ascii")},
        }
        for b in image_bytes_list
    ]
    notes_line = f"Original notes about this post: {notes}\n\n" if notes else ""
    content.append({
        "type": "text",
        "text": f"{notes_line}Current caption:\n{caption}\n\nRequested change: {feedback}",
    })
    response = await anthropic_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=512,
        system=REVISE_CAPTION_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    revised = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if not revised:
        raise RuntimeError("Claude returned an empty caption - try again.")
    return revised


async def generate_caption(image_bytes_list: list[bytes], notes: str) -> str:
    """One Claude vision call covering every photo in the post (a single photo, or the
    whole selected carousel set) - same pattern as poster_service.extract_poster_content."""
    from assistant import anthropic_client  # local import avoids a cycle at module load

    notes = (notes or "").strip()
    content: list[dict] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(b).decode("ascii")},
        }
        for b in image_bytes_list
    ]
    content.append({
        "type": "text",
        "text": f"Notes from the agent about this post: {notes}" if notes
        else "No extra notes were given - work from the photo(s) alone.",
    })
    response = await anthropic_client.messages.create(
        model=settings.anthropic_model,
        max_tokens=512,
        system=IG_CAPTION_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    caption = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if not caption:
        raise RuntimeError("Claude returned an empty caption - try again.")
    return caption
