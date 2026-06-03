from __future__ import annotations

import re
import secrets
from dataclasses import dataclass


MENTION_RE = re.compile(r"@([A-Za-z0-9_]{5,32})")
CALL_ID_RE = re.compile(r"\[herobot-call-id:\s*([^\]]+)\]", re.IGNORECASE)
DEPTH_RE = re.compile(r"\[herobot-depth:\s*(\d+)/(\d+)\]", re.IGNORECASE)
PURPOSE_RE = re.compile(r"\[herobot-purpose:\s*([^\]]+)\]", re.IGNORECASE)
FROM_RE = re.compile(r"\[herobot-from:\s*([^\]]+)\]", re.IGNORECASE)

DELEGATION_TRIGGERS = (
    "讨论",
    "问问",
    "询问",
    "咨询",
    "调用",
    "让",
    "请",
    "一起",
    "协作",
)


@dataclass(frozen=True)
class BotCall:
    call_id: str
    depth: int
    max_depth: int
    purpose: str
    from_bot: str


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


def choose_target_bot(text: str, own_username: str | None) -> str | None:
    own = (own_username or "").lower()
    for username in extract_mentions(text):
        if username.lower() != own:
            return username
    return None


def first_mentioned_bot(text: str) -> str | None:
    mentions = extract_mentions(text)
    return mentions[0] if mentions else None


def is_first_mentioned_bot(text: str, own_username: str | None) -> bool:
    first = first_mentioned_bot(text)
    return first is not None and first.lower() == (own_username or "").lower()


def looks_like_delegation(text: str, own_username: str | None) -> bool:
    return is_first_mentioned_bot(text, own_username) and choose_target_bot(
        text, own_username
    ) is not None and any(
        trigger in text for trigger in DELEGATION_TRIGGERS
    )


def clean_user_request(text: str, target_username: str, own_username: str | None) -> str:
    cleaned = text
    for username in {target_username, own_username or ""}:
        if username:
            cleaned = re.sub(rf"@{re.escape(username)}", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split()).strip()
    cleanup_patterns = (
        r"^请?你?和\s*讨论一下[，,：:\s]*",
        r"^请?你?跟\s*讨论一下[，,：:\s]*",
        r"^请?你?与\s*讨论一下[，,：:\s]*",
        r"^请?你?和\s*聊聊[，,：:\s]*",
        r"^请?你?跟\s*聊聊[，,：:\s]*",
        r"^讨论一下[，,：:\s]*",
    )
    for pattern in cleanup_patterns:
        cleaned = re.sub(pattern, "", cleaned).strip()
    return cleaned


def new_call_id() -> str:
    return secrets.token_urlsafe(12)


def parse_bot_call(text: str) -> BotCall | None:
    call_id = CALL_ID_RE.search(text)
    depth = DEPTH_RE.search(text)
    purpose = PURPOSE_RE.search(text)
    from_bot = FROM_RE.search(text)
    if not call_id or not depth or not purpose or not from_bot:
        return None
    return BotCall(
        call_id=call_id.group(1).strip(),
        depth=int(depth.group(1)),
        max_depth=int(depth.group(2)),
        purpose=purpose.group(1).strip().lower(),
        from_bot=from_bot.group(1).strip().lstrip("@"),
    )


def strip_envelope(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("[herobot-") and stripped.endswith("]"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def build_collaboration_prompt(topic: str) -> str:
    return (
        "你正在参与一个群聊里的 bot-to-bot 讨论。\n"
        "请像自然对话一样回应，不要解释协议，不要说“你可以复制给对方”。\n"
        "使用纯文本，不要 Markdown 表格、标题星号或引用块。\n"
        "请给出你的真实判断和关键理由；只有在确实能推进讨论时才顺手问一个问题，不要每轮都固定追问。\n\n"
        f"讨论题目：{topic}"
    )


def build_request_message(
    target_username: str,
    from_username: str,
    call_id: str,
    request: str,
    max_depth: int,
    depth: int = 1,
) -> str:
    del from_username, call_id, max_depth, depth
    return (
        f"@{target_username}\n"
        f"我先抛个观点给你：{request}\n"
        "你怎么看？"
    )


def build_response_message(
    target_username: str,
    from_username: str,
    call_id: str,
    depth: int,
    max_depth: int,
    response: str,
) -> str:
    del target_username, from_username, call_id, depth, max_depth
    return response


def is_natural_bot_request(text: str, own_username: str | None) -> bool:
    return bool(own_username and text.lstrip().lower().startswith(f"@{own_username.lower()}"))


def natural_request_body(text: str, own_username: str | None) -> str:
    if not own_username:
        return text.strip()
    return re.sub(rf"^\s*@{re.escape(own_username)}\b", "", text, flags=re.IGNORECASE).strip()


def build_natural_followup_message(target_username: str, followup: str) -> str:
    return f"@{target_username}\n{followup}"


def build_synthesis_prompt(peer_username: str, original_request: str, peer_response: str) -> str:
    return (
        f"你发起了和 @{peer_username} 的 bot-to-bot 协作讨论。\n"
        f"原始问题：{original_request}\n\n"
        f"@{peer_username} 的回复：\n{peer_response}\n\n"
        "请给群里的用户一个简短综合结论：\n"
        "- 不要说“你可以复制给对方”。\n"
        "- 不要重复协议字段。\n"
        "- 使用纯文本，不要 Markdown 表格、标题星号或引用块。\n"
        "- 先概括对方观点，再给你的补充或分歧，最后给一句结论。\n"
        "- 控制在 150 字以内。"
    )


def build_followup_prompt(peer_username: str, original_request: str, peer_response: str) -> str:
    return (
        f"你正在和 @{peer_username} 讨论。\n"
        f"原始问题：{original_request}\n\n"
        f"@{peer_username} 刚才的观点：\n{peer_response}\n\n"
        "请你作为发起方参与讨论，而不是直接收束：\n"
        "- 先用一句话回应你同意或不同意哪里。\n"
        "- 然后补充一个新角度、反例或判断标准。\n"
        "- 如果没有必要，不要用问题结尾。\n"
        "- 使用纯文本，不要 Markdown 表格、标题星号或引用块。\n"
        "- 不要写最终结论，不要说“你可以复制给对方”。\n"
        "- 控制在 120 字以内。"
    )
