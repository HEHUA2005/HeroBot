from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 0.5


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.model = config.model
        self.timeout_seconds = config.timeout_seconds
        self.max_retries = max(0, config.max_retries)
        self.retry_base_delay_seconds = max(0.0, config.retry_base_delay_seconds)
        base_url = config.base_url
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=base_url,
            timeout=config.timeout_seconds,
        )

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
        return await self._chat_with_retries(kwargs)

    async def _chat_with_retries(self, kwargs: dict[str, Any]) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self.client.chat.completions.create(**kwargs),
                    timeout=self.timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _is_retryable_llm_error(exc):
                    logger.exception("LLM chat request failed with non-retryable error.")
                    raise
                if attempt >= self.max_retries:
                    logger.exception("LLM chat request failed after retries.")
                    raise
                delay = self.retry_base_delay_seconds * (2**attempt)
                logger.warning(
                    "LLM chat request failed; retrying in %.2fs (attempt %s/%s).",
                    delay,
                    attempt + 1,
                    self.max_retries,
                    exc_info=True,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

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
                "content": (
                    f"已有摘要：{previous_summary or '无'}\n\n"
                    "新消息 JSON：\n"
                    f"{json.dumps(messages, ensure_ascii=False, indent=2)}"
                ),
            },
        ]
        response = await self.chat(prompt)
        return response.choices[0].message.content or previous_summary


def _is_retryable_llm_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 409, 429} or status_code >= 500
    return True
