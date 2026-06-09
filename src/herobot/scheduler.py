from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram.ext import Application

from herobot.storage import Storage


logger = logging.getLogger(__name__)


# REVIEW: reminder_loop 有几个从用户角度值得思考的问题：
#
# 1. 提醒精度问题：轮询间隔 30 秒意味着提醒最多延迟 30 秒。对于"提醒我 1 分钟后"
#    这种场景，30 秒延迟太大了。用户设了 14:00 的提醒，可能 14:00:29 才收到。
#    可以用更短的间隔（比如 10 秒），或者用 asyncio 精确调度。
#
# 2. 发送失败问题：如果 send_message 成功但 mark_reminder_sent 失败（比如数据库写入失败），
#    下一轮会再次发送同一个提醒——用户会收到重复通知。虽然概率很低，但如果用户设了
#    一个重要提醒，连续收到两次会很困惑。应该用事务把"发送"和"标记已发送"绑在一起。
#
# 3. 提醒格式太简单：只有 "提醒：{content}"。缺少时间信息、缺少关联的日程上下文。
#    用户如果设了多个提醒，收到"提醒：开会"时不知道是哪个会。
async def reminder_loop(app: Application, storage: Storage, interval_seconds: int = 30) -> None:
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            for reminder in await storage.due_reminders(now_iso):
                await app.bot.send_message(
                    chat_id=reminder.chat_id,
                    text=f"提醒：{reminder.content}",
                )
                await storage.mark_reminder_sent(reminder.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder loop failed")
        await asyncio.sleep(interval_seconds)
