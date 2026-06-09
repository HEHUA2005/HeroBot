from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from herobot.mcp.builtin.calendar.scheduling import (
    TimeWindow,
    find_free_windows,
    from_iso,
    intersect_windows,
    to_utc_iso,
)
from herobot.mcp.builtin.calendar.storage import CalendarStorage
from herobot.mcp.context import ToolContext


def ok_result(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def error_result(error: Exception | str) -> dict[str, Any]:
    return {"ok": False, "error": str(error)}


class CalendarTools:
    def __init__(self, storage: CalendarStorage, timezone_name: str = "Asia/Shanghai") -> None:
        self.storage = storage
        self.timezone = timezone_name

    async def invoke(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        try:
            result = await self._invoke(name, arguments, context)
            return ok_result(result)
        except Exception as exc:
            return error_result(exc)

    async def _invoke(self, name: str, arguments: dict[str, Any], context: ToolContext) -> Any:
        if name == "create_reminder":
            return await self.create_reminder(arguments, context)
        if name == "list_reminders":
            return await self.storage.list_reminders(
                context.chat_id, bool(arguments.get("include_sent", False))
            )
        if name == "list_due_reminders":
            reminders = await self.storage.due_reminders(arguments["now_iso"])
            return [reminder.__dict__ for reminder in reminders]
        if name == "mark_reminder_sent":
            return await self.storage.mark_reminder_sent(int(arguments["reminder_id"]))
        if name == "get_current_time":
            now = datetime.now(ZoneInfo(context.timezone or self.timezone))
            return {"iso": now.isoformat(), "display": now.strftime("%Y-%m-%d %H:%M:%S %Z")}
        if name == "add_contact":
            return await self.storage.upsert_contact(
                arguments["name"], arguments["bot_username"], arguments.get("note", "")
            )
        if name == "list_contacts":
            return await self.storage.list_contacts()
        if name == "delete_contact":
            return await self.storage.delete_contact(arguments["name"])
        if name == "create_calendar_event":
            return await self.create_calendar_event(arguments, context)
        if name == "list_calendar_events":
            return await self.list_calendar_events(arguments, context)
        if name == "find_availability":
            return await self.find_availability(arguments, context)
        if name == "start_scheduling_request":
            return await self.start_scheduling_request(arguments, context)
        if name == "get_scheduling_session":
            return await self.get_scheduling_session(arguments, context)
        if name == "update_scheduling_candidates":
            return await self.update_scheduling_candidates(arguments, context)
        if name == "confirm_scheduling_candidate":
            return await self.confirm_scheduling_candidate(arguments, context)
        if name == "cancel_scheduling_request":
            return await self.cancel_scheduling_request(arguments, context)
        raise ValueError(f"unknown calendar tool: {name}")

    async def create_reminder(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        remind_at = from_iso(arguments["remind_at"], context.timezone)
        return await self.storage.create_reminder(
            context.chat_id,
            context.user_id,
            arguments["content"].strip(),
            to_utc_iso(remind_at),
        )

    async def create_calendar_event(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        start = from_iso(arguments["start_at"], context.timezone)
        end = from_iso(arguments["end_at"], context.timezone)
        if end <= start:
            raise ValueError("end_at must be later than start_at")
        reminder_minutes = int(arguments.get("reminder_minutes_before", 0) or 0)
        reminder_at = None
        if reminder_minutes > 0:
            reminder_at = to_utc_iso(start - timedelta(minutes=reminder_minutes))
        event = await self.storage.create_calendar_event(
            context.effective_owner_user_id,
            arguments["title"].strip(),
            to_utc_iso(start),
            to_utc_iso(end),
            arguments.get("note", "").strip(),
            reminder_at,
        )
        if reminder_at:
            await self.storage.create_reminder(
                context.chat_id,
                context.user_id,
                f"日程提醒：{arguments['title'].strip()}",
                reminder_at,
            )
        return event

    async def list_calendar_events(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> list[dict[str, Any]]:
        start = from_iso(arguments["start_at"], context.timezone)
        end = from_iso(arguments["end_at"], context.timezone)
        return await self.storage.list_calendar_events(
            context.effective_owner_user_id,
            to_utc_iso(start),
            to_utc_iso(end),
        )

    async def find_availability(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> list[dict[str, str]]:
        start = from_iso(arguments["start_at"], context.timezone)
        end = from_iso(arguments["end_at"], context.timezone)
        events = await self.storage.list_calendar_events(
            context.effective_owner_user_id,
            to_utc_iso(start),
            to_utc_iso(end),
        )
        slots = find_free_windows(
            events,
            TimeWindow(start, end),
            int(arguments.get("duration_minutes", 30)),
        )
        return [
            {"start_at": to_utc_iso(slot.start), "end_at": to_utc_iso(slot.end)}
            for slot in slots
        ]

    async def start_scheduling_request(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        contact = await self.storage.get_contact(arguments["contact_name"])
        if contact is None:
            raise ValueError(f"contact not found: {arguments['contact_name']}")
        start = from_iso(arguments["window_start"], context.timezone)
        end = from_iso(arguments["window_end"], context.timezone)
        if end <= start:
            raise ValueError("window_end must be later than window_start")
        created = await self.storage.create_scheduling_session(
            context.chat_id,
            context.effective_owner_user_id,
            contact["name"],
            contact["bot_username"],
            arguments["request_text"].strip(),
            int(arguments["duration_minutes"]),
            to_utc_iso(start),
            to_utc_iso(end),
        )
        session = await self.storage.get_scheduling_session(int(created["id"]))
        return session or created

    async def get_scheduling_session(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any] | None:
        session_id = arguments.get("session_id")
        if session_id is not None:
            return await self.storage.get_scheduling_session(int(session_id))
        return await self.storage.latest_pending_session(context.effective_owner_user_id)

    async def update_scheduling_candidates(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        session = await self._session_from_arguments(arguments, context)
        if session is None:
            raise ValueError("scheduling session not found")

        if arguments.get("peer_windows"):
            peer_slots = [
                TimeWindow(
                    from_iso(item["start_at"], context.timezone),
                    from_iso(item["end_at"], context.timezone),
                )
                for item in arguments["peer_windows"]
            ]
            window = TimeWindow(
                from_iso(session["window_start"], context.timezone),
                from_iso(session["window_end"], context.timezone),
            )
            events = await self.storage.list_calendar_events(
                int(session["owner_user_id"]),
                to_utc_iso(window.start),
                to_utc_iso(window.end),
            )
            own_slots = find_free_windows(events, window, int(session["duration_minutes"]))
            candidate_slots = intersect_windows(
                own_slots,
                peer_slots,
                int(session["duration_minutes"]),
                limit=3,
            )
            candidates = [
                {"start_at": to_utc_iso(slot.start), "end_at": to_utc_iso(slot.end)}
                for slot in candidate_slots
            ]
        else:
            candidates = [
                {
                    "start_at": to_utc_iso(from_iso(item["start_at"], context.timezone)),
                    "end_at": to_utc_iso(from_iso(item["end_at"], context.timezone)),
                }
                for item in arguments.get("candidates", [])
            ]

        await self.storage.update_scheduling_candidates(int(session["id"]), candidates)
        return {
            "session_id": session["id"],
            "candidates": candidates,
            "status": "awaiting_confirmation",
        }

    async def confirm_scheduling_candidate(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        session = await self._session_from_arguments(arguments, context)
        if session is None or not session["candidates"]:
            raise ValueError("no scheduling candidates to confirm")
        candidate_index = int(arguments.get("candidate_index", 1))
        index = candidate_index - 1
        if index < 0 or index >= len(session["candidates"]):
            raise ValueError("candidate_index out of range")
        candidate = session["candidates"][index]
        title = arguments.get("title") or f"和 {session['contact_name']} 约时间"
        reminder_at = None
        reminder_minutes = arguments.get("reminder_minutes_before")
        if reminder_minutes is not None and int(reminder_minutes) > 0:
            reminder_at = to_utc_iso(
                from_iso(candidate["start_at"], context.timezone)
                - timedelta(minutes=int(reminder_minutes))
            )
        event = await self.storage.create_calendar_event(
            int(session["owner_user_id"]),
            title,
            candidate["start_at"],
            candidate["end_at"],
            session["request_text"],
            reminder_at,
        )
        if reminder_at:
            await self.storage.create_reminder(
                context.chat_id,
                context.user_id,
                f"日程提醒：{title}",
                reminder_at,
            )
        await self.storage.set_scheduling_status(int(session["id"]), "confirmed")
        return {"session_id": session["id"], "event": event}

    async def cancel_scheduling_request(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        session = await self._session_from_arguments(arguments, context)
        if session is None:
            raise ValueError("scheduling session not found")
        await self.storage.set_scheduling_status(int(session["id"]), "cancelled")
        return {"session_id": session["id"], "status": "cancelled"}

    async def _session_from_arguments(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any] | None:
        session_id = arguments.get("session_id")
        if session_id is not None:
            return await self.storage.get_scheduling_session(int(session_id))
        return await self.storage.latest_pending_session(context.effective_owner_user_id)
