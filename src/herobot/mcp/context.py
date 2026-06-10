from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


def context_from_payload(payload: dict[str, Any], default_timezone: str = "Asia/Shanghai") -> ToolContext:
    raw = payload.get(HIDDEN_CONTEXT_KEY) or {}
    if "chat_id" not in raw:
        raise ValueError("missing required HeroBot context: chat_id")
    if "user_id" not in raw:
        raise ValueError("missing required HeroBot context: user_id")
    return ToolContext(
        chat_id=int(raw["chat_id"]),
        user_id=int(raw["user_id"]),
        chat_type=str(raw.get("chat_type", "private")),
        timezone=str(raw.get("timezone") or default_timezone),
        owner_user_id=int(raw["owner_user_id"]) if raw.get("owner_user_id") is not None else None,
    )
