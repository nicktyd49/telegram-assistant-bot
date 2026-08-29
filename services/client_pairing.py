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
        return {"pending": {}, "confirmed": {}}
    try:
        store = json.loads(data)
    except json.JSONDecodeError:
        logger.exception("Pairing store is corrupt JSON - starting fresh (existing pairings are lost)")
        return {"pending": {}, "confirmed": {}}
    store.setdefault("pending", {})
    store.setdefault("confirmed", {})
    return store


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
