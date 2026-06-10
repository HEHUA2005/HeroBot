# HeroBot

[![CI](https://github.com/HEHUA2005/HeroBot/actions/workflows/ci.yml/badge.svg)](https://github.com/HEHUA2005/HeroBot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
![MCP](https://img.shields.io/badge/MCP-stdio-green)

HeroBot is a Telegram-native personal assistant Agent. It keeps Telegram as a thin
message interface, runs a ReAct-style Agent runtime, and exposes business capabilities
through pluggable MCP servers.

```text
Telegram Core -> Agent Runtime -> MCP Host -> MCP Servers
```

HeroBot ships with local notes and calendar MCP servers, and can also load community
`mcpServers` JSON configs such as `mcp.json` / `.mcp.json`.

## Highlights

- Minimal Telegram core: receive updates, authorize users, build Agent events, send replies.
- ReAct Agent runtime: planning, tool calls, action ledger, finish guard, bounded max steps.
- Pluggable MCP host: builtin notes/calendar plus external stdio MCP servers.
- Personal assistant tools: notes, todos, reminders, contacts, calendar events, availability.
- Bot-to-bot scheduling: assistants can negotiate candidate times in a shared Telegram group.
- Local-first storage: SQLite databases for builtin business tools.
- CI-backed test suite: compile check and unittest on Python 3.12.

## Quick Start

Requirements:

- Python 3.12+
- pyenv / pyenv-virtualenv
- Telegram bot token from BotFather
- OpenAI-compatible LLM API
- SQLite

Install:

```bash
pyenv virtualenv 3.12.9 herobot
pyenv local herobot
pip install -e .

cp .env.example .env
cp herobot.toml.example herobot.toml
```

Edit `.env`:

```bash
TELEGRAM_BOT_TOKEN=replace-with-your-token
TELEGRAM_ENABLE_USER_WHITELIST=true
TELEGRAM_ALLOWED_USER_IDS=123456789

OPENAI_API_KEY=replace-with-your-api-key
OPENAI_BASE_URL=http://localhost:4000
OPENAI_MODEL=your-model

HEROBOT_DEFAULT_TIMEZONE=Asia/Shanghai
HEROBOT_MAX_AGENT_STEPS=8
```

Run:

```bash
herobot
```

If you run multiple bot instances:

```bash
herobot-multi --env-dir instances
```

`herobot-multi` starts one process per `instances/*.env` file and automatically pairs
`instances/foo.env` with `instances/foo.toml` when the TOML file exists.

## What It Can Do

The builtin MCP servers provide:

- Notes and todos: create todos, list todos, complete todos, save notes, search notes.
- Calendar: reminders, contacts, calendar events, availability, scheduling sessions.
- Scheduler: checks hidden reminder tools and sends Telegram reminders when due.

Example Telegram prompts:

```text
帮我记一下：护照放在书桌右边抽屉里
添加联系人 李雷 @HEHUAone_bot
我明天 14:00-16:00 要和 mentor 开会
@super666666_bot 帮我和李雷约明天下午 30 分钟
@super666666_bot 帮我问问 @HEHUAone_bot 能做啥
```

## MCP Extensions

HeroBot can load external MCP servers in two ways.

### Option 1: Community JSON Config

Put a community-style MCP config in `mcp.json` or `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "weather": {
      "command": "npx",
      "args": ["-y", "@dangahagan/weather-mcp@latest"]
    }
  }
}
```

VS Code-style `servers` is also supported:

```json
{
  "servers": {
    "weather": {
      "command": "npx",
      "args": ["-y", "@dangahagan/weather-mcp@latest"]
    }
  }
}
```

JSON-configured external servers are optional by default, so a broken community MCP
server will not prevent HeroBot from starting.

Use the example file as a starting point:

```bash
cp mcp.json.example mcp.json
```

Real `mcp.json` and `.mcp.json` files are ignored by git because they may contain local
paths or API keys.

### Option 2: HeroBot TOML Config

Use `herobot.toml` when you need HeroBot-specific fields such as `hidden_tools`,
`exposed_tools`, or `timeout_seconds`:

```toml
[[mcp.servers]]
name = "weather"
command = "python"
args = ["-m", "my_weather_mcp"]
enabled = true
required = false
exposed_tools = []
hidden_tools = []
timeout_seconds = 30
```

If a server name appears in both `herobot.toml` and `mcp.json`, the TOML server wins and
the JSON server is skipped with a warning.

Full guide: [Adding an MCP Server to HeroBot](doc/adding-mcp-server.md).

## Configuration

HeroBot uses `.env` for secrets and instance-level runtime settings, and `herobot.toml`
for structured app configuration.

Common `.env` settings:

```bash
TELEGRAM_BOT_TOKEN=replace-with-your-token
TELEGRAM_ENABLE_USER_WHITELIST=true
TELEGRAM_ALLOWED_USER_IDS=123456789

OPENAI_API_KEY=replace-with-your-api-key
OPENAI_BASE_URL=http://localhost:4000
OPENAI_MODEL=your-model
HEROBOT_LLM_TIMEOUT_SECONDS=60
HEROBOT_LLM_MAX_RETRIES=2

HEROBOT_DB_PATH=data/herobot.sqlite3
HEROBOT_PERSONA=
HEROBOT_DEFAULT_TIMEZONE=Asia/Shanghai
HEROBOT_MAX_AGENT_STEPS=8

ENABLE_BOT_TO_BOT=false
TELEGRAM_ALLOWED_BOT_USERNAMES=other_bot,review_bot
```

Minimal `herobot.toml`:

```toml
[commands.aliases]
calendar = "查看未来 7 天日程"
contacts = "查看联系人通讯录"

[[mcp.servers]]
name = "notes"
command = "herobot-mcp-notes"
enabled = true
required = true
timeout_seconds = 30
env = { HEROBOT_NOTES_DB_PATH = "data/herobot-notes.sqlite3" }

[[mcp.servers]]
name = "calendar"
command = "herobot-mcp-calendar"
enabled = true
required = true
hidden_tools = ["list_due_reminders", "mark_reminder_sent"]
timeout_seconds = 30
env = { HEROBOT_CALENDAR_DB_PATH = "data/herobot-calendar.sqlite3" }
```

If `herobot.toml` is missing, HeroBot starts with builtin notes/calendar defaults and
still appends external servers from `mcp.json` / `.mcp.json`.

## Architecture

```text
Telegram Update
  -> Telegram Adapter
  -> AgentEvent
  -> ReAct Agent Runtime
      -> Planner LLM
      -> ActionLedger
      -> FinishGuard
      -> Internal Platform Tools
         - send_telegram_message
         - send_telegram_messages
         - finish_task
      -> MCP Host
         -> herobot-mcp-notes
            - notes
            - todos
         -> herobot-mcp-calendar
            - contacts
            - reminders
            - calendar
            - availability
            - scheduling
         -> external MCP servers
```

Key boundaries:

- `bot.py`: CLI/bootstrap only.
- `core/`: configuration, authorization, conversation store, lifecycle, scheduler.
- `telegram/`: Telegram update handling, command aliases, platform tools.
- `agent/`: ReAct loop, planning, action ledger, finish guard, context construction.
- `mcp/registry.py`: generic stdio MCP host.
- `mcp/builtin/`: HeroBot-provided MCP servers. External MCP servers do not need to live in this repo.

## Development

Installed console scripts:

```text
herobot              # run one Telegram bot instance
herobot-multi        # run multiple instances from instances/*.env
herobot-mcp-notes    # notes/todos MCP server
herobot-mcp-calendar # calendar/contacts/reminders/scheduling MCP server
```

Useful checks:

```bash
python -m compileall src tests
python -m unittest discover -s tests
```

Project layout:

```text
src/herobot/
  bot.py
  multi.py
  llm.py
  core/
  telegram/
  agent/
  mcp/
    registry.py
    builtin/
      notes/
      calendar/
```

## Troubleshooting

- `409 Conflict`: the same Telegram bot token is already being polled by another process.
- External MCP server does not appear: check `herobot` startup logs for loaded JSON configs and skipped duplicate names.
- `npx` MCP server fails: confirm Node.js and `npx` are installed in the same environment that starts HeroBot.
- Bot does not respond in a group: mention the bot first, and check whitelist / bot-to-bot settings.

## Security Notes

- Do not commit `.env`, `instances/*.env`, SQLite databases, real tokens, or real `mcp.json` files.
- If a bot token was exposed, regenerate it in BotFather.
- Keep `TELEGRAM_ENABLE_USER_WHITELIST=true` for personal assistants.
- Use `TELEGRAM_ALLOWED_BOT_USERNAMES` to constrain bot-to-bot experiments.
- `send_telegram_message` can only send to the current chat, not arbitrary chat IDs.
