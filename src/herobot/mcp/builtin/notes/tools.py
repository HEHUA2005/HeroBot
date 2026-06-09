from __future__ import annotations

from typing import Any

from herobot.mcp.builtin.notes.storage import NotesStorage
from herobot.mcp.context import ToolContext


def ok_result(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def error_result(error: Exception | str) -> dict[str, Any]:
    return {"ok": False, "error": str(error)}


class NotesTools:
    def __init__(self, storage: NotesStorage) -> None:
        self.storage = storage

    async def invoke(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        try:
            result = await self._invoke(name, arguments, context)
            return ok_result(result)
        except Exception as exc:
            return error_result(exc)

    async def _invoke(self, name: str, arguments: dict[str, Any], context: ToolContext) -> Any:
        if name == "create_todo":
            return await self.storage.create_todo(
                context.chat_id, context.user_id, arguments["title"].strip()
            )
        if name == "list_todos":
            return await self.storage.list_todos(context.chat_id, arguments.get("status", "open"))
        if name == "complete_todo":
            return await self.storage.complete_todo(context.chat_id, int(arguments["todo_id"]))
        if name == "create_note":
            return await self.storage.create_note(
                context.chat_id,
                context.user_id,
                arguments["title"].strip(),
                arguments["content"].strip(),
            )
        if name == "search_notes":
            return await self.storage.search_notes(context.chat_id, arguments["query"].strip())
        raise ValueError(f"unknown notes tool: {name}")
