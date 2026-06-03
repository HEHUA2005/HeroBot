from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from herobot.scheduling import TimeWindow, find_free_windows, from_iso, to_utc_iso
from herobot.storage import Storage, dumps_result


class SafeCalculator(ast.NodeVisitor):
    operators: dict[type[ast.operator], Callable[[float, float], float]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    unary_operators: dict[type[ast.unaryop], Callable[[float], float]] = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    def visit_Expression(self, node: ast.Expression) -> float:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> float:
        if isinstance(node.value, int | float):
            return float(node.value)
        raise ValueError("只支持数字计算。")

    def visit_BinOp(self, node: ast.BinOp) -> float:
        op = self.operators.get(type(node.op))
        if op is None:
            raise ValueError("不支持这个运算符。")
        return op(self.visit(node.left), self.visit(node.right))

    def visit_UnaryOp(self, node: ast.UnaryOp) -> float:
        op = self.unary_operators.get(type(node.op))
        if op is None:
            raise ValueError("不支持这个运算符。")
        return op(self.visit(node.operand))

    def generic_visit(self, node: ast.AST) -> float:
        raise ValueError(f"不支持的表达式：{type(node).__name__}")


def calculate(expression: str) -> str:
    tree = ast.parse(expression, mode="eval")
    result = SafeCalculator().visit(tree)
    if result.is_integer():
        return str(int(result))
    return f"{result:.8g}"


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_todo",
            "description": "Create a personal todo item.",
            "parameters": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_todos",
            "description": "List personal todo items.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["open", "done", "all"],
                        "default": "open",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_todo",
            "description": "Mark a todo as done by id.",
            "parameters": {
                "type": "object",
                "properties": {"todo_id": {"type": "integer"}},
                "required": ["todo_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_reminder",
            "description": "Create a Telegram reminder. remind_at must be ISO 8601 with timezone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "remind_at": {"type": "string"},
                },
                "required": ["content", "remind_at"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "List upcoming reminders.",
            "parameters": {
                "type": "object",
                "properties": {"include_sent": {"type": "boolean", "default": False}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Create a personal note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["title", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "Search personal notes by keyword.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_contact",
            "description": "Add or update a contact with their assistant bot username.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "bot_username": {"type": "string"},
                    "note": {"type": "string", "default": ""},
                },
                "required": ["name", "bot_username"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": "List all saved contacts.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_contact",
            "description": "Delete a contact by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a calendar event. Times must be ISO 8601 with timezone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start_at": {"type": "string"},
                    "end_at": {"type": "string"},
                    "note": {"type": "string", "default": ""},
                    "reminder_minutes_before": {"type": "integer", "default": 0},
                },
                "required": ["title", "start_at", "end_at"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_calendar_events",
            "description": "List calendar events in a time range. Times must be ISO 8601 with timezone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_at": {"type": "string"},
                    "end_at": {"type": "string"},
                },
                "required": ["start_at", "end_at"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_availability",
            "description": "Find free calendar slots in a range. Only returns free times, not busy details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_at": {"type": "string"},
                    "end_at": {"type": "string"},
                    "duration_minutes": {"type": "integer", "default": 30},
                },
                "required": ["start_at", "end_at"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_scheduling_request",
            "description": (
                "Create a pending scheduling request with a saved contact. "
                "Use this after resolving natural language time into ISO 8601 start/end."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {"type": "string"},
                    "request_text": {"type": "string"},
                    "duration_minutes": {"type": "integer"},
                    "window_start": {"type": "string"},
                    "window_end": {"type": "string"},
                },
                "required": [
                    "contact_name",
                    "request_text",
                    "duration_minutes",
                    "window_start",
                    "window_end",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Safely calculate a simple arithmetic expression.",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass
class ToolContext:
    chat_id: int
    user_id: int


class ToolRunner:
    def __init__(self, storage: Storage, timezone: str = "Asia/Shanghai") -> None:
        self.storage = storage
        self.timezone = timezone

    async def run(self, name: str, arguments: dict[str, Any], context: ToolContext) -> str:
        try:
            result = await self._run(name, arguments, context)
            return dumps_result({"ok": True, "result": result})
        except Exception as exc:
            return dumps_result({"ok": False, "error": str(exc)})

    async def _run(self, name: str, arguments: dict[str, Any], context: ToolContext) -> Any:
        if name == "create_todo":
            return await self.storage.create_todo(
                context.chat_id, context.user_id, arguments["title"].strip()
            )
        if name == "list_todos":
            return await self.storage.list_todos(context.chat_id, arguments.get("status", "open"))
        if name == "complete_todo":
            return await self.storage.complete_todo(context.chat_id, int(arguments["todo_id"]))
        if name == "create_reminder":
            remind_at = datetime.fromisoformat(arguments["remind_at"])
            if remind_at.tzinfo is None:
                remind_at = remind_at.replace(tzinfo=ZoneInfo(self.timezone))
            remind_at = remind_at.astimezone(timezone.utc)
            return await self.storage.create_reminder(
                context.chat_id,
                context.user_id,
                arguments["content"].strip(),
                remind_at.isoformat(),
            )
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
            now = datetime.now(ZoneInfo(self.timezone))
            return {"iso": now.isoformat(), "display": now.strftime("%Y-%m-%d %H:%M:%S %Z")}
        if name == "calculate":
            return {"expression": arguments["expression"], "result": calculate(arguments["expression"])}
        if name == "add_contact":
            return await self.storage.upsert_contact(
                arguments["name"], arguments["bot_username"], arguments.get("note", "")
            )
        if name == "list_contacts":
            return await self.storage.list_contacts()
        if name == "delete_contact":
            return await self.storage.delete_contact(arguments["name"])
        if name == "create_calendar_event":
            start = from_iso(arguments["start_at"], self.timezone)
            end = from_iso(arguments["end_at"], self.timezone)
            if end <= start:
                raise ValueError("end_at must be later than start_at")
            reminder_minutes = int(arguments.get("reminder_minutes_before", 0) or 0)
            reminder_at = None
            if reminder_minutes > 0:
                reminder_at = to_utc_iso(start - timedelta(minutes=reminder_minutes))
            event = await self.storage.create_calendar_event(
                context.user_id,
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
        if name == "list_calendar_events":
            start = from_iso(arguments["start_at"], self.timezone)
            end = from_iso(arguments["end_at"], self.timezone)
            return await self.storage.list_calendar_events(
                context.user_id, to_utc_iso(start), to_utc_iso(end)
            )
        if name == "find_availability":
            start = from_iso(arguments["start_at"], self.timezone)
            end = from_iso(arguments["end_at"], self.timezone)
            events = await self.storage.list_calendar_events(
                context.user_id, to_utc_iso(start), to_utc_iso(end)
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
        if name == "start_scheduling_request":
            contact = await self.storage.get_contact(arguments["contact_name"])
            if contact is None:
                raise ValueError(f"contact not found: {arguments['contact_name']}")
            start = from_iso(arguments["window_start"], self.timezone)
            end = from_iso(arguments["window_end"], self.timezone)
            if end <= start:
                raise ValueError("window_end must be later than window_start")
            return await self.storage.create_scheduling_session(
                context.chat_id,
                context.user_id,
                contact["name"],
                contact["bot_username"],
                arguments["request_text"].strip(),
                int(arguments["duration_minutes"]),
                to_utc_iso(start),
                to_utc_iso(end),
            )
        raise ValueError(f"unknown tool: {name}")
