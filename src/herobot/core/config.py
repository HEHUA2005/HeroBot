from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from herobot.mcp.config import MCPServerConfig


DEFAULT_COMMAND_ALIASES = {
    "contacts": "查看联系人通讯录",
    "calendar": "查看未来 7 天日程",
    "calender": "查看未来 7 天日程",
    "availability": "查看近期空闲时间",
    "pending": "查看待确认的约时间",
}


@dataclass(frozen=True)
class CoreConfig:
    timezone: str = "Asia/Shanghai"
    core_db_path: str = "data/herobot-core.sqlite3"
    max_agent_steps: int = 8
    persona: str = ""
    reminder_interval_seconds: int = 30


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    user_whitelist_enabled: bool = True
    allowed_user_ids: set[int] = field(default_factory=set)
    bot_to_bot_enabled: bool = False
    allowed_bot_usernames: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class LLMAppConfig:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1-mini"


@dataclass(frozen=True)
class CommandsConfig:
    aliases: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COMMAND_ALIASES))


@dataclass(frozen=True)
class HeroBotConfig:
    core: CoreConfig
    telegram: TelegramConfig
    llm: LLMAppConfig
    commands: CommandsConfig
    mcp_servers: list[MCPServerConfig]
    config_path: Path | None = None


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


def parse_username_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip().lstrip("@").lower() for item in value.split(",") if item.strip()}


def load_config(config_path: Path | None = None) -> HeroBotConfig:
    config_data = _read_toml(config_path)
    core_data = _section(config_data, "core")
    telegram_data = _section(config_data, "telegram")
    commands_data = _section(config_data, "commands")

    fallback_db_path = os.getenv("HEROBOT_DB_PATH")
    core_db_path = (
        os.getenv("HEROBOT_CORE_DB_PATH")
        or str(core_data.get("core_db_path") or "")
        or fallback_db_path
        or "data/herobot-core.sqlite3"
    )

    token = os.getenv("TELEGRAM_BOT_TOKEN") or str(telegram_data.get("token") or "")
    if not token:
        raise RuntimeError("请先在 .env 中设置 TELEGRAM_BOT_TOKEN。")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("请先在 .env 中设置 OPENAI_API_KEY。")

    aliases = dict(DEFAULT_COMMAND_ALIASES)
    aliases.update(_string_map(_section(commands_data, "aliases")))

    return HeroBotConfig(
        core=CoreConfig(
            timezone=os.getenv("HEROBOT_DEFAULT_TIMEZONE")
            or str(core_data.get("timezone") or "Asia/Shanghai"),
            core_db_path=core_db_path,
            max_agent_steps=int(
                os.getenv("HEROBOT_MAX_AGENT_STEPS")
                or core_data.get("max_agent_steps")
                or 8
            ),
            persona=os.getenv("HEROBOT_PERSONA") or str(core_data.get("persona") or ""),
            reminder_interval_seconds=int(
                os.getenv("HEROBOT_REMINDER_INTERVAL_SECONDS")
                or core_data.get("reminder_interval_seconds")
                or 30
            ),
        ),
        telegram=TelegramConfig(
            token=token,
            user_whitelist_enabled=parse_bool(
                os.getenv("TELEGRAM_ENABLE_USER_WHITELIST"),
                bool(telegram_data.get("user_whitelist_enabled", True)),
            ),
            allowed_user_ids=parse_allowed_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS")),
            bot_to_bot_enabled=parse_bool(
                os.getenv("ENABLE_BOT_TO_BOT"),
                bool(telegram_data.get("bot_to_bot_enabled", False)),
            ),
            allowed_bot_usernames=parse_username_set(os.getenv("TELEGRAM_ALLOWED_BOT_USERNAMES")),
        ),
        llm=LLMAppConfig(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            model=os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
        ),
        commands=CommandsConfig(aliases=aliases),
        mcp_servers=_load_mcp_servers(config_data, fallback_db_path),
        config_path=config_path if config_path and config_path.exists() else None,
    )


def _read_toml(config_path: Path | None) -> dict[str, Any]:
    if config_path is None:
        config_path = Path("herobot.toml")
    if not config_path.exists():
        return {}
    with config_path.open("rb") as file:
        return tomllib.load(file)


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, dict) else {}


def _string_map(data: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in data.items()}


def _load_mcp_servers(
    config_data: dict[str, Any], fallback_db_path: str | None
) -> list[MCPServerConfig]:
    raw_servers = _section(config_data, "mcp").get("servers")
    if isinstance(raw_servers, list) and raw_servers:
        return [_server_from_toml(item) for item in raw_servers if isinstance(item, dict)]
    return _default_mcp_servers(fallback_db_path)


def _server_from_toml(raw: dict[str, Any]) -> MCPServerConfig:
    return MCPServerConfig(
        name=str(raw["name"]),
        command=str(raw["command"]),
        args=[str(item) for item in raw.get("args", [])],
        enabled=bool(raw.get("enabled", True)),
        required=bool(raw.get("required", True)),
        cwd=str(raw["cwd"]) if raw.get("cwd") else None,
        env={str(key): str(value) for key, value in dict(raw.get("env", {})).items()},
        exposed_tools=[str(item) for item in raw.get("exposed_tools", [])],
        hidden_tools=[str(item) for item in raw.get("hidden_tools", [])],
    )


def _default_mcp_servers(fallback_db_path: str | None) -> list[MCPServerConfig]:
    notes_db_path = (
        os.getenv("HEROBOT_NOTES_DB_PATH") or fallback_db_path or "data/herobot-notes.sqlite3"
    )
    calendar_db_path = (
        os.getenv("HEROBOT_CALENDAR_DB_PATH")
        or fallback_db_path
        or "data/herobot-calendar.sqlite3"
    )
    return [
        MCPServerConfig(
            name="notes",
            command=sys.executable,
            args=["-m", "herobot.mcp.builtin.notes.server"],
            env={"HEROBOT_NOTES_DB_PATH": notes_db_path},
        ),
        MCPServerConfig(
            name="calendar",
            command=sys.executable,
            args=["-m", "herobot.mcp.builtin.calendar.server"],
            env={"HEROBOT_CALENDAR_DB_PATH": calendar_db_path},
            hidden_tools=["list_due_reminders", "mark_reminder_sent"],
        ),
    ]
