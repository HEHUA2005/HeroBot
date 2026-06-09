from __future__ import annotations

from dataclasses import dataclass, field

from herobot.mcp.context import ToolContext


@dataclass(frozen=True)
class AgentEvent:
    source: str
    chat_id: int
    chat_type: str
    message_id: int
    sender_id: int
    sender_username: str
    sender_is_bot: bool
    text: str
    mentioned_usernames: list[str] = field(default_factory=list)
    addressed_to_self: bool = False
    self_bot_id: int | None = None
    self_bot_username: str | None = None
    reply_to_message_id: int | None = None
    reply_to_sender_username: str | None = None
    message_thread_id: int | None = None
    owner_user_id: int | None = None
    timezone: str = "Asia/Shanghai"

    def tool_context(self) -> ToolContext:
        return ToolContext(
            chat_id=self.chat_id,
            user_id=self.sender_id,
            chat_type=self.chat_type,
            timezone=self.timezone,
            owner_user_id=self.owner_user_id,
        )

    def as_prompt(self) -> str:
        target_mentions = ", ".join(self.mentioned_usernames) or "none"
        return (
            "Telegram 事件：\n"
            f"- chat_id: {self.chat_id}\n"
            f"- chat_type: {self.chat_type}\n"
            f"- message_id: {self.message_id}\n"
            f"- sender_id: {self.sender_id}\n"
            f"- sender_username: {self.sender_username or 'unknown'}\n"
            f"- sender_is_bot: {self.sender_is_bot}\n"
            f"- self_bot_id: {self.self_bot_id or 'unknown'}\n"
            f"- self_bot_username: {self.self_bot_username or 'unknown'}\n"
            f"- addressed_to_self: {self.addressed_to_self}\n"
            f"- current_message_target_mentions: {target_mentions}\n"
            f"- message_thread_id: {self.message_thread_id or 'none'}\n"
            f"- reply_to_message_id: {self.reply_to_message_id or 'none'}\n"
            f"- reply_to_sender_username: {self.reply_to_sender_username or 'none'}\n\n"
            f"消息内容：\n{self.text}"
        )
