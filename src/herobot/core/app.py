from __future__ import annotations

from telegram.ext import Application

from herobot.agent import Agent
from herobot.core.config import HeroBotConfig
from herobot.core.conversation_store import ConversationStore
from herobot.core.scheduler import reminder_loop
from herobot.llm import LLMClient, LLMConfig
from herobot.mcp.registry import MCPToolRegistry
from herobot.telegram.adapter import register_handlers


async def post_init(app: Application) -> None:
    conversation_store: ConversationStore = app.bot_data["conversation_store"]
    await conversation_store.init()
    agent: Agent = app.bot_data["agent"]
    await agent.start()
    config: HeroBotConfig = app.bot_data["config"]
    tool_registry: MCPToolRegistry = app.bot_data["tool_registry"]
    app.create_task(
        reminder_loop(
            app,
            tool_registry,
            interval_seconds=config.core.reminder_interval_seconds,
            timezone_name=config.core.timezone,
        )
    )


async def post_shutdown(app: Application) -> None:
    agent: Agent = app.bot_data["agent"]
    await agent.close()


def build_application(config: HeroBotConfig) -> Application:
    app = (
        Application.builder()
        .token(config.telegram.token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    conversation_store = ConversationStore(config.core.core_db_path)
    llm = LLMClient(
        LLMConfig(
            api_key=config.llm.api_key,
            base_url=config.llm.base_url,
            model=config.llm.model,
        )
    )
    tool_registry = MCPToolRegistry(config.mcp_servers)
    app.bot_data["config"] = config
    app.bot_data["conversation_store"] = conversation_store
    app.bot_data["tool_registry"] = tool_registry
    app.bot_data["agent"] = Agent(
        storage=conversation_store,
        llm=llm,
        tool_registry=tool_registry,
        persona=config.core.persona,
        max_steps=config.core.max_agent_steps,
    )
    register_handlers(app, config)
    return app
