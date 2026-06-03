from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.model = config.model
        base_url = config.base_url
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        self.client = AsyncOpenAI(api_key=config.api_key, base_url=base_url)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        return await self.client.chat.completions.create(**kwargs)

    async def summarize(self, messages: list[dict[str, str]], previous_summary: str = "") -> str:
        prompt = [
            {
                "role": "system",
                "content": (
                    "你负责维护个人助理对话摘要。用中文简洁总结长期有用的信息，"
                    "包括用户偏好、正在进行的事项、重要上下文。不要编造。"
                ),
            },
            {
                "role": "user",
                "content": f"已有摘要：{previous_summary or '无'}\n\n新消息：{messages}",
            },
        ]
        response = await self.chat(prompt)
        return response.choices[0].message.content or previous_summary
