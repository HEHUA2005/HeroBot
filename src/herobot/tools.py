from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from herobot.scheduling import TimeWindow, find_free_windows, from_iso, intersect_windows, to_utc_iso
from herobot.storage import Storage


HIDDEN_CONTEXT_KEY = "herobot_context"


@dataclass(frozen=True)
class ToolContext:
    chat_id: int
    user_id: int
    chat_type: str = "private"
    timezone: str = "Asia/Shanghai"
    owner_user_id: int | None = None

    @property
    def effective_owner_user_id(self) -> int:
        return self.owner_user_id if self.owner_user_id is not None else self.user_id


# REVIEW: context_from_payload 对参数的提取非常脆弱。
# raw.get("chat_id", 0) 默认为 0——如果 MCP 上下文注入出问题，chat_id=0 会导致
# 所有数据写到一个"幽灵会话"里，而且不会报错，用户完全感知不到。
# 建议 chat_id 和 user_id 缺失时直接抛异常，而不是静默用 0。
def context_from_payload(payload: dict[str, Any], default_timezone: str = "Asia/Shanghai") -> ToolContext:
    raw = payload.get(HIDDEN_CONTEXT_KEY) or {}
    return ToolContext(
        chat_id=int(raw.get("chat_id", 0)),
        user_id=int(raw.get("user_id", 0)),
        chat_type=str(raw.get("chat_type", "private")),
        timezone=str(raw.get("timezone") or default_timezone),
        owner_user_id=int(raw["owner_user_id"]) if raw.get("owner_user_id") is not None else None,
    )


def ok_result(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def error_result(error: Exception | str) -> dict[str, Any]:
    return {"ok": False, "error": str(error)}


class BusinessTools:
    def __init__(self, storage: Storage, timezone_name: str = "Asia/Shanghai") -> None:
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

    # REVIEW: 这个巨大的 if/elif 链是典型的"面条式分发"。现在有 ~18 个工具，
    # 每加一个新工具就要在这里加一个 if 分支、在 mcp_server.py 加一个 @mcp.tool 函数、
    # 可能还要在 storage.py 加方法——三个文件联动改。
    #
    # 更好的方式是用注册表模式（dispatch table）：
    #   _handlers = {"create_todo": self._create_todo, "list_todos": self._list_todos, ...}
    #   return await self._handlers[name](arguments, context)
    # 或者用装饰器模式自动注册。这样加新工具只需要写一个方法并注册。
    #
    # 另外注意：所有工具参数都是 dict[str, Any]，没有任何校验。
    # 如果 LLM 漏传了 "title" 参数，这里会直接 KeyError 崩溃，
    # 用户看到的是 "Agent 执行失败：'title'"——完全看不懂。
    # 建议用 pydantic 或 dataclass 做参数校验，给用户友好的错误提示。
    async def _invoke(self, name: str, arguments: dict[str, Any], context: ToolContext) -> Any:
        if name == "create_todo":
            return await self.storage.create_todo(
                context.chat_id, context.user_id, arguments["title"].strip()
            )
        if name == "list_todos":
            return await self.storage.list_todos(context.chat_id, arguments.get("status", "open"))
        if name == "complete_todo":
            return await self.storage.complete_todo(context.chat_id, int(arguments["todo_id"]))
        if name == "create_reminder":
            return await self.create_reminder(arguments, context)
        if name == "list_reminders":
            return await self.storage.list_reminders(
                context.chat_id, bool(arguments.get("include_sent", False))
            )
        if name == "create_note":
            return await self.storage.create_note(
                context.chat_id,
                context.user_id,
                arguments["title"].strip(),
                arguments["content"].strip(),
            )
        if name == "search_notes":
            query = arguments["query"].strip()
            return await self.storage.search_notes_by_terms(context.chat_id, [query])
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
        raise ValueError(f"unknown tool: {name}")

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

    # REVIEW: find_availability 返回的是 UTC ISO 字符串。用户看到的会是类似
    # "2026-06-04T01:00:00+00:00" 这种格式——对于一个中文用户来说完全不友好。
    # 虽然 LLM 理论上会帮忙转换显示，但如果 LLM 不转换（或者转换错了），
    # 用户体验会很差。建议在返回结果中同时包含 UTC 和本地时间的可读格式，
    # 例如 {"start_at": "...", "display": "2026-06-04 09:00 (CST)"}。
    #
    # 另外 find_free_windows 的 limit 默认是 3，只返回前 3 个空闲段。
    # 如果用户查"这周有空吗"，只看到 3 个可能不够。这个 limit 应该可配置。
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
