from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from herobot.agent import Agent
from herobot.bot2bot import (
    build_followup_prompt,
    build_collaboration_prompt,
    build_natural_followup_message,
    build_request_message,
    build_response_message,
    build_synthesis_prompt,
    choose_target_bot,
    clean_user_request,
    is_first_mentioned_bot,
    is_natural_bot_request,
    looks_like_delegation,
    natural_request_body,
    new_call_id,
    parse_bot_call,
    parse_bot_usernames,
    strip_envelope,
)
from herobot.llm import LLMClient, LLMConfig
from herobot.scheduling import (
    TimeWindow,
    display_window,
    extract_iso_windows,
    find_free_windows,
    from_iso,
    intersect_windows,
    parse_iso_text_window,
    parse_working_hours,
    to_utc_iso,
)
from herobot.scheduler import reminder_loop
from herobot.storage import Storage
from herobot.tools import ToolRunner


HELP_TEXT = """可用命令：
/start - 开始会话
/help - 查看帮助
/reset - 清空短期记忆
/whoami - 查看你的 Telegram user id
/chatid - 查看当前 chat id
/contacts - 查看联系人
/calendar - 查看近期日程
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


def is_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    owner_user_ids: set[int] = context.application.bot_data["owner_user_ids"]
    user = update.effective_user
    return user is not None and (not owner_user_ids or user.id in owner_user_ids)


def owner_user_id(context: ContextTypes.DEFAULT_TYPE, fallback: int = 0) -> int:
    owner_user_ids: set[int] = context.application.bot_data["owner_user_ids"]
    return next(iter(owner_user_ids), fallback)


def instance_timezone(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.application.bot_data["timezone"]


def working_hours(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.application.bot_data["working_hours"]


def default_reminder_minutes(context: ContextTypes.DEFAULT_TYPE) -> int:
    return context.application.bot_data["default_reminder_minutes"]


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


def format_slots(slots: list[TimeWindow], timezone_name: str) -> str:
    if not slots:
        return "没有找到可用时间。"
    return "\n".join(
        f"{index}. {display_window(to_utc_iso(slot.start), to_utc_iso(slot.end), timezone_name)}"
        for index, slot in enumerate(slots, start=1)
    )


async def owner_events_for_window(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, window: TimeWindow
) -> list[dict]:
    storage: Storage = context.application.bot_data["storage"]
    return await storage.list_calendar_events(user_id, to_utc_iso(window.start), to_utc_iso(window.end))


async def find_owner_free_slots(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, window: TimeWindow, duration_minutes: int
) -> list[TimeWindow]:
    events = await owner_events_for_window(context, user_id, window)
    return find_free_windows(events, window, duration_minutes, limit=3)


async def maybe_delegate_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.application.bot_data["bot_to_bot_enabled"]:
        return False
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or chat.type not in {"group", "supergroup"}:
        return False
    text = message.text or ""
    own_username = context.bot.username
    if not looks_like_delegation(text, own_username):
        return False

    target_username = choose_target_bot(text, own_username)
    if not target_username:
        return False
    allowed_usernames: set[str] = context.application.bot_data["allowed_bot_usernames"]
    if allowed_usernames and target_username.lower() not in allowed_usernames:
        await message.reply_text(f"@{target_username} 不在 bot-to-bot 白名单里。")
        return True

    call_id = new_call_id()
    max_depth: int = context.application.bot_data["bot_to_bot_max_depth"]
    request = clean_user_request(text, target_username, own_username)
    outbound = build_request_message(
        target_username=target_username,
        from_username=own_username or "unknown_bot",
        call_id=call_id,
        request=request,
        max_depth=max_depth,
    )
    sent = await context.bot.send_message(chat_id=chat.id, text=outbound)
    context.application.bot_data["pending_bot_calls"][call_id] = {
        "chat_id": chat.id,
        "message_id": message.message_id,
        "last_outbound_message_id": sent.message_id,
        "depth": 1,
        "target_username": target_username,
        "request": request,
    }
    context.application.bot_data["pending_bot_replies"][sent.message_id] = call_id
    await message.reply_text(
        f"已邀请 @{target_username} 参与，我会根据它的回复继续讨论。"
    )
    logging.getLogger(__name__).info(
        "Delegated bot-to-bot call_id=%s target=%s message_id=%s",
        call_id,
        target_username,
        sent.message_id,
    )
    return True


async def dispatch_new_scheduling_session(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    previous_session_id: int | None,
) -> bool:
    if not context.application.bot_data["bot_to_bot_enabled"]:
        return False
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    if chat is None or message is None or user is None or chat.type not in {"group", "supergroup"}:
        return False
    storage: Storage = context.application.bot_data["storage"]
    session = await storage.latest_pending_session(owner_user_id(context, user.id))
    if session is None:
        return False
    if previous_session_id is not None and session["id"] <= previous_session_id:
        return False
    if session["chat_id"] != chat.id:
        return False
    owner_name = context.application.bot_data["owner_name"]
    outbound = (
        f"@{session['contact_bot_username']}\n"
        f"我是 {owner_name} 的助理，想帮 {owner_name} 和 {session['contact_name']} 协调时间：{session['request_text']}。\n"
        f"时长约 {session['duration_minutes']} 分钟，时间范围是 "
        f"{display_window(session['window_start'], session['window_end'], instance_timezone(context))}。\n"
        "请只回复你主人可用的 1-3 个时间段，格式类似：可用时间：2026-06-04 14:00-14:30；2026-06-04 16:00-16:30。"
    )
    sent = await context.bot.send_message(chat_id=chat.id, text=outbound)
    context.application.bot_data["pending_schedule_replies"][sent.message_id] = session["id"]
    await message.reply_text(f"我去问 @{session['contact_bot_username']} 的可用时间。")
    return True


async def maybe_handle_bot_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_allowed_bot(update, context):
        return False
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    if chat is None or message is None or user is None:
        return True
    text = message.text or ""
    replied_message_id = message.reply_to_message.message_id if message.reply_to_message else None
    schedule_session_id = (
        context.application.bot_data["pending_schedule_replies"].pop(replied_message_id, None)
        if replied_message_id is not None
        else None
    )
    if schedule_session_id is not None:
        storage: Storage = context.application.bot_data["storage"]
        session = await storage.get_scheduling_session(schedule_session_id)
        if session is None:
            return True
        peer_slots = extract_iso_windows(text, instance_timezone(context))
        window = TimeWindow(
            start=from_iso(session["window_start"], instance_timezone(context)),
            end=from_iso(session["window_end"], instance_timezone(context)),
        )
        own_slots = await find_owner_free_slots(
            context, session["owner_user_id"], window, int(session["duration_minutes"])
        )
        candidates = intersect_windows(own_slots, peer_slots, int(session["duration_minutes"]), limit=3)
        candidate_payload = [
            {"start_at": to_utc_iso(slot.start), "end_at": to_utc_iso(slot.end)}
            for slot in candidates
        ]
        await storage.update_scheduling_candidates(session["id"], candidate_payload)
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                f"我和 @{user.username} 对了一下空闲时间，候选如下：\n"
                f"{format_slots(candidates, instance_timezone(context))}\n"
                "如果合适，请回复：确认第 1 个时间"
            ),
            reply_to_message_id=message.message_id,
        )
        return True

    if is_natural_bot_request(text, context.bot.username) and "协调时间" in text:
        agent: Agent = context.application.bot_data["agent"]
        request = natural_request_body(text, context.bot.username)
        prompt = (
            "这是另一个私人助理发来的约时间请求。"
            "请理解其中的时间范围和时长，调用 find_availability 查询你主人的空闲时间。"
            "回复时只给空闲时间段，不要暴露具体日程内容。"
            "必须使用这种格式：可用时间：2026-06-04 14:00-14:30；2026-06-04 16:00-16:30。"
            f"\n\n请求：{request}"
        )
        await chat.send_action(ChatAction.TYPING)
        reply = await agent.respond(chat.id, user.id, prompt)
        await context.bot.send_message(
            chat_id=chat.id,
            text=reply,
            reply_to_message_id=message.message_id,
        )
        return True

    pending_call_id = (
        context.application.bot_data["pending_bot_replies"].pop(replied_message_id, None)
        if replied_message_id is not None
        else None
    )
    if pending_call_id is not None:
        pending = context.application.bot_data["pending_bot_calls"].get(pending_call_id)
        if pending is None:
            return True
        agent: Agent = context.application.bot_data["agent"]
        peer_response = strip_bot_mention(strip_envelope(text), context)
        next_depth = int(pending.get("depth", 1)) + 2
        max_depth: int = context.application.bot_data["bot_to_bot_max_depth"]
        await chat.send_action(ChatAction.TYPING)
        if next_depth < max_depth:
            followup_prompt = build_followup_prompt(
                peer_username=user.username or "peer_bot",
                original_request=pending["request"],
                peer_response=peer_response,
            )
            followup = await agent.respond(chat.id, user.id, followup_prompt)
            pending.setdefault("turns", []).append(
                {"from": user.username or "peer_bot", "text": peer_response}
            )
            pending["turns"].append({"from": context.bot.username or "unknown_bot", "text": followup})
            pending["depth"] = next_depth
            outbound = build_natural_followup_message(pending["target_username"], followup)
            sent = await context.bot.send_message(
                chat_id=chat.id,
                text=outbound,
                reply_to_message_id=message.message_id,
            )
            pending["last_outbound_message_id"] = sent.message_id
            context.application.bot_data["pending_bot_replies"][sent.message_id] = pending_call_id
            return True

        context.application.bot_data["pending_bot_calls"].pop(pending_call_id, None)
        turns = pending.get("turns", [])
        discussion = "\n\n".join(f"{turn['from']}：{turn['text']}" for turn in turns)
        final_peer_response = f"{discussion}\n\n{user.username or 'peer_bot'}：{peer_response}".strip()
        prompt = build_synthesis_prompt(
            peer_username=user.username or "peer_bot",
            original_request=pending["request"],
            peer_response=final_peer_response,
        )
        reply = await agent.respond(chat.id, user.id, prompt)
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"综合 @{user.username} 的观点：\n{reply}",
            reply_to_message_id=pending["message_id"],
        )
        return True

    call = parse_bot_call(text)
    if call is None:
        if not is_natural_bot_request(text, context.bot.username):
            return False
        agent: Agent = context.application.bot_data["agent"]
        request = natural_request_body(text, context.bot.username)
        await chat.send_action(ChatAction.TYPING)
        reply = await agent.respond(chat.id, user.id, build_collaboration_prompt(request))
        await context.bot.send_message(
            chat_id=chat.id,
            text=build_response_message(
                target_username=user.username or "peer_bot",
                from_username=context.bot.username or "unknown_bot",
                call_id="",
                depth=0,
                max_depth=0,
                response=reply,
            ),
            reply_to_message_id=message.message_id,
        )
        return True
    if call.purpose == "response":
        pending = context.application.bot_data["pending_bot_calls"].get(call.call_id)
        if pending is None:
            logging.getLogger(__name__).info(
                "Observed bot-to-bot response call_id=%s from=%s without pending call",
                call.call_id,
                user.username,
            )
            return True
        agent: Agent = context.application.bot_data["agent"]
        peer_response = strip_bot_mention(strip_envelope(text), context)
        await chat.send_action(ChatAction.TYPING)
        if call.depth + 1 < call.max_depth:
            followup_prompt = build_followup_prompt(
                peer_username=user.username or call.from_bot,
                original_request=pending["request"],
                peer_response=peer_response,
            )
            followup = await agent.respond(chat.id, user.id, followup_prompt)
            pending.setdefault("turns", []).append(
                {"from": user.username or call.from_bot, "text": peer_response}
            )
            pending["turns"].append({"from": context.bot.username or "unknown_bot", "text": followup})
            outbound = build_request_message(
                target_username=pending["target_username"],
                from_username=context.bot.username or "unknown_bot",
                call_id=call.call_id,
                request=(
                    f"原始问题：{pending['request']}\n\n"
                    f"我对你上一轮的回应：{followup}"
                ),
                max_depth=call.max_depth,
                depth=call.depth + 1,
            )
            sent = await context.bot.send_message(
                chat_id=chat.id,
                text=outbound,
                reply_to_message_id=message.message_id,
            )
            pending["last_outbound_message_id"] = sent.message_id
            context.application.bot_data["pending_bot_replies"][sent.message_id] = call.call_id
            return True

        context.application.bot_data["pending_bot_calls"].pop(call.call_id, None)
        turns = pending.get("turns", [])
        discussion = "\n\n".join(f"{turn['from']}：{turn['text']}" for turn in turns)
        final_peer_response = f"{discussion}\n\n{user.username or call.from_bot}：{peer_response}".strip()
        prompt = build_synthesis_prompt(
            peer_username=user.username or call.from_bot,
            original_request=pending["request"],
            peer_response=final_peer_response,
        )
        reply = await agent.respond(chat.id, user.id, prompt)
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"综合 @{user.username} 的观点：\n{reply}",
            reply_to_message_id=pending["message_id"],
        )
        return True
    if call.purpose != "request":
        logging.getLogger(__name__).info(
            "Observed bot-to-bot %s call_id=%s from=%s",
            call.purpose,
            call.call_id,
            user.username,
        )
        return True
    if call.depth >= call.max_depth:
        await message.reply_text(f"call_id={call.call_id} 已达到最大 bot-to-bot 深度。")
        return True

    agent: Agent = context.application.bot_data["agent"]
    request = strip_bot_mention(strip_envelope(text), context)
    await chat.send_action(ChatAction.TYPING)
    reply = await agent.respond(chat.id, user.id, build_collaboration_prompt(request))
    response = build_response_message(
        target_username=call.from_bot,
        from_username=context.bot.username or "unknown_bot",
        call_id=call.call_id,
        depth=call.depth + 1,
        max_depth=call.max_depth,
        response=reply,
    )
    await context.bot.send_message(chat_id=chat.id, text=response, reply_to_message_id=message.message_id)
    return True


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
        "HeroBot 个人助理已启动。发送 /help 看示例，或直接告诉我你要记录、提醒或查询什么。"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not is_allowed(update, context):
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


async def contacts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not is_owner(update, context):
        return
    storage: Storage = context.application.bot_data["storage"]
    contacts = await storage.list_contacts()
    if not contacts:
        await update.effective_message.reply_text("还没有联系人。可以说：添加联系人 李雷 @lilei_bot")
        return
    await update.effective_message.reply_text(
        "\n".join(f"{item['name']}: @{item['bot_username']}" for item in contacts)
    )


async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not is_owner(update, context):
        return
    tz = instance_timezone(context)
    now = datetime.now(ZoneInfo(tz))
    window = TimeWindow(now, now + timedelta(days=7))
    storage: Storage = context.application.bot_data["storage"]
    events = await storage.list_calendar_events(
        owner_user_id(context, update.effective_user.id if update.effective_user else 0),
        to_utc_iso(window.start),
        to_utc_iso(window.end),
    )
    if not events:
        await update.effective_message.reply_text("未来 7 天没有日程。")
        return
    await update.effective_message.reply_text(
        "\n".join(
            f"{event['id']}. {display_window(event['start_at'], event['end_at'], tz)} {event['title']}"
            for event in events
        )
    )


async def availability_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not is_owner(update, context):
        return
    text = update.effective_message.text or ""
    window = parse_iso_text_window(text, instance_timezone(context))
    if window is None:
        tomorrow = datetime.now(ZoneInfo(instance_timezone(context))).date() + timedelta(days=1)
        window = parse_working_hours(working_hours(context), ZoneInfo(instance_timezone(context)), tomorrow)
    slots = await find_owner_free_slots(
        context,
        owner_user_id(context, update.effective_user.id if update.effective_user else 0),
        window,
        30,
    )
    await update.effective_message.reply_text(format_slots(slots, instance_timezone(context)))


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if not is_owner(update, context):
        return
    storage: Storage = context.application.bot_data["storage"]
    session = await storage.latest_pending_session(
        owner_user_id(context, update.effective_user.id if update.effective_user else 0)
    )
    if session is None:
        await update.effective_message.reply_text("没有待确认的约时间。")
        return
    candidates = [
        TimeWindow(from_iso(item["start_at"], instance_timezone(context)), from_iso(item["end_at"], instance_timezone(context)))
        for item in session["candidates"]
    ]
    await update.effective_message.reply_text(
        f"待确认：和 {session['contact_name']} 约 {session['duration_minutes']} 分钟\n"
        f"{format_slots(candidates, instance_timezone(context))}"
    )


async def maybe_confirm_scheduling(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return False
    text = message.text or ""
    if "取消这次约时间" in text:
        if not is_owner(update, context):
            return True
        storage: Storage = context.application.bot_data["storage"]
        session = await storage.latest_pending_session(owner_user_id(context, user.id))
        if session:
            await storage.set_scheduling_status(session["id"], "cancelled")
        await message.reply_text("已取消这次约时间。")
        return True
    import re
    match = re.search(r"确认第\s*(\d+)\s*个时间", text)
    if not match:
        return False
    if not is_owner(update, context):
        await message.reply_text("只有主人可以确认日程。")
        return True
    storage: Storage = context.application.bot_data["storage"]
    session = await storage.latest_pending_session(owner_user_id(context, user.id))
    if session is None or not session["candidates"]:
        await message.reply_text("没有可确认的候选时间。")
        return True
    index = int(match.group(1)) - 1
    if index < 0 or index >= len(session["candidates"]):
        await message.reply_text("候选序号不对。")
        return True
    candidate = session["candidates"][index]
    title = f"和 {session['contact_name']} 约时间"
    reminder_at = None
    reminder_minutes = default_reminder_minutes(context)
    if reminder_minutes > 0:
        reminder_at = to_utc_iso(from_iso(candidate["start_at"]) - timedelta(minutes=reminder_minutes))
    event = await storage.create_calendar_event(
        owner_user_id(context, user.id),
        title,
        candidate["start_at"],
        candidate["end_at"],
        session["request_text"],
        reminder_at,
    )
    if reminder_at:
        await storage.create_reminder(chat.id, user.id, f"日程提醒：{title}", reminder_at)
    await storage.set_scheduling_status(session["id"], "confirmed")
    await message.reply_text(
        f"已确认：{display_window(event['start_at'], event['end_at'], instance_timezone(context))}，并写入日程。"
    )
    return True


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.application.bot_data["agent"]
    if update.effective_chat is None or update.effective_message is None:
        return
    if not is_allowed(update, context):
        await reject_unauthorized(update)
        return
    await update.effective_message.reply_text(await agent.reset(update.effective_chat.id))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: Agent = context.application.bot_data["agent"]
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
        await maybe_handle_bot_to_bot(update, context)
        return
    if not is_allowed(update, context):
        if update.effective_chat.type == "private":
            await reject_unauthorized(update)
        return
    if await maybe_confirm_scheduling(update, context):
        return
    if await maybe_delegate_to_bot(update, context):
        return
    if not await is_addressed_to_bot(update, context):
        return

    storage: Storage = context.application.bot_data["storage"]
    previous_session = await storage.latest_pending_session(
        owner_user_id(context, update.effective_user.id)
    )
    previous_session_id = previous_session["id"] if previous_session else None
    text = strip_bot_mention(update.effective_message.text or "", context)
    await update.effective_chat.send_action(ChatAction.TYPING)
    reply = await agent.respond(update.effective_chat.id, update.effective_user.id, text)
    await update.effective_message.reply_text(reply)
    await dispatch_new_scheduling_session(update, context, previous_session_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.getLogger(__name__).exception("Unhandled Telegram update error", exc_info=context.error)


async def post_init(app: Application) -> None:
    storage: Storage = app.bot_data["storage"]
    await storage.init()
    app.create_task(reminder_loop(app, storage))


def build_application(
    token: str,
    user_whitelist_enabled: bool,
    allowed_user_ids: set[int],
    bot_to_bot_enabled: bool,
    allowed_bot_usernames: set[str],
    bot_to_bot_max_depth: int,
    storage: Storage,
    llm: LLMClient,
) -> Application:
    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["user_whitelist_enabled"] = user_whitelist_enabled
    app.bot_data["allowed_user_ids"] = allowed_user_ids
    app.bot_data["bot_to_bot_enabled"] = bot_to_bot_enabled
    app.bot_data["allowed_bot_usernames"] = allowed_bot_usernames
    app.bot_data["bot_to_bot_max_depth"] = bot_to_bot_max_depth
    app.bot_data["pending_bot_calls"] = {}
    app.bot_data["pending_bot_replies"] = {}
    app.bot_data["pending_schedule_replies"] = {}
    app.bot_data["owner_user_ids"] = allowed_user_ids
    app.bot_data["owner_name"] = os.getenv("HEROBOT_OWNER_NAME", "主人")
    app.bot_data["timezone"] = os.getenv("HEROBOT_DEFAULT_TIMEZONE", "Asia/Shanghai")
    app.bot_data["working_hours"] = os.getenv("HEROBOT_WORKING_HOURS", "09:00-18:00")
    app.bot_data["default_reminder_minutes"] = int(
        os.getenv("HEROBOT_DEFAULT_REMINDER_MINUTES", "10")
    )
    app.bot_data["storage"] = storage
    app.bot_data["agent"] = Agent(
        storage=storage,
        llm=llm,
        tools=ToolRunner(storage, timezone=app.bot_data["timezone"]),
        persona=os.getenv("HEROBOT_PERSONA", ""),
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("contacts", contacts_command))
    app.add_handler(CommandHandler("calendar", calendar_command))
    app.add_handler(CommandHandler("availability", availability_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("reset", reset))
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
    bot_to_bot_max_depth = int(os.getenv("BOT_TO_BOT_MAX_DEPTH", "3"))
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
        bot_to_bot_max_depth,
        storage,
        llm,
    ).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
