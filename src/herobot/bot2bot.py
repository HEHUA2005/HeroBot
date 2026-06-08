from __future__ import annotations

import re


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
