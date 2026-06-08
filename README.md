# HeroBot

HeroBot 是一个运行在 Telegram 上的个人助理 Agent。Telegram 仅作为消息入口和发送通道；核心决策由 OpenAI-compatible LLM 通过 ReAct-style tool loop 完成。业务能力通过本地 MCP server 暴露，Telegram 发消息等平台能力由 Agent runtime 内置工具提供。

## Architecture

```text
Telegram Update
  -> Telegram Adapter
  -> AgentEvent
  -> ReAct Agent Runtime
      -> Internal Platform Tools
         - send_telegram_message
         - finish_task
      -> MCP Tool Registry
         -> herobot-mcp
            - notes
            - reminders
            - contacts
            - calendar
            - scheduling
            - time
            - todos
```

关键边界：

- `bot.py` 负责 Telegram 鉴权、命令入口、消息接收和 `AgentEvent` 构造。
- `agent.py` 负责 ReAct loop、工具选择、工具 observation 回填和 `finish_task` 收束。
- `platform.py` 提供 Telegram 平台工具，工具只能向当前 chat 发送消息。
- `mcp_server.py` 暴露本地 MCP 业务工具，不直接访问 Telegram API。
- `tool_registry.py` 负责启动 MCP server、读取 `tools/list`、调用 `tools/call`，并注入隐藏上下文。

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
```

安装后会注册三个命令：

```text
herobot       # 启动单个 Telegram bot 实例
herobot-multi # 从 instances/*.env 批量启动多个实例
herobot-mcp   # 本地 MCP business tool server
```

通常只需要运行 `herobot` 或 `herobot-multi`。主进程会按配置自动启动 `herobot-mcp`。

## Configuration

编辑 `.env`：

```bash
TELEGRAM_BOT_TOKEN=replace-with-your-token
TELEGRAM_ENABLE_USER_WHITELIST=true
TELEGRAM_ALLOWED_USER_IDS=123456789

OPENAI_API_KEY=replace-with-your-api-key
OPENAI_BASE_URL=localhost:4000
OPENAI_MODEL=your-model

HEROBOT_DB_PATH=data/herobot.sqlite3
HEROBOT_PERSONA=
HEROBOT_DEFAULT_TIMEZONE=Asia/Shanghai
HEROBOT_MAX_AGENT_STEPS=8
HEROBOT_MCP_SERVER_COMMAND=herobot-mcp

ENABLE_BOT_TO_BOT=false
TELEGRAM_ALLOWED_BOT_USERNAMES=other_bot,review_bot

LOG_LEVEL=INFO
```

配置说明：

- `TELEGRAM_ALLOWED_USER_IDS`：允许使用个人助理的 Telegram user id。可先启动 bot 后发送 `/whoami` 获取。
- `OPENAI_BASE_URL`：支持 OpenAI-compatible API，例如 `localhost:4000`。
- `HEROBOT_DB_PATH`：本地 SQLite 数据库路径。
- `HEROBOT_MAX_AGENT_STEPS`：Agent 单轮最大工具步数，是防止循环的保险丝。
- `HEROBOT_MCP_SERVER_COMMAND`：本地 MCP server 启动命令，默认 `herobot-mcp`。
- `ENABLE_BOT_TO_BOT`：是否允许白名单内其他 bot 的消息进入 Agent runtime。
- `TELEGRAM_ALLOWED_BOT_USERNAMES`：允许交互的 bot username，逗号分隔，不需要 `@`。

测试群聊时可以临时关闭用户白名单：

```bash
TELEGRAM_ENABLE_USER_WHITELIST=false
```

关闭后，任何能在群聊中 @ 到 bot 的用户都可能消耗 LLM API 额度。测试结束后建议重新开启。

## Running

启动单个实例：

```bash
herobot
```

指定 env 文件：

```bash
herobot --env-file instances/super666666.env
```

批量启动多个实例：

```bash
herobot-multi --env-dir instances
```

同一个 Telegram bot token 只能有一个 polling 进程。如果看到 `409 Conflict`，说明同一个 token 被多个进程同时使用。

## Capabilities

HeroBot 当前支持：

- 自然语言待办：创建、查询、完成待办。
- 自然语言提醒：创建提醒、查询提醒。
- 个人笔记：保存和搜索笔记。
- 联系人：维护“姓名 + bot username”的通讯录。
- 本地日程：创建事件、查询事件、计算空闲时间。
- 助理间约时间：在共同群聊中联系另一个助理 bot，只交换可用时间，不暴露具体日程内容。
- 通用 bot-to-bot 协作：Agent 可按意图通过 `send_telegram_message` 自然联系其他 bot。

示例：

```text
帮我记一下：护照放在书桌右边抽屉里
添加联系人 李雷 @HEHUAone_bot
我明天 14:00-16:00 要和 mentor 开会
@super666666_bot 帮我和李雷约明天下午 30 分钟
@super666666_bot 帮我问问 @HEHUAone_bot 能做啥
```

## Telegram Commands

- `/start`：启动说明。
- `/help`：查看帮助。
- `/reset`：清空当前 chat 的对话上下文，不删除笔记、提醒、联系人或日程。
- `/whoami`：查看当前 Telegram user id。
- `/chatid`：查看当前 chat id 和 chat type。
- `/contacts`：交给 Agent 查询联系人。
- `/calendar` / `/calender`：交给 Agent 查询近期日程。
- `/availability`：交给 Agent 查询空闲时间。
- `/pending`：交给 Agent 查询待确认的约时间。

## Development

常用检查：

```bash
python -m compileall src tests
python -m unittest discover -s tests
```

项目结构：

```text
src/herobot/
  agent.py         ReAct-style Agent runtime
  bot.py           Telegram adapter
  platform.py      Telegram platform tools
  mcp_server.py    Local MCP business tool server
  tool_registry.py MCP client/registry
  tools.py         Business tool implementations
  storage.py       SQLite persistence
  scheduling.py    Deterministic availability/time-window helpers
  scheduler.py     Reminder delivery loop
  llm.py           OpenAI-compatible LLM client
  multi.py         Multi-instance launcher
  bot2bot.py       Telegram username/mention helpers
```

## Security Notes

- 不要提交 `.env`、`instances/*.env`、SQLite 数据库或任何真实 token。
- 如果 bot token 曾经暴露，应在 BotFather 中重新生成。
- bot-to-bot 群聊测试建议使用 `TELEGRAM_ALLOWED_BOT_USERNAMES` 控制可交互 bot。
- `send_telegram_message` 只能向当前 chat 发送消息，不支持任意 chat id，避免 Agent 越权发消息。
