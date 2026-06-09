from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update

from herobot.core.app import build_application
from herobot.core.config import load_config
from herobot.core.logging import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one HeroBot instance.")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the env file for this bot instance. Defaults to .env.",
    )
    parser.add_argument(
        "--config",
        default="herobot.toml",
        help="Path to HeroBot TOML config. Missing config uses built-in defaults.",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file)
    if not env_file.exists():
        raise RuntimeError(f"Env file not found: {env_file}")
    load_dotenv(env_file, override=True)

    config = load_config(Path(args.config))
    configure_logging(os.getenv("LOG_LEVEL", "INFO"))

    if config.telegram.user_whitelist_enabled and not config.telegram.allowed_user_ids:
        logging.getLogger(__name__).warning(
            "TELEGRAM_ALLOWED_USER_IDS is empty; only /whoami will be usable."
        )
    if not config.telegram.user_whitelist_enabled:
        logging.getLogger(__name__).warning("User whitelist is disabled; anyone can use this bot.")
    if config.telegram.bot_to_bot_enabled:
        logging.getLogger(__name__).warning(
            "Bot-to-bot mode is enabled; allowed bots=%s",
            ",".join(sorted(config.telegram.allowed_bot_usernames)) or "<any>",
        )

    logging.getLogger(__name__).info(
        "Starting HeroBot with polling using env_file=%s config=%s.",
        env_file,
        config.config_path or "<defaults>",
    )
    build_application(config).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
