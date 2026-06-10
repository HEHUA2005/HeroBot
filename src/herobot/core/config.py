from __future__ import annotations

import json
import logging
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from herobot.mcp.config import MCPServerConfig


logger = logging.getLogger(__name__)


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
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 0.5


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
    mcp_json_config_paths: tuple[Path, ...] = ()
    mcp_json_server_names: tuple[str, ...] = ()


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
    config_dir = _config_dir(config_path)
    mcp_json_configs = _read_mcp_json_configs(config_dir)
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
    mcp_servers, mcp_json_server_names = _load_mcp_servers(
        config_data,
        fallback_db_path,
        mcp_json_configs,
    )

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
            timeout_seconds=float(os.getenv("HEROBOT_LLM_TIMEOUT_SECONDS") or 60),
            max_retries=int(os.getenv("HEROBOT_LLM_MAX_RETRIES") or 2),
            retry_base_delay_seconds=float(
                os.getenv("HEROBOT_LLM_RETRY_BASE_DELAY_SECONDS") or 0.5
            ),
        ),
        commands=CommandsConfig(aliases=aliases),
        mcp_servers=mcp_servers,
        config_path=config_path if config_path and config_path.exists() else None,
        mcp_json_config_paths=tuple(path for path, _data in mcp_json_configs),
        mcp_json_server_names=tuple(mcp_json_server_names),
    )


def _read_toml(config_path: Path | None) -> dict[str, Any]:
    if config_path is None:
        config_path = Path("herobot.toml")
    if not config_path.exists():
        return {}
    with config_path.open("rb") as file:
        return tomllib.load(file)


def _config_dir(config_path: Path | None) -> Path:
    if config_path is None:
        config_path = Path("herobot.toml")
    parent = config_path.parent
    return parent if str(parent) else Path(".")


def _read_mcp_json_configs(config_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    configs: list[tuple[Path, dict[str, Any]]] = []
    for filename in ("mcp.json", ".mcp.json"):
        path = config_dir / filename
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid MCP JSON config {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Invalid MCP JSON config {path}: top-level value must be an object")
        configs.append((path, data))
    return configs


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, dict) else {}


def _string_map(data: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in data.items()}


def _load_mcp_servers(
    config_data: dict[str, Any],
    fallback_db_path: str | None,
    mcp_json_configs: list[tuple[Path, dict[str, Any]]],
) -> tuple[list[MCPServerConfig], list[str]]:
    raw_servers = _section(config_data, "mcp").get("servers")
    if isinstance(raw_servers, list) and raw_servers:
        servers = [_server_from_toml(item) for item in raw_servers if isinstance(item, dict)]
    else:
        servers = _default_mcp_servers(fallback_db_path)
    return _merge_json_mcp_servers(servers, mcp_json_configs)


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
        timeout_seconds=float(raw.get("timeout_seconds", 30)),
    )


def _merge_json_mcp_servers(
    servers: list[MCPServerConfig],
    mcp_json_configs: list[tuple[Path, dict[str, Any]]],
) -> tuple[list[MCPServerConfig], list[str]]:
    merged = list(servers)
    existing_names = {server.name for server in merged}
    appended_names: list[str] = []
    for path, data in mcp_json_configs:
        for server in _servers_from_mcp_json(data, path):
            if server.name in existing_names:
                logger.warning(
                    "Skipping MCP JSON server %s from %s because a server with that name "
                    "is already configured.",
                    server.name,
                    path,
                )
                continue
            merged.append(server)
            existing_names.add(server.name)
            appended_names.append(server.name)
    return merged, appended_names


def _servers_from_mcp_json(data: dict[str, Any], path: Path) -> list[MCPServerConfig]:
    raw_servers = data.get("mcpServers")
    if not isinstance(raw_servers, dict):
        raw_servers = data.get("servers")
    if raw_servers is None:
        return []
    if not isinstance(raw_servers, dict):
        raise RuntimeError(f"Invalid MCP JSON config {path}: mcpServers/servers must be an object")

    servers: list[MCPServerConfig] = []
    for name, raw in raw_servers.items():
        if not isinstance(raw, dict):
            logger.warning("Skipping MCP JSON server %s from %s: value must be an object.", name, path)
            continue
        enabled = bool(raw.get("enabled", True))
        disabled = bool(raw.get("disabled", False))
        if disabled or not enabled:
            continue
        command = str(raw.get("command") or "").strip()
        if not command:
            logger.warning("Skipping MCP JSON server %s from %s: command is required.", name, path)
            continue
        servers.append(
            MCPServerConfig(
                name=str(name),
                command=command,
                args=[str(item) for item in raw.get("args", [])],
                enabled=True,
                required=bool(raw.get("required", False)),
                cwd=str(raw["cwd"]) if raw.get("cwd") else None,
                env={str(key): str(value) for key, value in dict(raw.get("env", {})).items()},
                exposed_tools=[str(item) for item in raw.get("exposed_tools", [])],
                hidden_tools=[str(item) for item in raw.get("hidden_tools", [])],
                timeout_seconds=float(raw.get("timeout_seconds", 30)),
            )
        )
    return servers


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
