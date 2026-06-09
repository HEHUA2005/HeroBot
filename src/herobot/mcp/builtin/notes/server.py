from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from herobot.mcp.builtin.notes.storage import NotesStorage
from herobot.mcp.builtin.notes.tools import NotesTools
from herobot.mcp.context import HIDDEN_CONTEXT_KEY, ToolContext, context_from_payload


mcp = FastMCP(
    "herobot-notes-tools",
    instructions="Notes and todo tools for HeroBot. Does not access Telegram directly.",
)

_tools: NotesTools | None = None
_initialized = False


async def notes_tools() -> NotesTools:
    global _tools, _initialized
    if _tools is None:
        db_path = (
            os.getenv("HEROBOT_NOTES_DB_PATH")
            or os.getenv("HEROBOT_DB_PATH")
            or "data/herobot-notes.sqlite3"
        )
        _tools = NotesTools(NotesStorage(db_path))
    if not _initialized:
        await _tools.storage.init()
        _initialized = True
    return _tools


def context(payload: dict[str, Any] | None) -> ToolContext:
    return context_from_payload(payload or {}, os.getenv("HEROBOT_DEFAULT_TIMEZONE", "Asia/Shanghai"))


async def invoke(
    name: str, arguments: dict[str, Any], herobot_context: dict[str, Any] | None
) -> dict[str, Any]:
    tools = await notes_tools()
    return await tools.invoke(name, arguments, context({HIDDEN_CONTEXT_KEY: herobot_context or {}}))


@mcp.tool(description="Create a personal todo item.")
async def create_todo(title: str, herobot_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return await invoke("create_todo", {"title": title}, herobot_context)


@mcp.tool(description="List personal todo items.")
async def list_todos(
    status: str = "open", herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("list_todos", {"status": status}, herobot_context)


@mcp.tool(description="Mark a todo as done by id.")
async def complete_todo(
    todo_id: int, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("complete_todo", {"todo_id": todo_id}, herobot_context)


@mcp.tool(description="Create a personal note.")
async def create_note(
    title: str, content: str, herobot_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    return await invoke("create_note", {"title": title, "content": content}, herobot_context)


@mcp.tool(description="Search personal notes by keyword.")
async def search_notes(query: str, herobot_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return await invoke("search_notes", {"query": query}, herobot_context)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
