# Adding an MCP Server to HeroBot

HeroBot 的扩展边界是 MCP server：

```text
Telegram Core -> Agent Runtime -> MCP Host -> MCP Servers
```

如果你想给 bot 增加新能力，推荐做成一个独立 MCP server，然后在 `herobot.toml`
里接入。这样 Telegram 层、Agent runtime 和业务能力可以保持解耦。

## 适合做成 MCP Server 的能力

适合：

- 查询外部 API，例如天气、股票、搜索、公司内部系统。
- 管理独立业务数据，例如账本、读书记录、健身记录。
- 执行确定性业务逻辑，例如格式转换、报表生成、文档检索。
- 需要自己维护依赖、数据库或配置的功能模块。

不适合：

- 直接发送 Telegram 消息。发送消息是 HeroBot runtime 的平台工具。
- 决定是否回复用户。这个判断属于 Agent runtime。
- 保存 Telegram token 或绕过 HeroBot 鉴权。

## 最小 MCP Server 示例

下面是一个最小的外部 MCP server。它提供一个 `get_weather` 工具。

```text
my_weather_mcp/
  my_weather_mcp/
    __init__.py
    server.py
  pyproject.toml
```

`my_weather_mcp/server.py`：

```python
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP


mcp = FastMCP(
    "weather-tools",
    instructions="Weather tools for HeroBot. Does not access Telegram directly.",
)


@mcp.tool(description="Get a simple weather report for a city.")
async def get_weather(city: str) -> dict[str, Any]:
    # Replace this stub with a real weather API call.
    return {
        "ok": True,
        "result": {
            "city": city,
            "summary": "晴，适合出门。",
        },
    }


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
```

`pyproject.toml`：

```toml
[project]
name = "my-weather-mcp"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "mcp>=1.27,<2",
]

[project.scripts]
my-weather-mcp = "my_weather_mcp.server:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["my_weather_mcp"]
```

安装到当前 HeroBot 的 pyenv 环境：

```bash
pyenv local herobot
pip install -e /path/to/my_weather_mcp
```

也可以不做 console script，直接用 `python -m my_weather_mcp.server` 启动。

## 接入 HeroBot

如果 MCP server 的 README 给的是社区常见 `mcpServers` JSON，直接复制到 HeroBot 项目根目录的
`mcp.json` 或 `.mcp.json`：

```json
{
  "mcpServers": {
    "weather": {
      "command": "my-weather-mcp",
      "args": []
    }
  }
}
```

HeroBot 启动时会自动读取 `mcp.json` 和 `.mcp.json`，并追加里面的外部 MCP server。
也兼容 VS Code 风格顶层 `servers`：

```json
{
  "servers": {
    "weather": {
      "command": "my-weather-mcp",
      "args": []
    }
  }
}
```

如果使用 npm/npx MCP server，可以直接写：

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

JSON 配置默认作为 optional server 处理，也就是启动失败不会阻止 HeroBot 启动。

如果你需要 HeroBot 专属字段，例如 `hidden_tools`、`exposed_tools` 或 `timeout_seconds`，
可以继续在 `herobot.toml` 中加入：

```toml
[[mcp.servers]]
name = "weather"
command = "my-weather-mcp"
args = []
enabled = true
required = false
exposed_tools = []
hidden_tools = []
timeout_seconds = 30
env = {}
```

如果你用 `python -m` 启动，JSON 写法是：

```json
{
  "mcpServers": {
    "weather": {
      "command": "python",
      "args": ["-m", "my_weather_mcp.server"]
    }
  }
}
```

对应的 TOML 写法是：

```toml
[[mcp.servers]]
name = "weather"
command = "python"
args = ["-m", "my_weather_mcp.server"]
enabled = true
required = false
timeout_seconds = 30
env = {}
```

如果 server 不在当前工作目录，可以加 `cwd`：

```json
{
  "mcpServers": {
    "weather": {
      "command": "python",
      "args": ["-m", "my_weather_mcp.server"],
      "cwd": "/path/to/my_weather_mcp"
    }
  }
}
```

启动 HeroBot：

```bash
herobot --config herobot.toml
```

然后在 Telegram 里测试：

```text
@super666666_bot 查一下上海天气
```

Agent 会看到 `get_weather` 工具，根据用户语义决定是否调用它。

如果 `herobot.toml` 和 `mcp.json` 里出现同名 server，HeroBot 以 `herobot.toml` 为准，
并跳过 JSON 里的同名 server。

## 配置字段说明

`mcp.json` / `.mcp.json` 的 server 配置支持这些字段：

```json
{
  "mcpServers": {
    "weather": {
      "command": "my-weather-mcp",
      "args": [],
      "enabled": true,
      "disabled": false,
      "required": false,
      "cwd": "/optional/working/directory",
      "env": { "WEATHER_API_KEY": "replace-me" },
      "exposed_tools": [],
      "hidden_tools": [],
      "timeout_seconds": 30
    }
  }
}
```

`herobot.toml` 的 `[[mcp.servers]]` 支持同一组字段：

```toml
name = "weather"
command = "my-weather-mcp"
args = []
enabled = true
required = false
cwd = "/optional/working/directory"
env = { WEATHER_API_KEY = "replace-me" }
exposed_tools = []
hidden_tools = []
timeout_seconds = 30
```

