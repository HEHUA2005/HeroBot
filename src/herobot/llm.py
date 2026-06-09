from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str


# REVIEW: LLMClient 封装得太薄了，几乎只是 AsyncOpenAI 的透传。
# 缺少以下生产级必需品：
# 1. 重试和退避（网络抖动、429、5xx）
# 2. 超时设置（当前没有 timeout，LLM 卡住会无限等待）
# 3. 请求/响应日志（调试 agent 行为的关键）
# 4. token 用量统计（不统计的话，API 费用根本没法追踪）
# 5. 流式响应支持（长回复时用户要等很久才能看到结果）
#
# 另外 if "://" not in base_url 的检查比较粗糙——
# "ftp://example.com" 也会通过检查但不是合法的 OpenAI 端点。
# 建议用 urllib.parse 做正确的 URL 处理。
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

    # REVIEW: summarize 方法有一个很隐蔽的问题：
    # f"新消息：{messages}" 会把 Python list[dict] 直接转成字符串表示，
    # LLM 收到的内容类似：
    #   新消息：[{'role': 'user', 'content': '帮我记一下护照在抽屉里'}, ...]
    # 这不是 LLM 容易理解的格式。应该格式化为可读的对话文本，比如：
    #   用户: 帮我记一下护照在抽屉里
    #   助手: 已记录。
    #
    # 另外 temperature=0.2 是在 chat() 里硬编码的，对于摘要任务可能需要不同的温度。
    # 建议 chat() 接受 temperature 参数，或者 summarize 用自己的调用。
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
