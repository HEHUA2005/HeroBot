from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from herobot.agent import Agent, AgentEvent
from herobot.core.auth import is_allowed_bot, is_allowed_user
from herobot.core.config import HeroBotConfig
from herobot.core.conversation_store import ConversationStore
from herobot.telegram.commands import (
    HELP_TEXT,
    START_TEXT,
    UNKNOWN_COMMAND_TEXT,
    alias_to_text,
    command_name,
)
from herobot.telegram.message_utils import extract_mentions, is_addressed_to_bot, strip_bot_mention
from herobot.telegram.platform_tools import TelegramPlatformTools


logger = logging.getLogger(__name__)


def register_handlers(app: Application, config: HeroBotConfig) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler(list(config.commands.aliases.keys()), command_alias))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)


def get_config(context: ContextTypes.DEFAULT_TYPE) -> HeroBotConfig:
    return context.application.bot_data["config"]


def owner_user_id(config: HeroBotConfig, fallback: int = 0) -> int:
    return next(iter(config.telegram.allowed_user_ids), fallback)


def instance_timezone(config: HeroBotConfig) -> str:
    return config.core.timezone


async def reject_unauthorized(update: Update) -> None:
    if update.effective_message is None:
        return
    await update.effective_message.reply_text(
        "这个 bot 已启用个人白名单。发送 /whoami 查看你的 Telegram user id，"
        "然后把它填入 TELEGRAM_ALLOWED_USER_IDS。"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if update.effective_message is None:
        return
    if not is_allowed_user(update, config):
        await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(START_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if update.effective_message is None:
        return
    if not is_allowed_user(update, config):
        await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def command_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    message = update.effective_message
    if message is None:
        return
    command = (message.text or "").split(maxsplit=1)[0]
    alias_text = alias_to_text(command, message.text or "", config.commands.aliases)
    if alias_text is None:
        await unknown_command(update, context)
        return
    await handle_command_as_agent(update, context, alias_text)


async def dispatch_text_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    name = command_name(text)
    if name is None:
        return False
    if name == "start":
        await start(update, context)
        return True
    if name == "help":
        await help_command(update, context)
        return True
    if name == "whoami":
        await whoami(update, context)
        return True
    if name == "chatid":
        await chatid(update, context)
        return True
    if name == "reset":
        await reset(update, context)
        return True

    config = get_config(context)
    command = text.split(maxsplit=1)[0]
    alias_text = alias_to_text(command, text, config.commands.aliases)
    if alias_text is not None:
        await handle_command_as_agent(update, context, alias_text)
        return True

    await unknown_command(update, context)
    return True


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if update.effective_message is None:
        return
    if not is_allowed_user(update, config):
        if update.effective_chat and update.effective_chat.type == "private":
            await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(UNKNOWN_COMMAND_TEXT)


def build_agent_event(
    update: Update,
    config: HeroBotConfig,
    text_override: str | None = None,
    force_addressed: bool = False,
    self_bot_id: int | None = None,
    self_bot_username: str | None = None,
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
        self_bot_id=self_bot_id,
        self_bot_username=self_bot_username,
        message_thread_id=getattr(message, "message_thread_id", None),
        reply_to_message_id=reply_to.message_id if reply_to else None,
        reply_to_sender_username=(
            reply_to.from_user.username
            if reply_to and reply_to.from_user and reply_to.from_user.username
            else None
        ),
        owner_user_id=owner_user_id(config, user.id),
        timezone=instance_timezone(config),
    )


async def run_agent_for_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text_override: str | None = None,
    force_addressed: bool = False,
) -> None:
    if update.effective_chat is None or update.effective_message is None:
        return
    config = get_config(context)
    event = build_agent_event(
        update,
        config,
        text_override,
        force_addressed,
        self_bot_id=context.bot.id,
        self_bot_username=context.bot.username,
    )
    if event is None:
        return
    agent: Agent = context.application.bot_data["agent"]
    storage: ConversationStore = context.application.bot_data["conversation_store"]
    platform_tools = TelegramPlatformTools(context, storage, event)
    await update.effective_chat.send_action(ChatAction.TYPING)
    try:
        await agent.handle_event(event, platform_tools)
    except Exception as exc:
        logger.exception("Agent event handling failed")
        await update.effective_message.reply_text(f"Agent 执行失败：{exc}")


async def handle_command_as_agent(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    config = get_config(context)
    if update.effective_message is None:
        return
    if not is_allowed_user(update, config):
        await reject_unauthorized(update)
        return
    await run_agent_for_update(update, context, text_override=text, force_addressed=True)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    agent: Agent = context.application.bot_data["agent"]
    if update.effective_chat is None or update.effective_message is None:
        return
    if not is_allowed_user(update, config):
        await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(await agent.reset(update.effective_chat.id))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_config(context)
    if update.effective_chat is None or update.effective_message is None or update.effective_user is None:
        return
    logger.info(
        "Incoming message chat_id=%s chat_type=%s user_id=%s text=%r",
        update.effective_chat.id,
        update.effective_chat.type,
        update.effective_user.id,
        update.effective_message.text,
    )
    addressed = await is_addressed_to_bot(update, context.bot.username, context.bot.id)
    if update.effective_user.is_bot:
        if not is_allowed_bot(update, context.bot.id, config):
            return
        if update.effective_chat.type != "private" and not addressed:
            return
        await run_agent_for_update(update, context, force_addressed=addressed)
        return
    if not is_allowed_user(update, config):
        if update.effective_chat.type == "private":
            await reject_unauthorized(update)
        return
    if update.effective_chat.type != "private" and not addressed:
        return
    text = strip_bot_mention(update.effective_message.text or "", context.bot.username)
    if await dispatch_text_command(update, context, text):
        return
    await run_agent_for_update(
        update,
        context,
        text_override=text,
        force_addressed=addressed or update.effective_chat.type == "private",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram update error", exc_info=context.error)
