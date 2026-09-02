"""One-time pairing codes linking a client's Telegram account to their
client_name, so the client-facing bot (client_bot.py) knows who's asking
before it will show anyone a Policy Summary. Deliberately NOT NRIC-based —
Singapore's PDPC is banning NRIC (even partial) as an authentication factor
for private orgs by 31 Dec 2026, and this needs to still be valid then.

Nic generates a code for a client with /client_code <name> (in bot.py, his
own personal bot). The client redeems it once in a private chat with the
client-facing bot; after that, their Telegram user id is permanently linked
to that client_name and they never need to re-enter anything.

Storage: a single JSON blob on OneDrive (services/onedrive_service.py),
not local disk — Railway wipes local disk on every redeploy, and a lost
pairing would silently lock every already-paired client out until they got
a fresh code from Nic.
"""
from __future__ import annotations

import json
import logging
import random
import string
from datetime import datetime, timedelta, timezone

from services import onedrive_service

logger = logging.getLogger("assistant-bot.client_pairing")

# Sibling to policy_workbook.py's CLIENT_EXPORT_TEMP_FOLDER convention - a
# top-level path outside Client/ so it never shows up as a fake client
# folder in policy_workbook.list_client_names().
PAIRING_STORE_PATH = "_bot_tmp/pairings.json"

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I - easy to read aloud
CODE_LENGTH = 6
CODE_TTL = timedelta(hours=48)


class PairingError(Exception):
    """Raised for an invalid, expired, or already-used pairing code."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _load() -> dict:
    data = await onedrive_service.download_bytes(PAIRING_STORE_PATH)
    if data is None:
        return _empty_store()
    try:
        store = json.loads(data)
    except json.JSONDecodeError:
        logger.exception("Pairing store is corrupt JSON - starting fresh (existing pairings are lost)")
        return _empty_store()
    store.setdefault("pending", {})
    store.setdefault("confirmed", {})
    store.setdefault("awaiting_name", {})
    return store


def _empty_store() -> dict:
    # Single source of truth for a brand-new/blank store's shape - the two
    # early-return branches above used to hand back {"pending": {},
    # "confirmed": {}} without "awaiting_name", which is exactly what threw
    # KeyError: 'awaiting_name' the moment mark_awaiting_name/get_awaiting_name/
    # clear_awaiting_name ran against a freshly-created store (e.g. right
    # after OneDrive briefly 404'd on the file). Keep this in sync with the
    # setdefault() calls just above.
    return {"pending": {}, "confirmed": {}, "awaiting_name": {}}


async def _save(store: dict) -> None:
    await onedrive_service.upload_bytes(PAIRING_STORE_PATH, json.dumps(store, indent=2).encode("utf-8"))


def _generate_code() -> str:
    return "".join(random.choices(CODE_ALPHABET, k=CODE_LENGTH))


async def create_code(client_name: str) -> str:
    """Generates a fresh pairing code for client_name, valid for CODE_TTL.
    Any earlier unredeemed code(s) for the same client are dropped, so only
    the newest one Nic just handed out will work."""
    store = await _load()
    store["pending"] = {
        code: entry for code, entry in store["pending"].items()
        if entry.get("client_name") != client_name
    }
    code = _generate_code()
    while code in store["pending"]:  # astronomically unlikely, but be safe
        code = _generate_code()
    store["pending"][code] = {
        "client_name": client_name,
        "expires_at": (_now() + CODE_TTL).isoformat(),
    }
    await _save(store)
    return code


async def redeem_code(code: str, telegram_user_id: int) -> str:
    """Validates and consumes a pairing code, permanently linking
    telegram_user_id to the client it was issued for. Returns the
    client_name. Raises PairingError with a client-facing message if the
    code is unknown, expired, or malformed."""
    code = code.strip().upper()
    store = await _load()
    entry = store["pending"].get(code)
    if entry is None:
        raise PairingError("That code isn't valid — please double-check it or ask for a new one.")
    if datetime.fromisoformat(entry["expires_at"]) < _now():
        del store["pending"][code]
        await _save(store)
        raise PairingError("That code has expired — please ask for a new one.")

    client_name = entry["client_name"]
    del store["pending"][code]
    store["confirmed"][str(telegram_user_id)] = client_name
    await _save(store)
    return client_name


async def get_paired_client(telegram_user_id: int) -> str | None:
    """Returns the client_name this Telegram user is already linked to, or
    None if they haven't redeemed a pairing code yet."""
    store = await _load()
    return store["confirmed"].get(str(telegram_user_id))


async def get_telegram_id_for_client(client_name: str) -> int | None:
    """Reverse lookup: given a client_name, finds the Telegram user id
    they're paired to, if any. Used by the auto-extraction approval flow
    (client_bot.py) to know where to deliver the finished policy summary
    PDF. O(n) scan of the confirmed dict - fine at this scale (a handful of
    paired clients, not thousands)."""
    store = await _load()
    for uid, name in store["confirmed"].items():
        if name == client_name:
            return int(uid)
    return None


async def pair_directly(telegram_user_id: int, client_name: str) -> None:
    """Pairs telegram_user_id straight to client_name, with no code to
    redeem - used once a brand-new referred joiner (see mark_awaiting_name
    below) has told the client bot their full name."""
    store = await _load()
    store["confirmed"][str(telegram_user_id)] = client_name
    await _save(store)


async def mark_awaiting_name(telegram_user_id: int, referred_by: str | None) -> None:
    """Marks a Telegram user as a brand-new prospect who just joined the
    Wealth Circle channel via a named Invite Friends link, and needs to
    supply their full name before they can be auto-paired to a fresh
    client record. Picked up in client_bot.py's start()/handle_private_text()
    the next time this user messages the bot - Telegram doesn't allow a bot
    to DM someone who hasn't started a chat with it first, so this can't be
    prompted immediately at join time; it's a durable flag checked on their
    next message instead."""
    store = await _load()
    store["awaiting_name"][str(telegram_user_id)] = {
        "referred_by": referred_by,
        "marked_at": _now().isoformat(),
    }
    await _save(store)


async def get_awaiting_name(telegram_user_id: int) -> dict | None:
    """Returns the {referred_by, marked_at} entry if this Telegram user is
    mid-way through the auto-pair-a-referral flow, else None."""
    store = await _load()
    return store["awaiting_name"].get(str(telegram_user_id))


async def clear_awaiting_name(telegram_user_id: int) -> None:
    store = await _load()
    store["awaiting_name"].pop(str(telegram_user_id), None)
    await _save(store)
