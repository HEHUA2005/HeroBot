from __future__ import annotations

import copy
import json
import os
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from herobot.storage import dumps_result
from herobot.tools import HIDDEN_CONTEXT_KEY, ToolContext


@dataclass(frozen=True)
class MCPToolConfig:
    command: str
    args: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None

    @classmethod
    def from_env(cls) -> "MCPToolConfig":
        command_line = os.getenv("HEROBOT_MCP_SERVER_COMMAND", "herobot-mcp")
        parts = shlex.split(command_line)
        if not parts:
            parts = ["herobot-mcp"]
        return cls(
            command=parts[0],
            args=parts[1:],
            cwd=os.getenv("HEROBOT_MCP_SERVER_CWD") or None,
            env=os.environ.copy(),
        )


class MCPToolRegistry:
    def __init__(self, config: MCPToolConfig) -> None:
        self.config = config
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[Any] = []
        self._tool_names: set[str] = set()

    # REVIEW: 每次访问 tool_names 都创建一个新的 set 拷贝。
    # 这个 property 在 agent 的每一步都被调用（通过 `name in self.tool_registry.tool_names`），
    # 虽然性能影响很小，但这种防御性拷贝没有必要——_tool_names 在 start() 之后就不变了。
    # 直接返回 self._tool_names 即可，或者在 start() 之后用 frozenset。
    @property
    def tool_names(self) -> set[str]:
        return set(self._tool_names)

    async def start(self) -> None:
        if self._session is not None:
            return
        stack = AsyncExitStack()
        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=self.config.env,
            cwd=self.config.cwd,
        )
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        result = await session.list_tools()
        self._tools = list(result.tools)
        self._tool_names = {tool.name for tool in self._tools}
        self._session = session
        self._stack = stack

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
        self._tools = []
        self._tool_names = set()

    def openai_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": self._sanitize_schema(tool.inputSchema or {}),
                },
            }
            for tool in self._tools
        ]

    # REVIEW: MCP tool 调用（通过子进程 stdio 通信）没有超时设置。
    # 如果 MCP server 子进程卡住或崩溃，这个 await 会永远挂起，
    # 导致整个 agent 停止响应。应该加 asyncio.wait_for 设置超时。
    #
    # 另外如果 MCP server 进程意外退出，_session 还是非 None，
    # 后续调用会得到难以理解的管道错误。需要健康检查或重连机制。
    async def call(self, name: str, arguments: dict[str, Any], context: ToolContext) -> str:
        if self._session is None:
            raise RuntimeError("MCP tool registry has not been started")
        payload = dict(arguments)
        payload[HIDDEN_CONTEXT_KEY] = {
            "chat_id": context.chat_id,
            "user_id": context.user_id,
            "chat_type": context.chat_type,
            "timezone": context.timezone,
            "owner_user_id": context.effective_owner_user_id,
        }
        result = await self._session.call_tool(name, payload)
        if result.isError:
            return dumps_result({"ok": False, "error": self._content_to_text(result.content)})
        if result.structuredContent is not None:
            return dumps_result(result.structuredContent)
        text = self._content_to_text(result.content)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return dumps_result({"ok": True, "result": text})
        return dumps_result(parsed)

    def _sanitize_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        sanitized = copy.deepcopy(schema)
        properties = sanitized.get("properties")
        if isinstance(properties, dict):
            properties.pop(HIDDEN_CONTEXT_KEY, None)
        required = sanitized.get("required")
        if isinstance(required, list):
            sanitized["required"] = [item for item in required if item != HIDDEN_CONTEXT_KEY]
        return sanitized

    def _content_to_text(self, content: list[Any]) -> str:
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
