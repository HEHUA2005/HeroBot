# HeroBot

一个 Telegram 个人助理 bot 模板，用 OpenAI-compatible LLM 理解自然语言，并通过本地工具管理待办、提醒和笔记。

## 安全提醒

你刚刚把 bot token 发在了聊天里。建议到 Telegram 的 BotFather 执行 `/revoke` 或重新生成 token，然后只把新 token 放进本地 `.env`，不要提交到 Git。

## 快速开始

```bash
pyenv virtualenv 3.12.9 herobot
pyenv local herobot
pip install -e .
cp .env.example .env
```

编辑 `.env`：

```bash
TELEGRAM_BOT_TOKEN=你的新token
TELEGRAM_ENABLE_USER_WHITELIST=true
TELEGRAM_ALLOWED_USER_IDS=你的Telegram用户ID
OPENAI_API_KEY=你的LLM API key
OPENAI_BASE_URL=localhost:4000
OPENAI_MODEL=你的模型名
HEROBOT_DB_PATH=data/herobot.sqlite3
HEROBOT_OWNER_NAME=你的名字
HEROBOT_DEFAULT_TIMEZONE=Asia/Shanghai
HEROBOT_WORKING_HOURS=09:00-18:00
HEROBOT_DEFAULT_REMINDER_MINUTES=10
ENABLE_BOT_TO_BOT=false
TELEGRAM_ALLOWED_BOT_USERNAMES=other_bot,review_bot
BOT_TO_BOT_MAX_DEPTH=3
```

如果不知道自己的 Telegram 用户 ID，先启动 bot 后发送 `/whoami`，再把返回的 ID 写入 `.env` 的 `TELEGRAM_ALLOWED_USER_IDS`。

群聊测试时可以临时关闭白名单：

```bash
TELEGRAM_ENABLE_USER_WHITELIST=false
```

关闭后任何能 @ 到 bot 的人都能消耗你的 LLM API 额度，测试完建议改回 `true`。

启动 bot：

```bash
herobot
```

然后在 Telegram 里打开你的 bot，发送 `/start`。

## 批量启动多个 bot

不要复制整个项目来启动多个实例。推荐一个代码目录配多个 env 文件：

```bash
cp instances/super666666.env.example instances/super666666.env
cp instances/other.env.example instances/other.env
```

分别填写不同的 `TELEGRAM_BOT_TOKEN`，并给每个实例设置不同的 `HEROBOT_DB_PATH`。

启动全部实例：

```bash
herobot-multi --env-dir instances
```

也可以单独启动某个实例：

```bash
herobot --env-file instances/super666666.env
herobot --env-file instances/other.env
```

同一个 Telegram bot token 只能有一个 polling 进程。如果看到 `409 Conflict`，基本就是某个 token 被两个进程同时使用了。

## 当前能力

- `/start`：开始会话
- `/help`：查看命令
- `/reset`：清空当前聊天的对话上下文，不删除待办、提醒、笔记
- `/whoami`：查看自己的 Telegram user id
- `/chatid`：查看当前 chat id 和 chat type
- `/contacts`：查看联系人通讯录
- `/calendar`：查看未来 7 天日程
- `/availability`：查看默认时间范围的空闲时间
- `/pending`：查看待确认的约时间
- 自然语言创建和查询待办，例如“我今天要买牛奶”“我还有什么待办？”
- 自然语言创建和查询提醒，例如“提醒我 20 分钟后喝水”
- 自然语言保存和搜索笔记，例如“帮我记一下护照放在抽屉里”
- 自然语言维护联系人和日程，例如“添加联系人 李雷 @lilei_bot”“明天 14:00-16:00 有会”
- 群聊中跨助理约时间，例如“@super666666_bot 帮我和李雷约明天下午 30 分钟”
- 计算和当前时间工具，由 LLM 按需调用
- 群聊实验功能：开启 `ENABLE_BOT_TO_BOT=true` 后，可以让 HeroBot 在群里 @ 另一个 bot 发起协作请求

群聊 bot-to-bot 委托时，第一个被 @ 的 bot 是主控方，后续 @ 的 bot 是协作目标。例如：

```text
@super666666_bot 请你和 @HEHUAone_bot 讨论一下：先有鸡还是先有蛋？
```

这条消息只会由 `@super666666_bot` 发起委托，`@HEHUAone_bot` 会等待协议消息，不会同时抢着发起。

## 项目结构

```text
src/herobot/
  agent.py      # LLM 编排层，负责上下文、工具调用和最终回复
  bot.py        # Telegram 适配层，负责鉴权、收消息、发消息、命令注册
  bot2bot.py    # 群聊 bot-to-bot 委托消息格式和解析
  llm.py        # OpenAI-compatible LLM 客户端
  scheduler.py  # 进程内提醒调度
  storage.py    # SQLite 持久化
  tools.py      # 待办、提醒、笔记、时间、计算工具
```
