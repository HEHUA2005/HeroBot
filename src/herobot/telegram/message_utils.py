from __future__ import annotations

import re

from telegram import Update


MENTION_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")


def parse_bot_usernames(value: str | None) -> set[str]:
    if not value:
        return set()
    usernames: set[str] = set()
    for item in value.split(","):
        username = item.strip().lstrip("@").lower()
        if username:
            usernames.add(username)
    return usernames


def extract_mentions(text: str) -> list[str]:
    seen: set[str] = set()
    mentions: list[str] = []
    for match in MENTION_RE.finditer(text):
        username = match.group(1)
        key = username.lower()
        if key not in seen:
            seen.add(key)
            mentions.append(username)
    return mentions


def first_mentioned_bot(text: str) -> str | None:
    mentions = extract_mentions(text)
    return mentions[0] if mentions else None


def is_first_mentioned_bot(text: str, own_username: str | None) -> bool:
    first = first_mentioned_bot(text)
    return first is not None and first.lower() == (own_username or "").lower()


def starts_with_bot_mention(text: str, own_username: str | None) -> bool:
    if not own_username:
        return False
    pattern = rf"^\s*@{re.escape(own_username)}(?=$|[^A-Za-z0-9_])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


async def is_addressed_to_bot(update: Update, bot_username: str | None, bot_id: int) -> bool:
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return False
    if chat.type == "private":
        return True
    if message.text and message.text.startswith("/"):
        return True

    sender_is_bot = bool(message.from_user and message.from_user.is_bot)
    if sender_is_bot and message.text:
        return starts_with_bot_mention(message.text, bot_username)

    if bot_username and message.text and f"@{bot_username.lower()}" in message.text.lower():
        return is_first_mentioned_bot(message.text, bot_username)

    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id == bot_id

    return False


def strip_bot_mention(text: str, bot_username: str | None) -> str:
    if not bot_username:
        return text
    return text.replace(f"@{bot_username}", "").replace(f"@{bot_username.lower()}", "").strip()
