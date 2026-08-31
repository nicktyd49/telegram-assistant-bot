"""Shared policy-PDF extraction pipeline: Claude field extraction + workbook
fill + illustration/action-plan rebuild + PDF archive.

This logic originally lived only inside bot.py (Nic's own personal
assistant bot), used when Nic sends himself a policy PDF. client_bot.py's
auto-extraction-on-submission flow (a paired client sending a document
straight through Submit a Document) needs the exact same pipeline - the
same Claude prompt, the same workbook-write logic - so it's pulled out here
rather than re-implemented, unlike client_bot.py's usual small per-bot
helpers (see client_bot.py's own module docstring): getting this out of
sync between the two bots would mean the two bots could file the same kind
of PDF differently, which is a real correctness risk, not just some
duplicated boilerplate.

bot.py itself is NOT changed to use this module - it keeps its own
internal implementation untouched, so this refactor carries zero risk to
Nic's existing day-to-day flow through his own bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from config import settings
from prompts import POLICY_FIELDS_EXTRACTION_PROMPT
from services import action_plan, onedrive_service, pdf_utils, policy_illustration, policy_workbook

logger = logging.getLogger("assistant-bot.policy_extraction")

anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

MAX_POLICY_TEXT_CHARS = 15000


class ExtractionError(Exception):
    """Raised when a PDF has no readable text layer, or Claude's output
    couldn't be parsed as the expected JSON fields. Callers should treat
    this as "couldn't auto-process this one" and fall back gracefully -
    never as a reason to block the plain relay-to-Nic path that already
    happened before extraction is attempted."""


def _parse_json_block(raw_text: str) -> dict | None:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Could not parse JSON from model output: %s", raw_text[:300])
        return None


async def extract_policy_fields(pdf_bytes: bytes) -> dict:
    """Extracts the PDF's text and runs Claude's field-extraction prompt on
    it (the same prompt bot.py uses). Raises ExtractionError if the PDF has
    no readable text layer (e.g. a scanned image), or if Claude's output
    couldn't be parsed."""
    text = pdf_utils.extract_text(pdf_bytes)
    if not text:
        raise ExtractionError(
            "No readable text layer in this PDF - it looks like a scanned image rather than a "
            "text-based document."
        )

    truncated = text[:MAX_POLICY_TEXT_CHARS]
    response = await anthropic_client.messages.create(
        model=settings.extraction_model,
        max_tokens=768,
        system=POLICY_FIELDS_EXTRACTION_PROMPT,
        messages=[{"role": "user", "content": f"Policy document text:\n\n{truncated}"}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    fields = _parse_json_block(raw)
    if fields is None:
        raise ExtractionError("Could not extract structured fields from that document.")
    return fields


async def _archive_original_pdf(client_name: str, filename: str | None, pdf_bytes: bytes) -> None:
    if not settings.onedrive_configured:
        return
    stem_source = Path(filename).stem if filename else "policy"
    stem = re.sub(r"[^A-Za-z0-9]+", "_", stem_source).strip("_") or "policy"
    timestamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M%S")
    client_dir_name = policy_workbook._onedrive_safe_name(client_name, "Unknown_Client")
    dest_name = f"{stem}_{timestamp}.pdf"
    remote_path = f"{policy_workbook.ONEDRIVE_WORKBOOK_FOLDER}/{client_dir_name}/{dest_name}"
    await onedrive_service.upload_bytes(remote_path, pdf_bytes)
    logger.info("Archived original policy PDF to OneDrive: %s", remote_path)


async def save_extracted_policy(
    client_name: str,
    fields: dict,
    pdf_bytes: bytes | None = None,
    pdf_filename: str | None = None,
) -> tuple[Path, int, list]:
    """Writes the extracted fields into client_name's workbook (creating it
    if needed), rebuilds the illustration + action-plan sheets, and
    archives the original PDF to OneDrive. Returns (xlsx_path,
    policy_count, action_items) - mirrors bot.py's _finish_policy_summary,
    minus the Telegram-reply part, so each bot can send whatever reply
    makes sense in its own chat."""
    xlsx_path, policy_count, _gap_notes = await asyncio.to_thread(
        policy_workbook.add_policy_row, client_name, fields
    )

    try:
        await asyncio.to_thread(policy_illustration.rebuild_illustration_sheet, client_name)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to rebuild policy illustration sheet for %s", client_name)

    action_items: list = []
    try:
        action_items = await asyncio.to_thread(action_plan.rebuild_action_plan_sheet, client_name)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to rebuild action plan sheet for %s", client_name)

    if pdf_bytes is not None:
        try:
            await _archive_original_pdf(client_name, pdf_filename, pdf_bytes)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to archive original policy PDF for %s", client_name)

    return xlsx_path, policy_count, action_items
