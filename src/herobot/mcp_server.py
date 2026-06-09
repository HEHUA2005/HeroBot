from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from herobot.storage import Storage
from herobot.tools import BusinessTools, ToolContext, context_from_payload


mcp = FastMCP(
    "herobot-business-tools",
    instructions=(
        "Business tools for HeroBot personal assistant data: notes, reminders, "
        "contacts, calendar, availability, and scheduling sessions."
    ),
)

# REVIEW: 用 module-level 全局变量 + global 做懒初始化是经典的 bad smell。
# 问题：
# 1. 不可测试——单元测试无法轻松替换 _business_tools，也无法在测试间重置状态
# 2. 不可配置——Storage 路径和 timezone 在首次调用时就固化了
# 3. 没有清理机制——进程退出时 Storage 连接（如果改为持久连接的话）没有关闭入口
# 4. 线程安全问题——虽然 MCP server 通常是单线程的，但这个模式本身不安全
#
# 建议用 FastMCP 的 lifespan 钩子来管理初始化和清理，或者至少封装成一个类。
_business_tools: BusinessTools | None = None
_initialized = False


async def business_tools() -> BusinessTools:
    global _business_tools, _initialized
    if _business_tools is None:
        timezone_name = os.getenv("HEROBOT_DEFAULT_TIMEZONE", "Asia/Shanghai")
        storage = Storage(os.getenv("HEROBOT_DB_PATH", "data/herobot.sqlite3"))
        _business_tools = BusinessTools(storage, timezone_name=timezone_name)
    if not _initialized:
        await _business_tools.storage.init()
        _initialized = True
    return _business_tools


def context(payload: dict[str, Any] | None) -> ToolContext:
    return context_from_payload(payload or {}, os.getenv("HEROBOT_DEFAULT_TIMEZONE", "Asia/Shanghai"))


async def invoke(
    name: str, arguments: dict[str, Any], herobot_context: dict[str, Any] | None
) -> dict[str, Any]:
    tools = await business_tools()
    return await tools.invoke(name, arguments, context({ "herobot_context": herobot_context or {} }))


# REVIEW: 下面所有 @mcp.tool 函数都是纯粹的样板代码——每个函数只是把参数打包成 dict
# 然后调用 invoke()。这意味着每加一个新工具，你要：
# 1. 在 tools.py 的 _invoke 里加一个 if 分支
# 2. 在这里写一个一模一样的 @mcp.tool 包装函数
# 3. 可能在 storage.py 里加一个方法
#
# 这种重复劳动完全可以通过元编程消除。比如可以：
# - 定义一个 tool 描述的 registry，自动生成 MCP tool 注册
# - 或者让 BusinessTools 的方法直接用装饰器注册为 MCP tool
#
# 当前 ~20 个工具函数占了 200+ 行代码，实际逻辑为零。
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
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await invoke(
        "find_availability",
        {
            "start_at": start_at,
            "end_at": end_at,
            "duration_minutes": duration_minutes,
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
