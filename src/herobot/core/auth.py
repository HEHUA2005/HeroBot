from __future__ import annotations

from telegram import Update

from herobot.core.config import HeroBotConfig


def is_allowed_user(update: Update, config: HeroBotConfig) -> bool:
    if not config.telegram.user_whitelist_enabled:
        return True
    if not config.telegram.allowed_user_ids:
        return False
    user = update.effective_user
    return user is not None and user.id in config.telegram.allowed_user_ids


def is_allowed_bot(update: Update, bot_id: int, config: HeroBotConfig) -> bool:
    if not config.telegram.bot_to_bot_enabled:
        return False
    user = update.effective_user
    if user is None or not user.is_bot:
        return False
    if user.id == bot_id:
        return False
    allowed_usernames = config.telegram.allowed_bot_usernames
    if not allowed_usernames:
        return True
    return (user.username or "").lower() in allowed_usernames
