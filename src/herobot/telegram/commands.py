from __future__ import annotations


HELP_TEXT = """可用命令：
/start - 开始会话
/help - 查看帮助
/reset - 清空短期记忆
/whoami - 查看你的 Telegram user id
/chatid - 查看当前 chat id
/contacts - 查看联系人
/calendar - 查看近期日程
/calender - /calendar 的常见拼写别名
/availability - 查看近期空闲
/pending - 查看待确认约时间

你可以直接用自然语言对我说：
你能做什么？
帮我处理一下这件事：……
帮我联系 @other_bot 问一下……

具体业务能力由当前启用的 MCP 工具决定。问“你能做什么？”可以查看当前能力。
"""


START_TEXT = "HeroBot Agent 已启动。发送 /help 看示例，或直接告诉我你要记录、提醒、查询或协作什么。"


UNKNOWN_COMMAND_TEXT = "没识别这个命令。常用命令：/help、/calendar、/contacts、/availability、/pending"


def command_name(text: str) -> str | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    return stripped.split(maxsplit=1)[0].lstrip("/").split("@", 1)[0].lower()


def alias_to_text(command: str, raw_text: str, aliases: dict[str, str]) -> str | None:
    name = command_name(command)
    if name is None:
        return None
    text = aliases.get(name)
    if text is None:
        return None
    if name == "availability" and raw_text and " " in raw_text:
        return f"查看这个范围内的空闲时间：{raw_text.split(maxsplit=1)[1]}"
    return text
