from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from herobot.core.conversation_store import dumps_result
from herobot.mcp.config import MCPServerConfig
from herobot.mcp.context import HIDDEN_CONTEXT_KEY, ToolContext


logger = logging.getLogger(__name__)


BUILTIN_COMMAND_MODULES = {
    "herobot-mcp-notes": "herobot.mcp.builtin.notes.server",
    "herobot-mcp-calendar": "herobot.mcp.builtin.calendar.server",
}


@dataclass
class _ToolBinding:
    server_name: str
    tool: Any
    hidden: bool


class _MCPServerConnection:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.tools: list[Any] = []

    async def start(self) -> None:
        if self._session is not None:
            return
        stack = AsyncExitStack()
        env = os.environ.copy()
        env.update(self.config.env)
        command, args = self._resolved_command(env)
        params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
            cwd=self.config.cwd,
        )
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        result = await session.list_tools()
        self.tools = list(result.tools)
        self._session = session
        self._stack = stack

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
        self.tools = []

    async def call(self, name: str, payload: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError(f"MCP server is not started: {self.config.name}")
        return await self._session.call_tool(name, payload)

    def _resolved_command(self, env: dict[str, str]) -> tuple[str, list[str]]:
        module = BUILTIN_COMMAND_MODULES.get(self.config.command)
        if module is None or shutil.which(self.config.command, path=env.get("PATH")):
            return self.config.command, self.config.args
        return sys.executable, ["-m", module, *self.config.args]


class MCPToolRegistry:
    def __init__(self, server_configs: MCPServerConfig | list[MCPServerConfig]) -> None:
        self.server_configs = server_configs if isinstance(server_configs, list) else [server_configs]
        self._servers: list[_MCPServerConnection] = []
        self._bindings: dict[str, _ToolBinding] = {}

    @property
    def tool_names(self) -> set[str]:
        return {name for name, binding in self._bindings.items() if not binding.hidden}

    @property
    def hidden_tool_names(self) -> set[str]:
        return {name for name, binding in self._bindings.items() if binding.hidden}

    async def start(self) -> None:
        if self._servers:
            return
        started: list[_MCPServerConnection] = []
        try:
            for config in self.server_configs:
                if not config.enabled:
                    continue
                connection = _MCPServerConnection(config)
                try:
                    await connection.start()
                except Exception:
                    if config.required:
                        raise
                    logger.exception("Optional MCP server failed to start: %s", config.name)
                    continue
                started.append(connection)
            self._servers = started
            self._index_tools()
        except Exception:
            for connection in started:
                await connection.close()
            self._servers = []
            self._bindings = {}
            raise

    async def close(self) -> None:
        for connection in reversed(self._servers):
            await connection.close()
        self._servers = []
        self._bindings = {}

    def openai_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": binding.tool.description or "",
                    "parameters": self._sanitize_schema(binding.tool.inputSchema or {}),
                },
            }
            for name, binding in self._bindings.items()
            if not binding.hidden
        ]

    async def call(self, name: str, arguments: dict[str, Any], context: ToolContext) -> str:
        binding = self._bindings.get(name)
        if binding is None:
            return dumps_result({"ok": False, "error": f"unknown tool: {name}"})
        if binding.hidden:
            return dumps_result({"ok": False, "error": f"tool is hidden from agent: {name}"})
        return await self._call_binding(name, arguments, context, binding)

    async def call_hidden(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> str:
        binding = self._bindings.get(name)
        if binding is None:
            return dumps_result({"ok": False, "error": f"unknown hidden tool: {name}"})
        if not binding.hidden:
            return dumps_result({"ok": False, "error": f"tool is not hidden: {name}"})
        return await self._call_binding(name, arguments, context, binding)

    async def _call_binding(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        binding: _ToolBinding,
    ) -> str:
        connection = self._server(binding.server_name)
        payload = dict(arguments)
        payload[HIDDEN_CONTEXT_KEY] = {
            "chat_id": context.chat_id,
            "user_id": context.user_id,
            "chat_type": context.chat_type,
            "timezone": context.timezone,
            "owner_user_id": context.effective_owner_user_id,
        }
        result = await connection.call(name, payload)
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

    def _index_tools(self) -> None:
        bindings: dict[str, _ToolBinding] = {}
        for connection in self._servers:
            config = connection.config
            exposed_filter = set(config.exposed_tools)
            hidden_filter = set(config.hidden_tools)
            for tool in connection.tools:
                hidden = tool.name in hidden_filter
                exposed = not exposed_filter or tool.name in exposed_filter
                if not hidden and not exposed:
                    continue
                if tool.name in bindings:
                    first = bindings[tool.name].server_name
                    raise RuntimeError(
                        f"Duplicate MCP tool name: {tool.name} from {first} and {config.name}"
                    )
                bindings[tool.name] = _ToolBinding(config.name, tool, hidden)
        self._bindings = bindings

    def _server(self, server_name: str) -> _MCPServerConnection:
        for connection in self._servers:
            if connection.config.name == server_name:
                return connection
        raise RuntimeError(f"MCP server not found: {server_name}")

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
