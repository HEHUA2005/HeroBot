from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from herobot.agent import Agent, AgentEvent
from herobot.bot2bot import extract_mentions, is_first_mentioned_bot, parse_bot_usernames
from herobot.llm import LLMClient, LLMConfig
from herobot.scheduler import reminder_loop
from herobot.storage import Storage
from herobot.platform import TelegramPlatformTools
from herobot.tool_registry import MCPToolConfig, MCPToolRegistry


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
提醒我 20 分钟后喝水
我今天要买牛奶
帮我记一下护照放在抽屉里
我还有什么待办？
"""


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, level_name, logging.INFO),
    )


def parse_allowed_user_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    return {int(item.strip()) for item in value.split(",") if item.strip()}


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.application.bot_data["user_whitelist_enabled"]:
        return True
    allowed_user_ids: set[int] = context.application.bot_data["allowed_user_ids"]
    if not allowed_user_ids:
        return False
    user = update.effective_user
    return user is not None and user.id in allowed_user_ids


def owner_user_id(context: ContextTypes.DEFAULT_TYPE, fallback: int = 0) -> int:
    owner_user_ids: set[int] = context.application.bot_data["owner_user_ids"]
    return next(iter(owner_user_ids), fallback)


def instance_timezone(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.application.bot_data["timezone"]


def is_allowed_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.application.bot_data["bot_to_bot_enabled"]:
        return False
    user = update.effective_user
    if user is None or not user.is_bot:
        return False
    if user.id == context.bot.id:
        return False
    allowed_usernames: set[str] = context.application.bot_data["allowed_bot_usernames"]
    if not allowed_usernames:
        return True
    return (user.username or "").lower() in allowed_usernames


async def is_addressed_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return False
    if chat.type == "private":
        return True
    if message.text and message.text.startswith("/"):
        return True

    bot_username = context.bot.username
    if bot_username and message.text and f"@{bot_username.lower()}" in message.text.lower():
        return is_first_mentioned_bot(message.text, bot_username)

    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id == context.bot.id

    return False


def strip_bot_mention(text: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    bot_username = context.bot.username
    if not bot_username:
        return text
    return text.replace(f"@{bot_username}", "").replace(f"@{bot_username.lower()}", "").strip()


# REVIEW: parse_scheduling_confirmation 放在 bot.py 里违反了关注点分离。
# 这是纯业务逻辑（解析用户的确认意图），跟 Telegram 适配层无关。
# 应该放到 agent.py 或单独的 parsing 模块。
#
# 而且这个函数目前只在 test_core.py 里被测试，在运行时代码中并没有被调用！
# 看起来像是写了但忘了集成到 handle_message 流程中。
# 如果意图是在用户确认时走快速路径（不经过 LLM），那应该在 handle_message 里
# 先尝试 parse，命中了直接调用 confirm_scheduling_candidate，省一次 LLM 调用。
def parse_scheduling_confirmation(text: str, allow_soft_confirmation: bool) -> int | None:
    # REVIEW: import re 应该放在文件顶部，不要在函数内部 import。
    # 这个文件的其他地方没用 re，但这不是理由——PEP 8 明确建议 import 放在文件开头。
    import re

    normalized = (
        text.lower()
        .replace("。", "")
        .replace("！", "")
        .replace("!", "")
        .replace("，", "")
        .replace(",", "")
        .strip()
    )
    soft_confirmation_words = {
        "可以",
        "可以的",
        "好",
        "好的",
        "行",
        "行的",
        "ok",
        "okay",
        "确认",
        "确定",
        "就这个",
        "定这个",
        "没问题",
    }
    if allow_soft_confirmation and normalized in soft_confirmation_words:
        return 0

    explicit_patterns = [
        r"^(?:确认|选|选择|定)\s*第?\s*(\d+)\s*(?:个)?(?:时间|候选)?$",
        r"^第\s*(\d+)\s*个(?:时间|候选)?$",
        r"^第\s*(\d+)\s*(?:时间|候选)$",
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, normalized)
        if match:
            return int(match.group(1)) - 1

    chinese_index = {"一": 0, "二": 1, "三": 2}
    chinese_patterns = [
        r"^(?:确认|选|选择|定)\s*第?\s*([一二三])\s*(?:个)?(?:时间|候选)?$",
        r"^第\s*([一二三])\s*个(?:时间|候选)?$",
        r"^第\s*([一二三])\s*(?:时间|候选)$",
    ]
    for pattern in chinese_patterns:
        match = re.search(pattern, normalized)
        if match:
            return chinese_index[match.group(1)]

    return None


async def reject_unauthorized(update: Update) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        "这个 bot 已启用个人白名单。发送 /whoami 查看你的 Telegram user id，"
        "然后把它填入 TELEGRAM_ALLOWED_USER_IDS。"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not is_allowed(update, context):
        await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(
        "HeroBot Agent 已启动。发送 /help 看示例，或直接告诉我你要记录、提醒、查询或协作什么。"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not is_allowed(update, context):
        await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # REVIEW: 用 `del context` 来消除 unused parameter 警告是 anti-pattern。
    # 惯用做法是把参数名改为 `_context` 或 `context: ContextTypes.DEFAULT_TYPE  # noqa`。
    # `del` 一个参数让人以为这里有什么特殊意图，实际上只是想消除 lint 警告。
    del context
    if update.effective_message is None or update.effective_user is None:
        return
    await update.effective_message.reply_text(f"你的 Telegram user id：{update.effective_user.id}")


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_message is None or update.effective_chat is None:
        return
    await update.effective_message.reply_text(
        f"当前 chat id：{update.effective_chat.id}\nchat type：{update.effective_chat.type}"
    )


async def contacts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_command_as_agent(update, context, "查看联系人通讯录")


async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_command_as_agent(update, context, "查看未来 7 天日程")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not is_allowed(update, context):
        if update.effective_chat and update.effective_chat.type == "private":
            await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(
        "没识别这个命令。常用命令：/help、/calendar、/contacts、/availability、/pending"
    )


async def availability_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text if update.effective_message else ""
    request = "查看近期空闲时间"
    if text and " " in text:
        request = f"查看这个范围内的空闲时间：{text.split(maxsplit=1)[1]}"
    await handle_command_as_agent(update, context, request)


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_command_as_agent(update, context, "查看待确认的约时间")


def build_agent_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text_override: str | None = None,
    force_addressed: bool = False,
) -> AgentEvent | None:
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    if chat is None or message is None or user is None:
        return None
    text = text_override if text_override is not None else (message.text or "")
    reply_to = message.reply_to_message
    return AgentEvent(
        source="telegram",
        chat_id=chat.id,
        chat_type=chat.type,
        message_id=message.message_id,
        sender_id=user.id,
        sender_username=user.username or user.full_name or "",
        sender_is_bot=bool(user.is_bot),
        text=text,
        mentioned_usernames=extract_mentions(text),
        addressed_to_self=force_addressed,
        reply_to_message_id=reply_to.message_id if reply_to else None,
        reply_to_sender_username=(
            reply_to.from_user.username
            if reply_to and reply_to.from_user and reply_to.from_user.username
            else None
        ),
        owner_user_id=owner_user_id(context, user.id),
        timezone=instance_timezone(context),
    )


async def run_agent_for_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text_override: str | None = None,
    force_addressed: bool = False,
) -> None:
    if update.effective_chat is None or update.effective_message is None:
        return
    event = build_agent_event(update, context, text_override, force_addressed)
    if event is None:
        return
    agent: Agent = context.application.bot_data["agent"]
    storage: Storage = context.application.bot_data["storage"]
    platform_tools = TelegramPlatformTools(context, storage, event)
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        await agent.handle_event(event, platform_tools)
    except Exception as exc:
        logging.getLogger(__name__).exception("Agent event handling failed")
        # REVIEW: 把原始异常信息直接展示给用户是很糟糕的用户体验。
        # 用户看到 "Agent 执行失败：'title'" 或 "Agent 执行失败：Connection refused"
        # 完全不知道该怎么办。应该给用户一个友好的提示，比如：
        # "抱歉，我遇到了一些问题，请稍后再试。如果持续出现，请联系管理员。"
        # 详细的错误信息只应该出现在服务端日志里（上面的 logger.exception 已经做了）。
        #
        # 另外这里还有一个安全问题：异常信息可能泄露内部实现细节，
        # 比如数据库路径、API key 格式错误等敏感信息。
        await update.effective_message.reply_text(f"Agent 执行失败：{exc}")


async def handle_command_as_agent(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    if update.effective_message is None:
        return
    if not is_allowed(update, context):
        await reject_unauthorized(update)
        return
    await run_agent_for_update(update, context, text_override=text, force_addressed=True)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.application.bot_data["agent"]
    if update.effective_chat is None or update.effective_message is None:
        return
    if not is_allowed(update, context):
        await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(await agent.reset(update.effective_chat.id))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_message is None or update.effective_user is None:
        return
    logging.getLogger(__name__).info(
        "Incoming message chat_id=%s chat_type=%s user_id=%s text=%r",
        update.effective_chat.id,
        update.effective_chat.type,
        update.effective_user.id,
        update.effective_message.text,
    )
    if update.effective_user.is_bot:
        if not is_allowed_bot(update, context):
            return
        addressed = await is_addressed_to_bot(update, context)
        if update.effective_chat.type != "private" and not addressed:
            return
        await run_agent_for_update(update, context, force_addressed=addressed)
        return
    if not is_allowed(update, context):
        if update.effective_chat.type == "private":
            await reject_unauthorized(update)
        return
    addressed = await is_addressed_to_bot(update, context)
    if update.effective_chat.type != "private" and not addressed:
        return
    text = strip_bot_mention(update.effective_message.text or "", context)
    await run_agent_for_update(
        update,
        context,
        text_override=text,
        force_addressed=addressed or update.effective_chat.type == "private",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.getLogger(__name__).exception("Unhandled Telegram update error", exc_info=context.error)


async def post_init(app: Application) -> None:
    storage: Storage = app.bot_data["storage"]
    await storage.init()
    agent: Agent = app.bot_data["agent"]
    await agent.start()
    app.create_task(reminder_loop(app, storage))


async def post_shutdown(app: Application) -> None:
    agent: Agent = app.bot_data["agent"]
    await agent.close()


# REVIEW: build_application 把大量配置塞进 bot_data 字典，用字符串 key 访问。
# 这是一种"穷人的依赖注入"——没有类型检查、没有自动补全、拼错 key 名只有运行时才发现。
# 比如 bot_data["alowed_user_ids"]（少打一个 l）不会有任何编译期错误。
#
# 更好的做法是定义一个 BotConfig dataclass，所有配置项都是带类型的字段，
# 然后 app.bot_data["config"] = BotConfig(...)，或者直接用 python-telegram-bot
# 的 ContextTypes 自定义 context 类型。
#
# 另外 app.bot_data["owner_user_ids"] = allowed_user_ids 把 "allowed" 和 "owner"
# 等同了，但概念上它们不一样——owner 应该只有一个（bot 的主人），allowed 可以有多个
# （被允许使用的人）。如果 allowed 列表里有朋友的 id，他们也会变成 "owner"，
# 导致日程数据错乱（日程是按 owner_user_id 隔离的）。
def build_application(
    token: str,
    user_whitelist_enabled: bool,
    allowed_user_ids: set[int],
    bot_to_bot_enabled: bool,
    allowed_bot_usernames: set[str],
    storage: Storage,
    llm: LLMClient,
) -> Application:
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["user_whitelist_enabled"] = user_whitelist_enabled
    app.bot_data["allowed_user_ids"] = allowed_user_ids
    app.bot_data["bot_to_bot_enabled"] = bot_to_bot_enabled
    app.bot_data["allowed_bot_usernames"] = allowed_bot_usernames
    app.bot_data["owner_user_ids"] = allowed_user_ids
    app.bot_data["timezone"] = os.getenv("HEROBOT_DEFAULT_TIMEZONE", "Asia/Shanghai")
    app.bot_data["storage"] = storage
    tool_registry = MCPToolRegistry(MCPToolConfig.from_env())
    app.bot_data["agent"] = Agent(
        storage=storage,
        llm=llm,
        tool_registry=tool_registry,
        persona=os.getenv("HEROBOT_PERSONA", ""),
        max_steps=int(os.getenv("HEROBOT_MAX_AGENT_STEPS", "8")),
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("contacts", contacts_command))
    app.add_handler(CommandHandler(["calendar", "calender"], calendar_command))
    app.add_handler(CommandHandler("availability", availability_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one HeroBot instance.")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the env file for this bot instance. Defaults to .env.",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file)
    if not env_file.exists():
        raise RuntimeError(f"Env file not found: {env_file}")
    load_dotenv(env_file, override=True)
    configure_logging()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("请先在 .env 中设置 TELEGRAM_BOT_TOKEN。")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("请先在 .env 中设置 OPENAI_API_KEY。")
    user_whitelist_enabled = parse_bool(os.getenv("TELEGRAM_ENABLE_USER_WHITELIST"), True)
    allowed_user_ids = parse_allowed_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS"))
    if user_whitelist_enabled and not allowed_user_ids:
        logging.getLogger(__name__).warning(
            "TELEGRAM_ALLOWED_USER_IDS is empty; only /whoami will be usable."
        )
    if not user_whitelist_enabled:
        logging.getLogger(__name__).warning("User whitelist is disabled; anyone can use this bot.")
    bot_to_bot_enabled = parse_bool(os.getenv("ENABLE_BOT_TO_BOT"), False)
    allowed_bot_usernames = parse_bot_usernames(os.getenv("TELEGRAM_ALLOWED_BOT_USERNAMES"))
    if bot_to_bot_enabled:
        logging.getLogger(__name__).warning(
            "Bot-to-bot mode is enabled; allowed bots=%s",
            ",".join(sorted(allowed_bot_usernames)) or "<any>",
        )

    llm = LLMClient(
        LLMConfig(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        )
    )
    storage = Storage(os.getenv("HEROBOT_DB_PATH", "data/herobot.sqlite3"))

    logging.getLogger(__name__).info("Starting HeroBot with polling using env_file=%s.", env_file)
    build_application(
        token,
        user_whitelist_enabled,
        allowed_user_ids,
        bot_to_bot_enabled,
        allowed_bot_usernames,
        storage,
        llm,
    ).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
