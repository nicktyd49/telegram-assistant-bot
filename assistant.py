"""Claude conversation loop with tool use for scheduling + receipt logging.

This is the brain behind free-text chat messages. PDF/photo handling for
policy summaries and photo receipts live in bot.py since those are one-shot
document-in, reply-out flows rather than back-and-forth chat.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from config import settings
from services import calendar_service, sheets_service

logger = logging.getLogger("assistant-bot.assistant")

anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

MAX_TOOL_ITERATIONS = 6


def _now_str() -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    return now.strftime("%A, %Y-%m-%d %H:%M (%Z)")


def build_system_prompt() -> str:
    capabilities = [
        "- Summarizing insurance policy documents in client-friendly language "
        "(the user sends a PDF directly in chat; you don't need a tool for that).",
        "- Drafting messages to clients and general day-to-day admin support.",
    ]
    if settings.calendar_configured:
        capabilities.append(
            "- Scheduling, viewing, and cancelling appointments on the user's real Google "
            "Calendar via the create_calendar_event / list_calendar_events / "
            "delete_calendar_event tools."
        )
    else:
        capabilities.append(
            "- Calendar scheduling is NOT configured yet — if asked to book/check/cancel "
            "an appointment, say so plainly and don't pretend to have done it."
        )
    if settings.sheets_configured:
        capabilities.append(
            "- Logging business expense receipts the user *describes in text* to a "
            "spreadsheet via the log_receipt tool. (Receipts sent as photos are handled "
            "automatically elsewhere — you won't see those as chat messages.)"
        )
    else:
        capabilities.append(
            "- Receipt logging is NOT configured yet — if asked to log an expense, say so "
            "plainly and don't pretend to have done it."
        )

    return f"""You are a personal assistant for an insurance agent/broker, reachable via Telegram.
Current date/time: {_now_str()} — timezone: {settings.timezone}.

You can help with:
{chr(10).join(capabilities)}

Be concise and practical — this is a chat interface, not a document editor. When you use a
tool, report back to the user in plain, friendly language (never raw JSON or IDs, except you
may mention an event time/title to confirm). If a tool call fails, tell the user what went
wrong in one sentence rather than a stack trace. Resolve relative dates ("tomorrow", "next
Tuesday") against the current date/time above and always pass full ISO 8601 datetimes with
the {settings.timezone} UTC offset to calendar tools."""


def _build_tools() -> list[dict]:
    tools = []
    if settings.calendar_configured:
        tools.extend(
            [
                {
                    "name": "create_calendar_event",
                    "description": (
                        "Create an appointment/meeting on the user's Google Calendar. Use "
                        "whenever the user asks to schedule, book, or add something to their "
                        "calendar. Default the end time to 1 hour after the start if the user "
                        "doesn't give a duration."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Short event title, e.g. 'Policy review with John Tan'."},
                            "start_time": {
                                "type": "string",
                                "description": f"ISO 8601 datetime with UTC offset, e.g. 2026-08-27T15:00:00{_offset_suffix()}",
                            },
                            "end_time": {
                                "type": "string",
                                "description": "ISO 8601 datetime with UTC offset.",
                            },
                            "description": {"type": "string", "description": "Optional notes/agenda for the event."},
                            "location": {"type": "string", "description": "Optional location (address, Zoom link, etc.)."},
                        },
                        "required": ["title", "start_time", "end_time"],
                    },
                },
                {
                    "name": "list_calendar_events",
                    "description": (
                        "List the user's calendar events between two dates (inclusive). Use "
                        "this to answer questions about their schedule, availability, or to "
                        "find an event's ID before cancelling it."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "start_date": {"type": "string", "description": "ISO date, e.g. 2026-08-25."},
                            "end_date": {"type": "string", "description": "ISO date, inclusive."},
                        },
                        "required": ["start_date", "end_date"],
                    },
                },
                {
                    "name": "delete_calendar_event",
                    "description": (
                        "Cancel/delete a calendar event. You must first call list_calendar_events "
                        "to find the correct event_id — never guess an ID."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "event_id": {"type": "string"},
                        },
                        "required": ["event_id"],
                    },
                },
            ]
        )
    if settings.sheets_configured:
        tools.append(
            {
                "name": "log_receipt",
                "description": (
                    "Log a business expense receipt to the tracking spreadsheet, when the "
                    "user describes an expense in text (e.g. 'log $45 lunch with a client "
                    "at Cedele today')."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "ISO date of the expense; default to today if unspecified."},
                        "vendor": {"type": "string"},
                        "amount": {"type": "number"},
                        "currency": {"type": "string", "description": f"3-letter currency code, default {settings.default_currency}."},
                        "category": {"type": "string", "description": "e.g. Meals, Transport, Office Supplies, Client Entertainment."},
                        "notes": {"type": "string"},
                    },
                    "required": ["date", "vendor", "amount"],
                },
            }
        )
    return tools


def _offset_suffix() -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    return now.strftime("%z")[:3] + ":" + now.strftime("%z")[3:]


async def _dispatch_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """Runs a tool call. Returns (result_text, is_error)."""
    try:
        if name == "create_calendar_event":
            event = await calendar_service.create_event(
                title=tool_input["title"],
                start_time=tool_input["start_time"],
                end_time=tool_input["end_time"],
                description=tool_input.get("description"),
                location=tool_input.get("location"),
            )
            return (
                json.dumps(
                    {
                        "status": "created",
                        "event_id": event.get("id"),
                        "title": event.get("summary"),
                        "start": event.get("start"),
                        "end": event.get("end"),
                        "link": event.get("htmlLink"),
                    }
                ),
                False,
            )

        if name == "list_calendar_events":
            events = await calendar_service.list_events(tool_input["start_date"], tool_input["end_date"])
            summarized = [
                {
                    "event_id": e.get("id"),
                    "title": e.get("summary", "(no title)"),
                    "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date")),
                    "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date")),
                    "location": e.get("location"),
                }
                for e in events
            ]
            return json.dumps({"events": summarized}), False

        if name == "delete_calendar_event":
            await calendar_service.delete_event(tool_input["event_id"])
            return json.dumps({"status": "deleted", "event_id": tool_input["event_id"]}), False

        if name == "log_receipt":
            await sheets_service.append_receipt(
                date=tool_input["date"],
                vendor=tool_input["vendor"],
                amount=tool_input["amount"],
                currency=tool_input.get("currency") or settings.default_currency,
                category=tool_input.get("category"),
                notes=tool_input.get("notes"),
            )
            return json.dumps({"status": "logged"}), False

        return json.dumps({"error": f"Unknown tool '{name}'"}), True

    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
        logger.exception("Tool %s failed", name)
        return json.dumps({"error": str(exc)}), True


async def run_conversation(history: list[dict]) -> str:
    """Runs the tool-use loop against `history` (mutated in place) and
    returns the final assistant reply text."""
    tools = _build_tools()

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await anthropic_client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=build_system_prompt(),
            messages=history,
            tools=tools if tools else None,
        )

        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return "".join(block.text for block in response.content if block.type == "text").strip()

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result_text, is_error = await _dispatch_tool(block.name, block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                    "is_error": is_error,
                }
            )
        history.append({"role": "user", "content": tool_results})

    return "I went through a few tool calls but couldn't wrap this up cleanly — could you rephrase or try again?"
