from __future__ import annotations

import json
from typing import Any

from telegram.ext import ContextTypes

from herobot.agent import AgentEvent
from herobot.storage import Storage, dumps_result


PLATFORM_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "send_telegram_message",
            "description": (
                "Send a message in the current Telegram chat. Optionally mention a target bot/user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "target_username": {"type": "string"},
                    "reply_to_message_id": {"type": "integer"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": "Explicitly finish the current Agent event handling loop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["done", "blocked", "failed"]},
                    "summary": {"type": "string"},
                    "needs_user_input": {"type": "boolean"},
                },
                "required": ["status", "summary", "needs_user_input"],
                "additionalProperties": False,
            },
        },
    },
]


class TelegramPlatformTools:
    def __init__(
        self,
        telegram_context: ContextTypes.DEFAULT_TYPE,
        storage: Storage,
        event: AgentEvent,
    ) -> None:
        self.telegram_context = telegram_context
        self.storage = storage
        self.event = event
        self.finished = False
        self.finish_payload: dict[str, Any] | None = None

    def openai_tool_schemas(self) -> list[dict[str, Any]]:
        return PLATFORM_TOOL_SCHEMAS

    def can_handle(self, name: str) -> bool:
        return name in {"send_telegram_message", "finish_task"}

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "send_telegram_message":
            return await self._send_telegram_message(arguments)
        if name == "finish_task":
            self.finished = True
            self.finish_payload = {
                "status": arguments.get("status", "done"),
                "summary": arguments.get("summary", ""),
                "needs_user_input": bool(arguments.get("needs_user_input", False)),
            }
            return dumps_result({"ok": True, "result": self.finish_payload})
        return dumps_result({"ok": False, "error": f"unknown platform tool: {name}"})

    # REVIEW: _send_telegram_message 没有做 Telegram API 的错误处理。
    # Telegram sendMessage 可能因为很多原因失败：
    # - 消息太长（Telegram 限制 4096 字符）
    # - bot 被踢出群
    # - reply_to_message_id 指向的消息已被删除
    # - 频率限制（429）
    #
    # 当前任何失败都会抛异常上去，最终变成 "Agent 执行失败：..."。
    # 特别是消息长度——如果 LLM 生成了超长文本，应该自动截断或分段发送。
    # 这是一个很常见的用户可见问题。
    async def _send_telegram_message(self, arguments: dict[str, Any]) -> str:
        text = str(arguments.get("text", "")).strip()
        if not text:
            return dumps_result({"ok": False, "error": "text is required"})

        target_username = str(arguments.get("target_username") or "").strip().lstrip("@")
        outbound_text = self._compose_outbound_text(text, target_username)

        reply_to_message_id = arguments.get("reply_to_message_id")
        if reply_to_message_id is None and not target_username:
            reply_to_message_id = self.event.message_id
        if reply_to_message_id is not None:
            reply_to_message_id = int(reply_to_message_id)

        sent = await self.telegram_context.bot.send_message(
            chat_id=self.event.chat_id,
            text=outbound_text,
            reply_to_message_id=reply_to_message_id,
        )
        await self.storage.add_message(self.event.chat_id, "assistant", outbound_text)
        return dumps_result(
            {
                "ok": True,
                "result": {
                    "chat_id": sent.chat_id,
                    "message_id": sent.message_id,
                    "target_username": target_username or None,
                    "text": outbound_text,
                },
            }
        )

    def _compose_outbound_text(self, text: str, target_username: str) -> str:
        if not target_username:
            return text
        if text.lstrip().lower().startswith(f"@{target_username.lower()}"):
            return text
        return f"@{target_username}\n{text}"


class RecordingPlatformTools:
    def __init__(self) -> None:
        self.finished = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def openai_tool_schemas(self) -> list[dict[str, Any]]:
        return PLATFORM_TOOL_SCHEMAS

    def can_handle(self, name: str) -> bool:
        return name in {"send_telegram_message", "finish_task"}

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        if name == "finish_task":
            self.finished = True
        return json.dumps({"ok": True, "result": arguments}, ensure_ascii=False)
