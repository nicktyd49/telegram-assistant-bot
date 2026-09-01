"""Turns a photo (plus optional notes) from a /igpost session into an
Instagram-ready square image and caption.

Editing is deliberately simple and deterministic - center-crop to a square,
resize, autocontrast, a light color/contrast/sharpness lift - all done with
Pillow, the same toolkit poster_service already uses for the Market Poster
feature. No AI photo retouching (blemish removal, background swaps, etc.) -
that would need a separate paid image-editing API this bot doesn't have.

The caption is the one Claude call here, in the same one-shot-vision style
as poster_service.extract_poster_content and the receipt/policy extraction
prompts elsewhere in this bot.
"""
from __future__ import annotations

import base64
import io
import logging

from PIL import Image, ImageEnhance, ImageOps

from config import settings

logger = logging.getLogger("assistant-bot.ig_post_service")

# Instagram's classic square feed format - the safest universal default
# since it also displays fine in most other placements.
IG_SIZE = 1080

IG_CAPTION_PROMPT = """You write Instagram captions for a Singapore life insurance and financial \
advisory agent, in their own casual-but-professional voice - not corporate marketing copy.

Given a photo and the agent's notes about it (notes may be empty - work from the photo alone \
if so), write ONE Instagram caption:

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


async def generate_caption(image_bytes: bytes, notes: str) -> str:
    """One Claude vision call - same pattern as poster_service.extract_poster_content."""
    from assistant import anthropic_client  # local import avoids a cycle at module load

    notes = (notes or "").strip()
    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode("ascii")},
        },
        {
            "type": "text",
            "text": f"Notes from the agent about this post: {notes}" if notes
            else "No extra notes were given - work from the photo alone.",
        },
    ]
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
