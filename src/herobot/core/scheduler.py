from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from telegram.ext import Application

from herobot.mcp.context import ToolContext
from herobot.mcp.registry import MCPToolRegistry


logger = logging.getLogger(__name__)


async def reminder_loop(
    app: Application,
    tool_registry: MCPToolRegistry,
    interval_seconds: int = 30,
    timezone_name: str = "Asia/Shanghai",
) -> None:
    if not {"list_due_reminders", "mark_reminder_sent"} <= tool_registry.hidden_tool_names:
        logger.info("Reminder scheduler disabled; hidden reminder tools are not available.")
        return

    context = ToolContext(chat_id=0, user_id=0, timezone=timezone_name)
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            result = json.loads(
                await tool_registry.call_hidden(
                    "list_due_reminders",
                    {"now_iso": now_iso},
                    context,
                )
            )
            if not result.get("ok"):
                logger.warning("Failed to list due reminders: %s", result.get("error"))
            for reminder in _reminders_from_result(result):
                await _deliver_reminder(app, tool_registry, context, reminder)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder loop failed")
        await asyncio.sleep(interval_seconds)


def _reminders_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    if not result.get("ok"):
        return []
    reminders = result.get("result") or []
    if not isinstance(reminders, list):
        return []
    return [item for item in reminders if isinstance(item, dict)]


async def _deliver_reminder(
    app: Application,
    tool_registry: MCPToolRegistry,
    context: ToolContext,
    reminder: dict[str, Any],
) -> None:
    reminder_id = int(reminder["id"])
    try:
        await app.bot.send_message(
            chat_id=int(reminder["chat_id"]),
            text=f"提醒：{reminder['content']}",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Failed to send reminder id=%s chat_id=%s; marking sent to avoid retry loop.",
            reminder.get("id"),
            reminder.get("chat_id"),
        )
        await _mark_reminder_sent(tool_registry, context, reminder_id)
        return

    await _mark_reminder_sent(tool_registry, context, reminder_id)


async def _mark_reminder_sent(
    tool_registry: MCPToolRegistry,
    context: ToolContext,
    reminder_id: int,
) -> None:
    try:
        result = json.loads(
            await tool_registry.call_hidden(
                "mark_reminder_sent",
                {"reminder_id": reminder_id},
                context,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Failed to mark reminder id=%s as sent.", reminder_id)
        return
    if not result.get("ok"):
        logger.warning(
            "Failed to mark reminder id=%s as sent: %s",
            reminder_id,
            result.get("error"),
        )