- `name`：server 名称，只在 HeroBot 内部用于日志和冲突报错。
- `command` / `args`：stdio MCP server 的启动命令。
- `enabled`：设为 `false` 时不会启动。
- `required`：设为 `true` 时，server 启动失败会导致 HeroBot 启动失败；外部实验功能建议先用 `false`。
- `cwd`：server 进程工作目录，可选。
- `env`：传给 server 进程的环境变量。不要提交真实 API key。
- `exposed_tools`：工具白名单。空列表表示暴露该 server 的所有非 hidden 工具。
- `hidden_tools`：不暴露给 LLM 的工具。当前主要用于 runtime 或 scheduler 内部调用。
- `timeout_seconds`：单次 MCP tool call 超时时间。

JSON server 默认 `required=false`；TOML server 默认 `required=true`。
工具名必须全局唯一。如果两个 MCP server 都暴露 `search`，HeroBot 会 fail fast。
建议给工具使用明确名字，例如 `get_weather`、`search_company_docs`、`create_expense_record`。

## 返回格式

建议所有工具返回统一 JSON：

```python
return {
    "ok": True,
    "result": {
        "key": "value",
    },
}
```

失败时：

```python
return {
    "ok": False,
    "error": "city is required",
}
```

HeroBot 会把 tool observation 回填给 Agent。Agent 再决定是否继续调用工具、回复用户或结束任务。

## 使用隐藏上下文

有些工具需要知道当前 Telegram chat、用户或时区。不要让 LLM 填这些字段。

HeroBot 会在调用 MCP tool 时自动注入隐藏参数 `herobot_context`。如果你的工具需要上下文，
把它声明为可选参数：

```python
from typing import Any


@mcp.tool(description="Save a preference for the current Telegram chat.")
async def save_preference(
    key: str,
    value: str,
    herobot_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not herobot_context:
        return {"ok": False, "error": "missing HeroBot context"}

    chat_id = int(herobot_context["chat_id"])
    user_id = int(herobot_context["user_id"])
    timezone_name = str(herobot_context.get("timezone") or "Asia/Shanghai")

    # Store key/value scoped by chat_id and user_id.
    return {
        "ok": True,
        "result": {
            "chat_id": chat_id,
            "user_id": user_id,
            "timezone": timezone_name,
            "key": key,
            "value": value,
        },
    }
```

LLM 看不到 `herobot_context`，也不会被要求填写它。HeroBot 当前注入的字段是：

```json
{
  "chat_id": 123,
  "user_id": 8954064995,
  "chat_type": "private",
  "timezone": "Asia/Shanghai",
  "owner_user_id": 8954064995
}
```

如果工具完全不需要上下文，可以不要声明 `herobot_context`。

## 给工具写好描述

MCP tools 是 model-controlled。Agent 会根据工具名、description 和参数 schema 判断什么时候调用。

推荐：

```python
@mcp.tool(description="Search internal engineering docs by keyword.")
async def search_engineering_docs(query: str, limit: int = 5) -> dict[str, Any]:
    ...
```

不推荐：

```python
@mcp.tool(description="Search.")
async def search(q: str) -> dict[str, Any]:
    ...
```

原则：

- 工具名用动词开头。
- description 写清楚业务对象和使用场景。
- 参数名使用自然含义，例如 `city`、`query`、`start_at`。
- 时间参数优先使用 ISO 8601 字符串，并在 description 里说明时区要求。

## 命令 Alias

如果你希望 `/weather` 走 Agent，而不是写 Telegram 命令处理逻辑，可以在 `herobot.toml`
里加 alias：

```toml
[commands.aliases]
weather = "查询天气"
```

用户发送：

```text
/weather 上海
```

Telegram adapter 会把它转换成自然语言事件交给 Agent，Agent 再决定调用哪个 MCP tool。

## 调试清单

1. 确认外部 server 能被当前 pyenv 环境找到：

```bash
which my-weather-mcp
python -m my_weather_mcp.server
```

`python -m my_weather_mcp.server` 会等待 stdio 输入，看起来像卡住是正常的。按 `Ctrl+C` 退出。

2. 把外部功能先设为 optional：

```toml
required = false
```

这样 server 启动失败时 HeroBot 仍能启动，并在日志里记录错误。

3. 确认工具名没有冲突。

如果冲突，HeroBot 会在启动时抛出类似错误：

```text
Duplicate MCP tool name: get_weather from weather and another_server
```

4. 控制超时。

外部 API 容易慢，先给 server 设置合理的超时：

```toml
timeout_seconds = 30
```

5. 让 Agent 更容易选中工具。

如果 Agent 没有调用你的工具，优先检查工具名和 description。多数情况下，不需要改
Telegram adapter 或 Agent runtime。

## 安全边界

- 不要把 Telegram token 传给外部 MCP server，除非你非常明确需要这样做。
- MCP server 不应该直接向 Telegram 发消息。返回结果，让 Agent 使用平台工具回复。
- 不要在工具返回值里暴露敏感配置、API key 或完整内部错误栈。
- 需要按用户或群聊隔离的数据，使用 `herobot_context.chat_id` 和 `herobot_context.user_id`
  做作用域。

## 什么时候需要改 HeroBot 本体

大多数扩展只需要新增 MCP server 和修改 `herobot.toml`。

只有下面这些情况才需要改 HeroBot 本体：

- 你要新增 Telegram 平台能力，例如发送图片、文件、按钮。
- 你要让 runtime 或 scheduler 主动调用某个 hidden tool。
- 你要改变 Agent 的全局决策策略或上下文构造方式。
- 你要支持 stdio 之外的 MCP transport。
