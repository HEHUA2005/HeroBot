from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from herobot.agent.event import AgentEvent
from herobot.agent.prompts import SYSTEM_PROMPT
from herobot.agent.task import (
    PLANNER_PROMPT,
    ActionLedger,
    FinishDecision,
    TaskFrame,
    deterministic_finish_check,
)
from herobot.core.conversation_store import ConversationStore
from herobot.llm import LLMClient
from herobot.mcp.registry import MCPToolRegistry


SUMMARY_THRESHOLD = 24


class PlatformTools(Protocol):
    finished: bool

    def openai_tool_schemas(self) -> list[dict[str, Any]]:
        ...

    def can_handle(self, name: str) -> bool:
        ...

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        ...


def _tool_call_to_dict(tool_call: Any) -> dict[str, Any]:
    if hasattr(tool_call, "model_dump"):
        return tool_call.model_dump()
    return {
        "id": tool_call.id,
        "type": tool_call.type,
        "function": {
            "name": tool_call.function.name,
            "arguments": tool_call.function.arguments,
        },
    }


@dataclass
class Agent:
    storage: ConversationStore
    llm: LLMClient
    tool_registry: MCPToolRegistry
    persona: str = ""
    max_steps: int = 8

    async def start(self) -> None:
        await self.tool_registry.start()

    async def close(self) -> None:
        await self.tool_registry.close()

    async def reset(self, chat_id: int) -> str:
        await self.storage.reset_conversation(chat_id)
        return "已清空这段会话的上下文记忆。MCP 工具保存的业务数据不会被删除。"

    async def handle_event(self, event: AgentEvent, platform_tools: PlatformTools) -> None:
        if not event.text.strip():
            await platform_tools.call(
                "send_telegram_message",
                {"text": "发点文字给我，我会帮你处理。"},
            )
            await platform_tools.call(
                "finish_task",
                {"status": "blocked", "summary": "empty message", "needs_user_input": True},
            )
            return

        tool_context = event.tool_context()
        tools = self.tool_registry.openai_tool_schemas() + platform_tools.openai_tool_schemas()
        task = await self._plan_task(event, tools)
        ledger = ActionLedger()
        current_message_id = await self.storage.add_message(
            event.chat_id,
            "user",
            event.as_prompt(),
        )
        messages = await self._build_messages(
            event,
            tools,
            task,
            ledger,
            exclude_message_id=current_message_id,
        )

        for step in range(self.max_steps):
            response = await self.llm.chat(messages, tools=tools)
            assistant_message = response.choices[0].message
            tool_calls = assistant_message.tool_calls or []

            if not tool_calls:
                await self._handle_missing_finish(assistant_message.content or "", platform_tools)
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_message.content or "",
                    "tool_calls": [_tool_call_to_dict(tool_call) for tool_call in tool_calls],
                }
            )

            for tool_call in tool_calls:
                name = tool_call.function.name
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}

                if name == "finish_task":
                    decision = await self._evaluate_finish(task, ledger, arguments)
                    if decision.accepted:
                        result = await platform_tools.call(name, arguments)
                    else:
                        result = decision.as_tool_result()
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "finish_task 被 runtime 拒绝。继续执行，直到满足任务完成条件。\n"
                                    f"拒绝原因：{decision.reason}\n"
                                    f"缺失条件：{decision.missing_conditions}\n"
                                    f"当前 ActionLedger：{ledger.as_prompt()}"
                                ),
                            }
                        )
                elif platform_tools.can_handle(name):
                    result = await platform_tools.call(name, arguments)
                    ledger.record(name, arguments, result)
                elif name in self.tool_registry.tool_names:
                    result = await self.tool_registry.call(name, arguments, tool_context)
                    ledger.record(name, arguments, result)
                else:
                    result = json.dumps(
                        {"ok": False, "error": f"unknown tool: {name}"},
                        ensure_ascii=False,
                    )
                    ledger.record(name, arguments, result)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )

            if platform_tools.finished:
                break
        else:
            await platform_tools.call(
                "send_telegram_message",
                {"text": "这轮任务执行步数达到了上限，我先停下来。你可以把目标拆小一点再发我。"},
            )
            await platform_tools.call(
                "finish_task",
                {
                    "status": "failed",
                    "summary": "agent max steps reached",
                    "needs_user_input": True,
                },
            )

        await self._maybe_summarize(event.chat_id)

    async def _plan_task(self, event: AgentEvent, tools: list[dict[str, Any]]) -> TaskFrame:
        prompt = [
            {"role": "system", "content": PLANNER_PROMPT},
            {"role": "system", "content": self._tool_capability_prompt(tools)},
            {"role": "user", "content": event.as_prompt()},
        ]
        try:
            response = await self.llm.chat(prompt)
        except Exception:
            logging.getLogger(__name__).exception("Task planning failed; using fallback TaskFrame.")
            return TaskFrame.fallback(event)
        content = response.choices[0].message.content or ""
        return TaskFrame.from_json_text(content, event)

    async def _evaluate_finish(
        self, task: TaskFrame, ledger: ActionLedger, finish_payload: dict[str, Any]
    ) -> FinishDecision:
        decision = deterministic_finish_check(task, ledger, finish_payload)
        if not decision.accepted:
            return decision
        if str(finish_payload.get("status", "done")) != "done":
            return decision
        if task.expected_messages:
            return decision
        llm_decision = await self._llm_finish_check(task, ledger, finish_payload)
        return llm_decision or decision

    async def _llm_finish_check(
        self, task: TaskFrame, ledger: ActionLedger, finish_payload: dict[str, Any]
    ) -> FinishDecision | None:
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是 HeroBot runtime 的完成度审查器。"
                    "请只输出 JSON："
                    '{"accepted": true|false, "reason": "...", "missing_conditions": ["..."]}。'
                    "如果 ActionLedger 已满足 TaskFrame 的完成条件，accepted=true；否则 accepted=false。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"TaskFrame:\n{task.as_prompt()}\n\n"
                    f"ActionLedger:\n{ledger.as_prompt()}\n\n"
                    f"finish_task payload:\n{json.dumps(finish_payload, ensure_ascii=False)}"
                ),
            },
        ]
        try:
            response = await self.llm.chat(prompt)
            payload = json.loads(response.choices[0].message.content or "{}")
        except Exception:
            return None
        return FinishDecision(
            accepted=bool(payload.get("accepted", False)),
            reason=str(payload.get("reason") or ""),
            missing_conditions=[
                str(item) for item in payload.get("missing_conditions", []) if str(item).strip()
            ],
        )

    async def _build_messages(
        self,
        event: AgentEvent,
        tools: list[dict[str, Any]],
        task: TaskFrame,
        ledger: ActionLedger,
        exclude_message_id: int | None = None,
    ) -> list[dict[str, Any]]:
        summary = await self.storage.get_summary(event.chat_id)
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.append({"role": "system", "content": self._tool_capability_prompt(tools)})
        if self.persona:
            messages.append({"role": "system", "content": f"你的实例人格设定：{self.persona}"})
        if summary:
            messages.append({"role": "system", "content": f"长期记忆摘要：{summary}"})
        messages.append({"role": "system", "content": f"当前 TaskFrame：\n{task.as_prompt()}"})
        messages.append({"role": "system", "content": f"当前 ActionLedger：\n{ledger.as_prompt()}"})
        messages.append(
            {
                "role": "system",
                "content": (
                    "当前必须处理的是下面这个 Telegram 事件。"
                    "最近历史只用于理解上下文，不代表本轮要继续执行历史任务。\n\n"
                    f"{event.as_prompt()}"
                ),
            }
        )
        messages.extend(
            await self.storage.recent_messages(
                event.chat_id,
                exclude_message_id=exclude_message_id,
            )
        )
        return messages

    def _tool_capability_prompt(self, tools: list[dict[str, Any]]) -> str:
        lines = [
            "当前可见工具列表如下。能力自述必须严格基于这些工具；"
            "未列出的工具或 MCP server 一律视为未启用。"
        ]
        for item in tools:
            function = item.get("function", {})
            name = function.get("name", "")
            description = function.get("description", "")
            if not name:
                continue
            lines.append(f"- {name}: {description}")
        return "\n".join(lines)

    async def _handle_missing_finish(self, content: str, platform_tools: PlatformTools) -> None:
        logging.getLogger(__name__).warning("Agent returned without finish_task or tool calls.")
        if content.strip():
            await platform_tools.call("send_telegram_message", {"text": content.strip()})
        else:
            await platform_tools.call(
                "send_telegram_message",
                {"text": "我这轮没有完成任务。请换一种更具体的说法。"},
            )
        await platform_tools.call(
            "finish_task",
            {
                "status": "failed",
                "summary": "agent returned without explicit finish_task",
                "needs_user_input": True,
            },
        )

    async def _maybe_summarize(self, chat_id: int) -> None:
        count = await self.storage.message_count(chat_id)
        last_summary_count = await self.storage.get_summary_message_count(chat_id)
        if count - last_summary_count < SUMMARY_THRESHOLD:
            return
        recent = await self.storage.recent_messages(chat_id, limit=SUMMARY_THRESHOLD)
        previous = await self.storage.get_summary(chat_id)
        try:
            summary = await self.llm.summarize(recent, previous)
        except Exception:
            return
        await self.storage.set_summary(chat_id, summary, count)
