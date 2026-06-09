from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from herobot.agent.event import AgentEvent


PLANNER_PROMPT = """你是 HeroBot 的任务规划器。你只负责理解当前 Telegram 事件，不调用工具。

请只输出 JSON 对象，不要输出 Markdown 或解释。

JSON schema:
{
  "goal": "用户本轮真实目标",
  "subtasks": ["子任务"],
  "completion_conditions": ["可验证完成条件"],
  "expected_messages": ["如果用户明确要求逐条/连续/挨个发送固定消息，列出每条消息文本，否则为空数组"],
  "requires_user_input": false,
  "requires_external_response": false
}

规则：
- expected_messages 只用于用户明确要求逐条、连续、挨个、每条单独发送消息的场景。
- “挨个报数，数到10” 的 expected_messages 必须是 ["1","2","3","4","5","6","7","8","9","10"]。
- 不要编造无法从当前事件确定的消息序列。
"""


@dataclass
class TaskFrame:
    goal: str
    subtasks: list[str] = field(default_factory=list)
    completion_conditions: list[str] = field(default_factory=list)
    expected_messages: list[str] = field(default_factory=list)
    requires_user_input: bool = False
    requires_external_response: bool = False

    @classmethod
    def fallback(cls, event: AgentEvent) -> "TaskFrame":
        expected = infer_expected_messages(event.text)
        conditions = ["必须对用户请求给出可观察处理结果"]
        if expected:
            conditions = [f"必须按顺序成功发送 {len(expected)} 条独立 Telegram 消息"]
        return cls(
            goal=event.text.strip() or "处理空消息",
            subtasks=[event.text.strip()] if event.text.strip() else [],
            completion_conditions=conditions,
            expected_messages=expected,
        )

    @classmethod
    def from_json_text(cls, text: str, event: AgentEvent) -> "TaskFrame":
        try:
            payload = json.loads(_extract_json_object(text))
        except (json.JSONDecodeError, ValueError, TypeError):
            return cls.fallback(event)
        expected = _string_list(payload.get("expected_messages"))
        inferred_expected = infer_expected_messages(event.text)
        if inferred_expected and not expected:
            expected = inferred_expected
        conditions = _string_list(payload.get("completion_conditions"))
        if expected and not any("发送" in item for item in conditions):
            conditions.append(f"必须按顺序成功发送 {len(expected)} 条独立 Telegram 消息")
        return cls(
            goal=str(payload.get("goal") or event.text.strip() or "处理当前消息"),
            subtasks=_string_list(payload.get("subtasks")),
            completion_conditions=conditions or ["必须对用户请求给出可观察处理结果"],
            expected_messages=expected,
            requires_user_input=bool(payload.get("requires_user_input", False)),
            requires_external_response=bool(payload.get("requires_external_response", False)),
        )

    def as_prompt(self) -> str:
        return json.dumps(
            {
                "goal": self.goal,
                "subtasks": self.subtasks,
                "completion_conditions": self.completion_conditions,
                "expected_messages": self.expected_messages,
                "requires_user_input": self.requires_user_input,
                "requires_external_response": self.requires_external_response,
            },
            ensure_ascii=False,
        )


@dataclass
class LedgerEntry:
    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error: str = ""


@dataclass
class ActionLedger:
    tool_calls: list[LedgerEntry] = field(default_factory=list)
    sent_messages: list[str] = field(default_factory=list)
    successful_effects: list[str] = field(default_factory=list)
    failed_effects: list[str] = field(default_factory=list)

    def record(self, name: str, arguments: dict[str, Any], raw_result: str) -> None:
        parsed = _parse_tool_result(raw_result)
        ok = bool(parsed.get("ok", False))
        result = parsed.get("result")
        error = str(parsed.get("error") or "")
        self.tool_calls.append(LedgerEntry(name, arguments, ok, result, error))
        if ok:
            self.successful_effects.append(name)
            self._record_success(name, result)
        else:
            self.failed_effects.append(f"{name}: {error}")

    def _record_success(self, name: str, result: Any) -> None:
        if name == "send_telegram_message" and isinstance(result, dict):
            text = result.get("text")
            if text is not None:
                self.sent_messages.append(str(text))
        elif name == "send_telegram_messages" and isinstance(result, list):
            for item in result:
                if isinstance(item, dict) and item.get("text") is not None:
                    self.sent_messages.append(str(item["text"]))

    def has_failures(self) -> bool:
        return bool(self.failed_effects)

    def as_prompt(self) -> str:
        return json.dumps(
            {
                "tool_calls": [
                    {
                        "name": item.name,
                        "ok": item.ok,
                        "arguments": item.arguments,
                        "result": item.result,
                        "error": item.error,
                    }
                    for item in self.tool_calls
                ],
                "sent_messages": self.sent_messages,
                "successful_effects": self.successful_effects,
                "failed_effects": self.failed_effects,
            },
            ensure_ascii=False,
            default=str,
        )


@dataclass(frozen=True)
class FinishDecision:
    accepted: bool
    reason: str
    missing_conditions: list[str] = field(default_factory=list)

    def as_tool_result(self) -> str:
        return json.dumps(
            {
                "ok": self.accepted,
                "result": {
                    "accepted": self.accepted,
                    "reason": self.reason,
                    "missing_conditions": self.missing_conditions,
                },
            },
            ensure_ascii=False,
        )


def deterministic_finish_check(
    task: TaskFrame, ledger: ActionLedger, finish_payload: dict[str, Any]
) -> FinishDecision:
    status = str(finish_payload.get("status", "done"))
    needs_user_input = bool(finish_payload.get("needs_user_input", False))

    if status == "blocked":
        if needs_user_input and ledger.sent_messages:
            return FinishDecision(True, "已向用户发送澄清问题，允许 blocked 结束")
        if needs_user_input:
            return FinishDecision(False, "需要用户输入前必须先发送问题", ["发送澄清问题"])
        return FinishDecision(True, "任务明确阻塞，允许结束")

    if ledger.has_failures() and status == "done":
        return FinishDecision(False, "存在失败的工具调用，不能标记完成", ledger.failed_effects)

    if task.expected_messages:
        actual = [_normalize_message_text(item) for item in ledger.sent_messages]
        expected = [_normalize_message_text(item) for item in task.expected_messages]
        if actual[: len(expected)] != expected:
            missing = expected[len(actual) :] if len(actual) < len(expected) else expected
            return FinishDecision(
                False,
                f"用户要求发送 {len(expected)} 条独立消息，目前只匹配 {min(len(actual), len(expected))} 条",
                [f"还需要按顺序发送: {', '.join(missing)}"],
            )
        return FinishDecision(True, "期望消息序列已全部发送")

    if status == "done" and not ledger.tool_calls:
        return FinishDecision(False, "没有任何可观察动作，不能标记完成", ["调用回复或业务工具"])

    return FinishDecision(True, "确定性校验通过")


def infer_expected_messages(text: str) -> list[str]:
    if not re.search(r"(挨个|逐条|连续|每条|单独).{0,8}(报数|数|发送|发)", text):
        return []
    match = re.search(r"(?:数到|到)\s*(\d{1,2})", text)
    if not match:
        return []
    end = int(match.group(1))
    if end <= 0 or end > 20:
        return []
    return [str(index) for index in range(1, end + 1)]


def _parse_tool_result(raw_result: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_result)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"invalid tool result: {raw_result}"}
    if isinstance(parsed, dict):
        return parsed
    return {"ok": True, "result": parsed}


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    raise ValueError("no JSON object found")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _normalize_message_text(text: str) -> str:
    return str(text).strip()
