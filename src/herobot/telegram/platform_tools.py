from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from telegram.error import BadRequest
from telegram.ext import ContextTypes

from herobot.agent import AgentEvent
from herobot.core.conversation_store import ConversationStore, dumps_result


logger = logging.getLogger(__name__)
TELEGRAM_MESSAGE_LIMIT = 4096


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
            "name": "send_telegram_messages",
            "description": (
                "Send multiple messages sequentially in the current Telegram chat. "
                "Use this when the user explicitly asks for separate consecutive messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "messages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                    },
                    "target_username": {"type": "string"},
                },
                "required": ["messages"],
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
        storage: ConversationStore,
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
        return name in {"send_telegram_message", "send_telegram_messages", "finish_task"}

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "send_telegram_message":
            return await self._send_telegram_message(arguments)
        if name == "send_telegram_messages":
            return await self._send_telegram_messages(arguments)
        if name == "finish_task":
            self.finished = True
            self.finish_payload = {
                "status": arguments.get("status", "done"),
                "summary": arguments.get("summary", ""),
                "needs_user_input": bool(arguments.get("needs_user_input", False)),
            }
            return dumps_result({"ok": True, "result": self.finish_payload})
        return dumps_result({"ok": False, "error": f"unknown platform tool: {name}"})

    async def _send_telegram_messages(self, arguments: dict[str, Any]) -> str:
        raw_messages = arguments.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            return dumps_result({"ok": False, "error": "messages must be a non-empty list"})
        target_username = str(arguments.get("target_username") or "").strip().lstrip("@")
        sent_messages: list[dict[str, Any]] = []
        for item in raw_messages[:20]:
            text = str(item).strip()
            if not text:
                continue
            result = json.loads(
                await self._send_telegram_message(
                    {"text": text, "target_username": target_username}
                    if target_username
                    else {"text": text}
                )
            )
            if not result.get("ok"):
                return dumps_result(result)
            sent_messages.append(result["result"])
        return dumps_result({"ok": True, "result": sent_messages})

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

        send_kwargs: dict[str, Any] = {
            "chat_id": self.event.chat_id,
            "text": outbound_text,
        }
        if reply_to_message_id is not None:
            send_kwargs["reply_to_message_id"] = reply_to_message_id
        if self.event.message_thread_id is not None:
            send_kwargs["message_thread_id"] = self.event.message_thread_id

        try:
            sent_messages = await self._send_chunks(send_kwargs, outbound_text)
        except BadRequest as exc:
            if reply_to_message_id is None or "message to be replied not found" not in str(exc).lower():
                logger.exception("Telegram send_message failed with BadRequest.")
                return dumps_result({"ok": False, "error": "telegram send failed"})
            send_kwargs.pop("reply_to_message_id", None)
            try:
                sent_messages = await self._send_chunks(send_kwargs, outbound_text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram send_message retry without reply failed.")
                return dumps_result({"ok": False, "error": "telegram send failed"})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram send_message failed.")
            return dumps_result({"ok": False, "error": "telegram send failed"})

        await self.storage.add_message(self.event.chat_id, "assistant", outbound_text)
        last_message = sent_messages[-1]
        return dumps_result(
            {
                "ok": True,
                "result": {
                    "chat_id": last_message.chat_id,
                    "message_id": last_message.message_id,
                    "message_ids": [item.message_id for item in sent_messages],
                    "target_username": target_username or None,
                    "text": outbound_text,
                    "chunks": len(sent_messages),
                },
            }
        )

    async def _send_chunks(self, send_kwargs: dict[str, Any], outbound_text: str) -> list[Any]:
        sent_messages: list[Any] = []
        chunks = split_telegram_text(outbound_text)
        for index, chunk in enumerate(chunks):
            chunk_kwargs = dict(send_kwargs)
            chunk_kwargs["text"] = chunk
            if index > 0:
                chunk_kwargs.pop("reply_to_message_id", None)
            sent_messages.append(await self.telegram_context.bot.send_message(**chunk_kwargs))
        return sent_messages

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
        return name in {"send_telegram_message", "send_telegram_messages", "finish_task"}

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        if name == "finish_task":
            self.finished = True
        if name == "send_telegram_messages":
            return json.dumps(
                {
                    "ok": True,
                    "result": [
                        {"text": str(item)}
                        for item in arguments.get("messages", [])
                        if str(item).strip()
                    ],
                },
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, "result": arguments}, ensure_ascii=False)


def split_telegram_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks
