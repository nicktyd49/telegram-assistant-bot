"""Thin wrapper around the Google Calendar API using a service account.

Setup note (see README): the target calendar must be *shared* with the
service account's email (found in the JSON key as "client_email"), with
"Make changes to events" permission — otherwise every call here will fail
with a 403.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings

logger = logging.getLogger("assistant-bot.calendar")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None


class CalendarNotConfigured(RuntimeError):
    pass


def _get_service():
    global _service
    if _service is not None:
        return _service
    if not settings.calendar_configured:
        raise CalendarNotConfigured(
            "Google Calendar isn't set up yet — GOOGLE_SERVICE_ACCOUNT_JSON and "
            "GOOGLE_CALENDAR_ID need to be configured (see README)."
        )
    creds = service_account.Credentials.from_service_account_info(
        settings.google_service_account_info, scopes=SCOPES
    )
    _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def _create_event_sync(title: str, start_time: str, end_time: str,
                        description: Optional[str], location: Optional[str]) -> dict:
    service = _get_service()
    body = {
        "summary": title,
        "start": {"dateTime": start_time, "timeZone": settings.timezone},
        "end": {"dateTime": end_time, "timeZone": settings.timezone},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    event = service.events().insert(calendarId=settings.google_calendar_id, body=body).execute()
    return event


def _list_events_sync(start_date: str, end_date: str) -> list[dict]:
    service = _get_service()
    time_min = f"{start_date}T00:00:00"
    time_max = f"{end_date}T23:59:59"
    result = (
        service.events()
        .list(
            calendarId=settings.google_calendar_id,
            timeMin=_to_rfc3339(time_min),
            timeMax=_to_rfc3339(time_max),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return result.get("items", [])


def _delete_event_sync(event_id: str) -> None:
    service = _get_service()
    service.events().delete(calendarId=settings.google_calendar_id, eventId=event_id).execute()


def _list_updated_events_sync(updated_min_iso: str) -> list[dict]:
    """Events created or changed since updated_min_iso (an RFC3339 UTC
    timestamp), regardless of when they occur. Used to poll for new
    invites — showDeleted is off since a cancelled invite isn't something
    worth notifying about."""
    service = _get_service()
    result = (
        service.events()
        .list(
            calendarId=settings.google_calendar_id,
            updatedMin=updated_min_iso,
            singleEvents=True,
            showDeleted=False,
        )
        .execute()
    )
    return result.get("items", [])


def _to_rfc3339(naive_local_iso: str) -> str:
    # timeMin/timeMax need a timezone; googleapiclient accepts an offset-less
    # string as long as we pass timeZone separately via the API params, but
    # the list() call above doesn't take a top-level timeZone — so we encode
    # it directly using the configured IANA zone via zoneinfo.
    from zoneinfo import ZoneInfo
    from datetime import datetime

    dt = datetime.fromisoformat(naive_local_iso).replace(tzinfo=ZoneInfo(settings.timezone))
    return dt.isoformat()


async def create_event(title: str, start_time: str, end_time: str,
                        description: Optional[str] = None, location: Optional[str] = None) -> dict:
    try:
        return await asyncio.to_thread(_create_event_sync, title, start_time, end_time, description, location)
    except HttpError as exc:
        logger.exception("Calendar create_event failed")
        raise RuntimeError(f"Google Calendar rejected the request: {exc.reason}") from exc


async def list_events(start_date: str, end_date: str) -> list[dict]:
    try:
        return await asyncio.to_thread(_list_events_sync, start_date, end_date)
    except HttpError as exc:
        logger.exception("Calendar list_events failed")
        raise RuntimeError(f"Google Calendar rejected the request: {exc.reason}") from exc


async def delete_event(event_id: str) -> None:
    try:
        await asyncio.to_thread(_delete_event_sync, event_id)
    except HttpError as exc:
        logger.exception("Calendar delete_event failed")
        raise RuntimeError(f"Google Calendar rejected the request: {exc.reason}") from exc


async def list_updated_events(updated_min_iso: str) -> list[dict]:
    try:
        return await asyncio.to_thread(_list_updated_events_sync, updated_min_iso)
    except HttpError as exc:
        logger.exception("Calendar list_updated_events failed")
        raise RuntimeError(f"Google Calendar rejected the request: {exc.reason}") from exc
