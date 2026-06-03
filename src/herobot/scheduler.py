from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram.ext import Application

from herobot.storage import Storage


logger = logging.getLogger(__name__)


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
