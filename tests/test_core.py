from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from herobot.bot import parse_allowed_user_ids, parse_bool, parse_scheduling_confirmation
from herobot.agent import note_search_terms
from herobot.bot2bot import (
    extract_mentions,
    first_mentioned_bot,
    is_first_mentioned_bot,
    parse_bot_usernames,
)
from herobot.storage import Storage
from herobot.tools import BusinessTools, ToolContext
from herobot.tool_registry import MCPToolConfig, MCPToolRegistry
from herobot.agent import Agent, AgentEvent
from herobot.platform import RecordingPlatformTools
from herobot.scheduling import TimeWindow, find_free_windows, intersect_windows, parse_iso_text_window, to_utc_iso


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_allowed_user_ids(self) -> None:
        self.assertEqual(parse_allowed_user_ids("1, 2,3"), {1, 2, 3})
        self.assertEqual(parse_allowed_user_ids(""), set())
        self.assertEqual(parse_allowed_user_ids(None), set())

    def test_parse_bool(self) -> None:
        self.assertTrue(parse_bool("true", False))
        self.assertTrue(parse_bool("on", False))
        self.assertFalse(parse_bool("false", True))
        self.assertFalse(parse_bool("0", True))
        self.assertTrue(parse_bool(None, True))

    def test_parse_scheduling_confirmation(self) -> None:
        self.assertEqual(parse_scheduling_confirmation("可以", True), 0)
        self.assertIsNone(parse_scheduling_confirmation("可以", False))
        self.assertEqual(parse_scheduling_confirmation("确认第 1 个时间", False), 0)
        self.assertEqual(parse_scheduling_confirmation("选第二个", False), 1)
        self.assertIsNone(
            parse_scheduling_confirmation(
                "帮我记一下我的学校是上海交通大学，再帮我添加一个联系人 李雷 他的bot是 @HEHUAone_bot",
                True,
            )
        )

    def test_bot_to_bot_helpers(self) -> None:
        self.assertEqual(parse_bot_usernames("@Other_Bot, review_bot"), {"other_bot", "review_bot"})
        text = "@super666666_bot 请你和 @other_bot 讨论一下这个数学问题"
        self.assertEqual(extract_mentions(text), ["super666666_bot", "other_bot"])
        self.assertEqual(first_mentioned_bot(text), "super666666_bot")
        self.assertTrue(is_first_mentioned_bot(text, "super666666_bot"))
        self.assertFalse(is_first_mentioned_bot(text, "other_bot"))

    def test_note_search_terms(self) -> None:
        self.assertIn("护照", note_search_terms("我护照在哪里？"))

    async def test_storage_backed_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "test.sqlite3"))
            await storage.init()
            tools = BusinessTools(storage)
            context = ToolContext(chat_id=10, user_id=20)

            todo = await tools.invoke("create_todo", {"title": "买牛奶"}, context)
            self.assertTrue(todo["ok"])
            todos = await storage.list_todos(10)
            self.assertEqual(todos[0]["title"], "买牛奶")

            note = await tools.invoke(
                    "create_note",
                    {"title": "护照", "content": "放在抽屉里"},
                    context,
                )
            self.assertTrue(note["ok"])
            notes = await storage.search_notes(10, "护照")
            self.assertEqual(notes[0]["content"], "放在抽屉里")
            fuzzy_notes = await storage.search_notes_by_terms(10, ["护照"])
            self.assertEqual(fuzzy_notes[0]["title"], "护照")

    async def test_reminder_time_is_stored_as_utc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "test.sqlite3"))
            await storage.init()
            tools = BusinessTools(storage)
            context = ToolContext(chat_id=10, user_id=20)

            result = await tools.invoke(
                    "create_reminder",
                    {"content": "喝水", "remind_at": "2026-05-28T14:30:00+08:00"},
                    context,
                )

            self.assertTrue(result["ok"])
            remind_at = datetime.fromisoformat(result["result"]["remind_at"])
            self.assertEqual(remind_at.tzinfo, timezone.utc)
            self.assertEqual(remind_at.hour, 6)

    async def test_contacts_calendar_and_availability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "test.sqlite3"))
            await storage.init()
            contact = await storage.upsert_contact("李雷", "@lilei_bot")
            self.assertEqual(contact["bot_username"], "lilei_bot")
            self.assertEqual((await storage.get_contact("李雷"))["bot_username"], "lilei_bot")

            window = parse_iso_text_window("2026-06-04 09:00-12:00")
            self.assertIsNotNone(window)
            assert window is not None
            event = await storage.create_calendar_event(
                20,
                "已有会议",
                to_utc_iso(window.start.replace(hour=10)),
                to_utc_iso(window.start.replace(hour=11)),
            )
            self.assertEqual(event["title"], "已有会议")
            events = await storage.list_calendar_events(20, to_utc_iso(window.start), to_utc_iso(window.end))
            slots = find_free_windows(events, window, 30)
            self.assertEqual(len(slots), 2)
            self.assertEqual(slots[0].start.hour, 9)
            self.assertEqual(slots[1].start.astimezone(ZoneInfo("Asia/Shanghai")).hour, 11)

            peer_slots = [TimeWindow(window.start.replace(hour=9), window.start.replace(hour=9, minute=30))]
            common = intersect_windows(slots, peer_slots, 30)
            self.assertEqual(len(common), 1)
            self.assertEqual(common[0].start.hour, 9)
            self.assertEqual(common[0].start.minute, 0)

    async def test_mcp_registry_lists_and_calls_business_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["HEROBOT_DB_PATH"] = str(Path(tmp) / "mcp.sqlite3")
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            registry = MCPToolRegistry(
                MCPToolConfig(
                    command=sys.executable,
                    args=["-m", "herobot.mcp_server"],
                    env=env,
                )
            )
            await registry.start()
            try:
                schemas = registry.openai_tool_schemas()
                self.assertIn("create_note", {item["function"]["name"] for item in schemas})
                create_note_schema = next(
                    item for item in schemas if item["function"]["name"] == "create_note"
                )
                self.assertNotIn(
                    "herobot_context",
                    create_note_schema["function"]["parameters"].get("properties", {}),
                )
                context = ToolContext(chat_id=10, user_id=20, owner_user_id=20)
                result = json.loads(
                    await registry.call(
                        "create_note",
                        {"title": "学校", "content": "上海交通大学"},
                        context,
                    )
                )
                self.assertTrue(result["ok"])
            finally:
                await registry.close()

    async def test_agent_runtime_uses_mcp_and_finish_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            db_path = str(Path(tmp) / "agent.sqlite3")
            env["HEROBOT_DB_PATH"] = db_path
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            storage = Storage(db_path)
            await storage.init()
            registry = MCPToolRegistry(
                MCPToolConfig(
                    command=sys.executable,
                    args=["-m", "herobot.mcp_server"],
                    env=env,
                )
            )
            await registry.start()
            try:
                llm = FakeLLM(
                    [
                        [
                            FakeToolCall(
                                "1",
                                "create_note",
                                {"title": "学校", "content": "上海交通大学"},
                            ),
                            FakeToolCall(
                                "2",
                                "send_telegram_message",
                                {"text": "已记录学校。"},
                            ),
                            FakeToolCall(
                                "3",
                                "finish_task",
                                {
                                    "status": "done",
                                    "summary": "saved note",
                                    "needs_user_input": False,
                                },
                            ),
                        ]
                    ]
                )
                agent = Agent(storage=storage, llm=llm, tool_registry=registry, max_steps=3)
                event = AgentEvent(
                    source="telegram",
                    chat_id=10,
                    chat_type="private",
                    message_id=1,
                    sender_id=20,
                    sender_username="tester",
                    sender_is_bot=False,
                    text="帮我记一下我的学校是上海交通大学",
                    addressed_to_self=True,
                    owner_user_id=20,
                )
                platform = RecordingPlatformTools()
                await agent.handle_event(event, platform)
                self.assertTrue(platform.finished)
                self.assertIn("send_telegram_message", [name for name, _ in platform.calls])
                notes = await storage.search_notes(10, "学校")
                self.assertEqual(notes[0]["content"], "上海交通大学")
            finally:
                await registry.close()


class FakeFunction:
    def __init__(self, name: str, arguments: dict) -> None:
        self.name = name
        self.arguments = json.dumps(arguments, ensure_ascii=False)


class FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict) -> None:
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, tool_calls: list[FakeToolCall] | None = None, content: str = "") -> None:
        self.tool_calls = tool_calls
        self.content = content


class FakeChoice:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message


class FakeResponse:
    def __init__(self, message: FakeMessage) -> None:
        self.choices = [FakeChoice(message)]


class FakeLLM:
    def __init__(self, tool_call_batches: list[list[FakeToolCall]]) -> None:
        self.tool_call_batches = tool_call_batches

    async def chat(self, messages, tools=None, tool_choice="auto"):
        del messages, tools, tool_choice
        batch = self.tool_call_batches.pop(0)
        return FakeResponse(FakeMessage(batch))

    async def summarize(self, messages, previous_summary=""):
        del messages
        return previous_summary


if __name__ == "__main__":
    unittest.main()
