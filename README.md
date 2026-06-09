# HeroBot

HeroBot 是一个运行在 Telegram 上的个人助理 Agent。现在的定位是：

```text
Telegram Core -> Agent Runtime -> MCP Host -> MCP Servers
```

Telegram 只负责接收和发送消息；Agent 负责 ReAct-style 决策；业务能力由 MCP server 插拔提供。HeroBot 自带 notes 和 calendar 两个 MCP server，也可以通过 `herobot.toml` 接入外部 MCP server。

## Architecture

```text
Telegram Update
  -> Telegram Adapter
  -> AgentEvent
  -> ReAct Agent Runtime
      -> Planner LLM
         - TaskFrame
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

关键边界：

- `bot.py` 只负责 CLI/bootstrap。
- `core/` 负责配置、鉴权、最小 conversation store、app lifecycle 和 scheduler。
- `telegram/` 负责 Telegram update 处理、命令 alias、消息发送平台工具。
- `agent/` 负责 Planner、ReAct loop、ActionLedger、FinishGuard 和上下文构造。
- `mcp/registry.py` 是通用 MCP Host，支持多个 stdio MCP server。
- `mcp/builtin/` 是 HeroBot 自带 MCP server；外部 MCP server 不需要放进本 repo。

## Requirements

- Python 3.12+
- pyenv / pyenv-virtualenv
- Telegram BotFather 创建的 bot token
- OpenAI-compatible LLM API
- SQLite

## Installation

```bash
pyenv virtualenv 3.12.9 herobot
pyenv local herobot
pip install -e .
cp .env.example .env
cp herobot.toml.example herobot.toml
```

安装后会注册：

```text
herobot              # 启动单个 Telegram bot 实例
herobot-multi        # 从 instances/*.env 批量启动多个实例
herobot-mcp-notes    # notes/todos MCP server
herobot-mcp-calendar # calendar/contacts/reminders/scheduling MCP server
```

## Configuration

`.env` 放 token、LLM key、实例人格和 DB fallback：

```bash
TELEGRAM_BOT_TOKEN=replace-with-your-token
TELEGRAM_ENABLE_USER_WHITELIST=true
TELEGRAM_ALLOWED_USER_IDS=123456789

OPENAI_API_KEY=replace-with-your-api-key
OPENAI_BASE_URL=http://localhost:4000
OPENAI_MODEL=your-model

HEROBOT_DB_PATH=data/herobot.sqlite3
HEROBOT_PERSONA=
HEROBOT_DEFAULT_TIMEZONE=Asia/Shanghai
HEROBOT_MAX_AGENT_STEPS=8

ENABLE_BOT_TO_BOT=false
TELEGRAM_ALLOWED_BOT_USERNAMES=other_bot,review_bot
```

`herobot.toml` 放 MCP server 和命令 alias：

```toml
[commands.aliases]
calendar = "查看未来 7 天日程"
contacts = "查看联系人通讯录"

[[mcp.servers]]
name = "notes"
command = "herobot-mcp-notes"
enabled = true
required = true
env = { HEROBOT_NOTES_DB_PATH = "data/herobot-notes.sqlite3" }

[[mcp.servers]]
name = "calendar"
command = "herobot-mcp-calendar"
enabled = true
required = true
hidden_tools = ["list_due_reminders", "mark_reminder_sent"]
env = { HEROBOT_CALENDAR_DB_PATH = "data/herobot-calendar.sqlite3" }
```

外部 MCP server 插拔示例：

```toml
[[mcp.servers]]
name = "weather"
command = "python"
args = ["-m", "my_weather_mcp"]
enabled = true
required = false
exposed_tools = []
hidden_tools = []
```

如果没有 `herobot.toml`，HeroBot 会使用内置默认配置并启动 notes/calendar 两个 builtin MCP server。旧 `HEROBOT_DB_PATH` 会作为 core、notes、calendar 的兼容 fallback。

## Running

启动单个实例：

```bash
herobot
```

指定 env 和 config：

```bash
herobot --env-file instances/super666666.env --config instances/super666666.toml
```

批量启动：

```bash
herobot-multi --env-dir instances
```

`herobot-multi` 会自动为 `instances/foo.env` 配对 `instances/foo.toml`，如果 TOML 不存在则使用默认配置。

同一个 Telegram bot token 只能有一个 polling 进程。如果看到 `409 Conflict`，说明同一个 token 被多个进程同时使用。

## Capabilities

内置 MCP server 当前提供：

- notes/todos：创建待办、查询待办、完成待办、保存笔记、搜索笔记。
- calendar：创建提醒、查询提醒、联系人管理、创建日程、查询日程、计算空闲时间、助理间约时间。
- scheduler：通过 calendar MCP 的 hidden tools 查询到期提醒，再由 Telegram adapter 发送提醒。

示例：

```text
帮我记一下：护照放在书桌右边抽屉里
添加联系人 李雷 @HEHUAone_bot
我明天 14:00-16:00 要和 mentor 开会
@super666666_bot 帮我和李雷约明天下午 30 分钟
@super666666_bot 帮我问问 @HEHUAone_bot 能做啥
```

## Development

常用检查：

```bash
python -m compileall src tests
python -m unittest discover -s tests
```

项目结构：

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

## Security Notes

- 不要提交 `.env`、`instances/*.env`、SQLite 数据库或任何真实 token。
- 如果 bot token 曾经暴露，应在 BotFather 中重新生成。
- bot-to-bot 群聊测试建议使用 `TELEGRAM_ALLOWED_BOT_USERNAMES` 控制可交互 bot。
- `send_telegram_message` 只能向当前 chat 发送消息，不支持任意 chat id，避免 Agent 越权发消息。
