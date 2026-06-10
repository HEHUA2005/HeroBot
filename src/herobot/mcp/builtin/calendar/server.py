from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from herobot.mcp.builtin.calendar.storage import CalendarStorage
from herobot.mcp.builtin.calendar.tools import CalendarTools
from herobot.mcp.context import HIDDEN_CONTEXT_KEY, ToolContext, context_from_payload


mcp = FastMCP(
    "herobot-calendar-tools",
    instructions=(
        "Calendar, contacts, reminders, availability, and scheduling tools for HeroBot. "
        "Does not access Telegram directly."
    ),
)

_tools: CalendarTools | None = None
_initialized = False


async def calendar_tools() -> CalendarTools:
    global _tools, _initialized
    if _tools is None:
        timezone_name = os.getenv("HEROBOT_DEFAULT_TIMEZONE", "Asia/Shanghai")
        db_path = (
            os.getenv("HEROBOT_CALENDAR_DB_PATH")
            or os.getenv("HEROBOT_DB_PATH")
            or "data/herobot-calendar.sqlite3"
        )
        _tools = CalendarTools(CalendarStorage(db_path), timezone_name=timezone_name)
    if not _initialized:
        await _tools.storage.init()
        _initialized = True
    return _tools


def context(payload: dict[str, Any] | None) -> ToolContext:
    return context_from_payload(payload or {}, os.getenv("HEROBOT_DEFAULT_TIMEZONE", "Asia/Shanghai"))


async def invoke(
    name: str, arguments: dict[str, Any], herobot_context: dict[str, Any] | None
) -> dict[str, Any]:
    tools = await calendar_tools()
    try:
        tool_context = context({HIDDEN_CONTEXT_KEY: herobot_context or {}})
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return await tools.invoke(name, arguments, tool_context)


@mcp.tool(description="Create a Telegram reminder. remind_at must be ISO 8601 with timezone.")
async def create_reminder(
    content: str,
    remind_at: str,
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await invoke(
        "create_reminder",
        {"content": content, "remind_at": remind_at},
        herobot_context,
    )


@mcp.tool(description="List upcoming reminders.")
async def list_reminders(
    include_sent: bool = False, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("list_reminders", {"include_sent": include_sent}, herobot_context)


@mcp.tool(description="List due reminders for the runtime scheduler. Hidden from the LLM.")
async def list_due_reminders(
    now_iso: str, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("list_due_reminders", {"now_iso": now_iso}, herobot_context)


@mcp.tool(description="Mark a reminder as sent for the runtime scheduler. Hidden from the LLM.")
async def mark_reminder_sent(
    reminder_id: int, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("mark_reminder_sent", {"reminder_id": reminder_id}, herobot_context)


@mcp.tool(description="Get the current date and time.")
async def get_current_time(herobot_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return await invoke("get_current_time", {}, herobot_context)


@mcp.tool(description="Add or update a contact with their assistant bot username.")
async def add_contact(
    name: str,
    bot_username: str,
    note: str = "",
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await invoke(
        "add_contact",
        {"name": name, "bot_username": bot_username, "note": note},
        herobot_context,
    )


@mcp.tool(description="List all saved contacts.")
async def list_contacts(herobot_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return await invoke("list_contacts", {}, herobot_context)


@mcp.tool(description="Delete a contact by name.")
async def delete_contact(
    name: str, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("delete_contact", {"name": name}, herobot_context)


@mcp.tool(description="Create a calendar event. Times must be ISO 8601 with timezone.")
async def create_calendar_event(
    title: str,
    start_at: str,
    end_at: str,
    note: str = "",
    reminder_minutes_before: int = 0,
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await invoke(
        "create_calendar_event",
        {
            "title": title,
            "start_at": start_at,
            "end_at": end_at,
            "note": note,
            "reminder_minutes_before": reminder_minutes_before,
        },
        herobot_context,
    )


@mcp.tool(description="List calendar events in a time range. Times must be ISO 8601 with timezone.")
async def list_calendar_events(
    start_at: str, end_at: str, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke(
        "list_calendar_events",
        {"start_at": start_at, "end_at": end_at},
        herobot_context,
    )


@mcp.tool(description="Find free calendar slots in a range. Only returns free times.")
async def find_availability(
    start_at: str,
    end_at: str,
    duration_minutes: int = 30,
    limit: int = 6,
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await invoke(
        "find_availability",
        {
            "start_at": start_at,
            "end_at": end_at,
            "duration_minutes": duration_minutes,
            "limit": limit,
        },
        herobot_context,
    )


@mcp.tool(description="Create a pending scheduling request with a saved contact.")
async def start_scheduling_request(
    contact_name: str,
    request_text: str,
    duration_minutes: int,
    window_start: str,
    window_end: str,
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await invoke(
        "start_scheduling_request",
        {
            "contact_name": contact_name,
            "request_text": request_text,
            "duration_minutes": duration_minutes,
            "window_start": window_start,
            "window_end": window_end,
        },
        herobot_context,
    )


@mcp.tool(description="Get a scheduling session. If session_id is omitted, get latest pending one.")
async def get_scheduling_session(
    session_id: int | None = None, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("get_scheduling_session", {"session_id": session_id}, herobot_context)


@mcp.tool(
    description=(
        "Update scheduling candidates. Provide peer_windows to compute common free windows, "
        "or candidates to store known candidate windows."
    )
)
async def update_scheduling_candidates(
    session_id: int | None = None,
    peer_windows: list[dict[str, str]] | None = None,
    candidates: list[dict[str, str]] | None = None,
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await invoke(
        "update_scheduling_candidates",
        {
            "session_id": session_id,
            "peer_windows": peer_windows or [],
            "candidates": candidates or [],
        },
        herobot_context,
    )


@mcp.tool(description="Confirm one scheduling candidate and create the calendar event.")
async def confirm_scheduling_candidate(
    session_id: int | None = None,
    candidate_index: int = 1,
    title: str | None = None,
    reminder_minutes_before: int | None = None,
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await invoke(
        "confirm_scheduling_candidate",
        {
            "session_id": session_id,
            "candidate_index": candidate_index,
            "title": title,
            "reminder_minutes_before": reminder_minutes_before,
        },
        herobot_context,
    )


@mcp.tool(description="Cancel a pending scheduling request.")
async def cancel_scheduling_request(
    session_id: int | None = None, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("cancel_scheduling_request", {"session_id": session_id}, herobot_context)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
