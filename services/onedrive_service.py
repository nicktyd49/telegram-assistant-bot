"""Microsoft OneDrive (Graph API) integration.

Personal OneDrive accounts have no service-account equivalent — unlike
Google's setup, there's no "share a folder with a robot email" option. So
this uses OAuth's "device code" flow instead: Nic runs /onedrive_setup once
in Telegram, the bot gives him a short URL + code, he signs in with his own
Microsoft account in a browser, and the resulting sign-in (a refresh token
wrapped in an MSAL token cache) gets saved as the ONEDRIVE_TOKEN_CACHE
Railway variable. From then on the bot silently refreshes its own access
tokens using that cache — no more sign-ins needed unless the refresh token
itself expires (Microsoft rotates these; if unused for a long stretch, or
occasionally regardless, it can lapse — the fix is just running
/onedrive_setup again).

Important: this flow *must* run on Railway. Neither Nic's local machine (via
the device bridge) nor the assistant's own cloud sandbox has network access
to login.microsoftonline.com / graph.microsoft.com — confirmed directly by
testing both. Railway is the only place in this whole setup with real
internet access to Microsoft's endpoints, which is why /onedrive_setup is a
bot command rather than a setup script run anywhere else.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import quote

import msal
import requests

from config import settings

logger = logging.getLogger("assistant-bot.onedrive")

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Files.ReadWrite"]
GRAPH_ROOT = "https://graph.microsoft.com/v1.0/me/drive/root:"
REQUEST_TIMEOUT = 60

_app: Optional[msal.PublicClientApplication] = None
_cache: Optional[msal.SerializableTokenCache] = None


class OneDriveNotConfigured(RuntimeError):
    pass


class OneDriveAuthExpired(RuntimeError):
    pass


def _get_app() -> msal.PublicClientApplication:
    global _app, _cache
    if _app is not None:
        return _app
    if not settings.onedrive_client_id:
        raise OneDriveNotConfigured(
            "OneDrive isn't set up yet — ONEDRIVE_CLIENT_ID needs to be added to Railway first."
        )
    _cache = msal.SerializableTokenCache()
    if settings.onedrive_token_cache:
        _cache.deserialize(settings.onedrive_token_cache)
    _app = msal.PublicClientApplication(
        settings.onedrive_client_id, authority=AUTHORITY, token_cache=_cache
    )
    return _app


def start_device_flow() -> dict:
    """Kicks off the device-code sign-in flow. Returns the flow dict (has
    'user_code', 'verification_uri', plus the 'device_code' complete_device_flow
    needs) — hang on to this dict and pass it straight to complete_device_flow."""
    app = _get_app()
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Could not start OneDrive sign-in: {flow.get('error_description', flow)}")
    return flow


def complete_device_flow(flow: dict) -> str:
    """Blocks (polling Microsoft every few seconds) until the user finishes
    signing in at the URL/code from start_device_flow, or the flow expires
    (~15 minutes). Returns the serialized token cache — save this as the
    ONEDRIVE_TOKEN_CACHE Railway variable."""
    app = _get_app()
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"OneDrive sign-in failed: {result.get('error_description', result)}")
    return _cache.serialize()


def _access_token_sync() -> str:
    app = _get_app()
    accounts = app.get_accounts()
    if not accounts:
        raise OneDriveAuthExpired("OneDrive isn't signed in yet — run /onedrive_setup.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise OneDriveAuthExpired(
            "OneDrive's sign-in has expired — run /onedrive_setup again to reconnect."
        )
    return result["access_token"]


def _graph_headers() -> dict:
    return {"Authorization": f"Bearer {_access_token_sync()}"}


def _upload_bytes_sync(remote_path: str, data: bytes) -> None:
    """remote_path is relative to the OneDrive root, e.g.
    'Client/John Tan/Policy summary John Tan.xlsx'. Parent folders are
    created automatically by Graph if they don't exist yet. URL-encoded
    below since real client/file names can contain spaces and other
    characters that aren't safe to drop straight into a URL."""
    url = f"{GRAPH_ROOT}/{quote(remote_path, safe='/')}:/content"
    resp = requests.put(url, headers=_graph_headers(), data=data, timeout=REQUEST_TIMEOUT)
    if resp.status_code >= 300:
        raise RuntimeError(f"OneDrive upload failed ({resp.status_code}): {resp.text[:300]}")


def _download_bytes_sync(remote_path: str) -> Optional[bytes]:
    """Returns None (not an error) if the file doesn't exist on OneDrive yet
    — the normal case for a brand-new client."""
    url = f"{GRAPH_ROOT}/{quote(remote_path, safe='/')}:/content"
    resp = requests.get(url, headers=_graph_headers(), timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 300:
        raise RuntimeError(f"OneDrive download failed ({resp.status_code}): {resp.text[:300]}")
    return resp.content


def _list_children_sync(remote_folder: str) -> list[dict]:
    # Lists the immediate children (files and subfolders) of remote_folder,
    # e.g. 'Client'. Returns [] if the folder does not exist yet - nothing
    # filed there so far, not an error.
    url = f"{GRAPH_ROOT}/{quote(remote_folder, safe='/')}:/children"
    resp = requests.get(url, headers=_graph_headers(), timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return []
    if resp.status_code >= 300:
        raise RuntimeError(f"OneDrive listing failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json().get("value", [])


async def list_children(remote_folder: str) -> list[dict]:
    return await asyncio.to_thread(_list_children_sync, remote_folder)


async def upload_bytes(remote_path: str, data: bytes) -> None:
    await asyncio.to_thread(_upload_bytes_sync, remote_path, data)


async def download_bytes(remote_path: str) -> Optional[bytes]:
    return await asyncio.to_thread(_download_bytes_sync, remote_path)
