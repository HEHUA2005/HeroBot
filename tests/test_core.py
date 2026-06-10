from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from telegram.error import BadRequest, Forbidden

from herobot.telegram.message_utils import (
    extract_mentions,
    first_mentioned_bot,
    is_first_mentioned_bot,
    parse_bot_usernames,
    starts_with_bot_mention,
)
from herobot.core.config import load_config, parse_allowed_user_ids, parse_bool
from herobot.core.conversation_store import ConversationStore
from herobot.core.scheduler import _deliver_reminder
from herobot.llm import LLMClient, LLMConfig
from herobot.mcp.builtin.calendar.scheduling import (
    TimeWindow,
    find_free_windows,
    intersect_windows,
    merge_windows,
    parse_iso_text_window,
    to_utc_iso,
)
from herobot.mcp.builtin.calendar.storage import CalendarStorage
from herobot.mcp.builtin.calendar.tools import CalendarTools
from herobot.mcp.builtin.notes.storage import NotesStorage
from herobot.mcp.builtin.notes.tools import NotesTools
from herobot.mcp.config import MCPServerConfig
from herobot.mcp.context import ToolContext, context_from_payload
from herobot.mcp.registry import MCPToolRegistry, _MCPServerConnection, _ToolBinding
from herobot.agent import Agent, AgentEvent
from herobot.agent.task import (
    ActionLedger,
    TaskFrame,
    deterministic_finish_check,
    infer_expected_messages,
)
from herobot.telegram.platform_tools import (
    RecordingPlatformTools,
    TelegramPlatformTools,
    split_telegram_text,
)
from herobot.telegram.commands import alias_to_text, command_name
from herobot.telegram.message_utils import strip_bot_mention


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

    def test_default_config_uses_builtin_mcp_servers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "fallback.sqlite3")
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "OPENAI_API_KEY": "key",
                    "HEROBOT_DB_PATH": db_path,
                    "TELEGRAM_ALLOWED_USER_IDS": "1,2",
                },
            ):
                config = load_config(Path(tmp) / "missing.toml")
            self.assertEqual(config.core.core_db_path, db_path)
            self.assertEqual(config.llm.timeout_seconds, 60)
            self.assertEqual(config.llm.max_retries, 2)
            self.assertEqual({server.name for server in config.mcp_servers}, {"notes", "calendar"})
            calendar = next(server for server in config.mcp_servers if server.name == "calendar")
            self.assertIn("list_due_reminders", calendar.hidden_tools)

    def test_config_loads_llm_retry_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "OPENAI_API_KEY": "key",
                    "HEROBOT_LLM_TIMEOUT_SECONDS": "12.5",
                    "HEROBOT_LLM_MAX_RETRIES": "4",
                    "HEROBOT_LLM_RETRY_BASE_DELAY_SECONDS": "0.25",
                },
                clear=False,
            ):
                config = load_config(Path(tmp) / "missing.toml")
            self.assertEqual(config.llm.timeout_seconds, 12.5)
            self.assertEqual(config.llm.max_retries, 4)
            self.assertEqual(config.llm.retry_base_delay_seconds, 0.25)

    def test_config_loads_mcp_json_with_default_servers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "weather": {
                                "command": "npx",
                                "args": ["-y", "@dangahagan/weather-mcp@latest"],
                                "env": {"WEATHER_UNITS": "metric"},
                                "cwd": "/tmp/weather",
                                "timeout_seconds": 12.5,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "token", "OPENAI_API_KEY": "key"},
                clear=False,
            ):
                config = load_config(root / "missing.toml")

            servers = {server.name: server for server in config.mcp_servers}
            self.assertEqual({"notes", "calendar", "weather"}, set(servers))
            self.assertEqual(servers["weather"].command, "npx")
            self.assertEqual(
                servers["weather"].args,
                ["-y", "@dangahagan/weather-mcp@latest"],
            )
            self.assertEqual(servers["weather"].env, {"WEATHER_UNITS": "metric"})
            self.assertEqual(servers["weather"].cwd, "/tmp/weather")
            self.assertEqual(servers["weather"].timeout_seconds, 12.5)
            self.assertFalse(servers["weather"].required)
            self.assertEqual(config.mcp_json_config_paths, (root / "mcp.json",))
            self.assertEqual(config.mcp_json_server_names, ("weather",))

    def test_config_merges_toml_and_mcp_json_with_toml_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "herobot.toml"
            config_path.write_text(
                """
                [[mcp.servers]]
                name = "weather"
                command = "python"
                args = ["-m", "toml_weather"]
                required = true
                """,
                encoding="utf-8",
            )
            (root / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "weather": {"command": "npx", "args": ["json-weather"]},
                            "search": {"command": "uvx", "args": ["search-mcp"]},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "token", "OPENAI_API_KEY": "key"},
                clear=False,
            ):
                with self.assertLogs("herobot.core.config", level="WARNING"):
                    config = load_config(config_path)

            servers = {server.name: server for server in config.mcp_servers}
            self.assertEqual({"weather", "search"}, set(servers))
            self.assertEqual(servers["weather"].command, "python")
            self.assertEqual(servers["weather"].args, ["-m", "toml_weather"])
            self.assertTrue(servers["weather"].required)
            self.assertEqual(servers["search"].command, "uvx")
            self.assertFalse(servers["search"].required)
            self.assertEqual(config.mcp_json_server_names, ("search",))

    def test_config_supports_servers_key_disabled_and_missing_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "servers": {
                            "enabled_server": {
                                "command": "node",
                                "args": ["server.js"],
                                "required": True,
                                "exposed_tools": ["get_weather"],
                                "hidden_tools": ["refresh_cache"],
                            },
                            "disabled_server": {"command": "node", "disabled": True},
                            "also_disabled": {"command": "node", "enabled": False},
                            "missing_command": {"args": ["server.js"]},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "token", "OPENAI_API_KEY": "key"},
                clear=False,
            ):
                with self.assertLogs("herobot.core.config", level="WARNING"):
                    config = load_config(root / "missing.toml")

            servers = {server.name: server for server in config.mcp_servers}
            self.assertIn("enabled_server", servers)
            self.assertNotIn("disabled_server", servers)
            self.assertNotIn("also_disabled", servers)
            self.assertNotIn("missing_command", servers)
            self.assertTrue(servers["enabled_server"].required)
            self.assertEqual(servers["enabled_server"].exposed_tools, ["get_weather"])
            self.assertEqual(servers["enabled_server"].hidden_tools, ["refresh_cache"])

    def test_mcp_json_example_is_parseable(self) -> None:
        payload = json.loads(Path("mcp.json.example").read_text(encoding="utf-8"))

        self.assertIn("mcpServers", payload)
        self.assertIn("weather", payload["mcpServers"])
        self.assertEqual(payload["mcpServers"]["weather"]["command"], "npx")

    def test_config_rejects_malformed_mcp_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mcp.json").write_text("{not json", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "token", "OPENAI_API_KEY": "key"},
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "Invalid MCP JSON config"):
                    load_config(root / "missing.toml")

    def test_context_from_payload_requires_chat_and_user(self) -> None:
        with self.assertRaisesRegex(ValueError, "chat_id"):
            context_from_payload({"herobot_context": {"user_id": 20}})
        with self.assertRaisesRegex(ValueError, "user_id"):
            context_from_payload({"herobot_context": {"chat_id": 10}})

    def test_bot_to_bot_helpers(self) -> None:
        self.assertEqual(parse_bot_usernames("@Other_Bot, review_bot"), {"other_bot", "review_bot"})
        text = "@super666666_bot 请你和 @other_bot 讨论一下这个数学问题"
        self.assertEqual(extract_mentions(text), ["super666666_bot", "other_bot"])
        self.assertEqual(first_mentioned_bot(text), "super666666_bot")
        self.assertTrue(is_first_mentioned_bot(text, "super666666_bot"))
        self.assertFalse(is_first_mentioned_bot(text, "other_bot"))
        self.assertTrue(starts_with_bot_mention("@HEHUAone_bot\n你好", "HEHUAone_bot"))
        self.assertFalse(starts_with_bot_mention("李雷：@HEHUAone_bot", "HEHUAone_bot"))

    def test_mentioned_command_can_be_dispatched_after_strip(self) -> None:
        text = strip_bot_mention("@super666666_bot /reset", "super666666_bot")
        self.assertEqual(text, "/reset")
        self.assertEqual(command_name(text), "reset")
        self.assertEqual(
            alias_to_text("/calendar", "/calendar", {"calendar": "查看未来 7 天日程"}),
            "查看未来 7 天日程",
        )

    def test_agent_event_prompt_marks_current_mentions(self) -> None:
        event = AgentEvent(
            source="telegram",
            chat_id=10,
            chat_type="group",
            message_id=1,
            sender_id=20,
            sender_username="tester",
            sender_is_bot=False,
            text="你能做什么？",
            mentioned_usernames=[],
            addressed_to_self=True,
            self_bot_id=99,
            self_bot_username="super666666_bot",
            owner_user_id=20,
        )
        prompt = event.as_prompt()
        self.assertIn("current_message_target_mentions: none", prompt)
        self.assertIn("self_bot_username: super666666_bot", prompt)
        self.assertIn("消息内容：\n你能做什么？", prompt)

    def test_tool_capability_prompt_uses_current_tools_only(self) -> None:
        agent = Agent(storage=FakeConversationStore(), llm=FakeLLM([]), tool_registry=FakeRegistry())
        prompt = agent._tool_capability_prompt(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "create_calendar_event",
                        "description": "Create a calendar event.",
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "send_telegram_message",
                        "description": "Send a Telegram message.",
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "send_telegram_messages",
                        "description": "Send multiple Telegram messages.",
                    },
                },
            ]
        )
        self.assertIn("create_calendar_event", prompt)
        self.assertIn("send_telegram_message", prompt)
        self.assertIn("send_telegram_messages", prompt)
        self.assertNotIn("create_note", prompt)
        self.assertIn("未列出的工具", prompt)

    async def test_llm_chat_retries_transient_failures(self) -> None:
        llm = LLMClient(
            LLMConfig(
                api_key="key",
                base_url="http://localhost:4000",
                model="model",
                timeout_seconds=1,
                max_retries=1,
                retry_base_delay_seconds=0,
            )
        )
        completions = FakeCompletions([RuntimeError("temporary"), FakeResponse(FakeMessage(content="ok"))])
        llm.client = FakeOpenAIClient(completions)

        with self.assertLogs("herobot.llm", level="WARNING"):
            response = await llm.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(response.choices[0].message.content, "ok")
        self.assertEqual(completions.calls, 2)

    async def test_llm_chat_does_not_retry_bad_request(self) -> None:
        llm = LLMClient(
            LLMConfig(
                api_key="key",
                base_url="http://localhost:4000",
                model="model",
                timeout_seconds=1,
                max_retries=2,
                retry_base_delay_seconds=0,
            )
        )
        completions = FakeCompletions([FakeStatusError("bad request", status_code=400)])
        llm.client = FakeOpenAIClient(completions)

        with self.assertLogs("herobot.llm", level="ERROR"):
            with self.assertRaises(FakeStatusError):
                await llm.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(completions.calls, 1)

    async def test_llm_summarize_uses_json_message_format(self) -> None:
        llm = LLMClient(
            LLMConfig(
                api_key="key",
                base_url="http://localhost:4000",
                model="model",
                timeout_seconds=1,
                max_retries=0,
            )
        )
        completions = FakeCompletions([FakeResponse(FakeMessage(content="摘要"))])
        llm.client = FakeOpenAIClient(completions)

        summary = await llm.summarize([{"role": "user", "content": "hello"}], "旧摘要")

        self.assertEqual(summary, "摘要")
        content = completions.last_kwargs["messages"][1]["content"]
        self.assertIn("新消息 JSON", content)
        self.assertIn('"role": "user"', content)
        self.assertNotIn("[{'role':", content)

    async def test_recent_messages_filters_roles_before_limit_and_excludes_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            await storage.add_message(10, "user", "old user")
            await storage.add_message(10, "tool", "tool observation")
            await storage.add_message(10, "assistant", "old assistant")
            current_id = await storage.add_message(10, "user", "current user")

            recent = await storage.recent_messages(10, limit=2, exclude_message_id=current_id)

            self.assertEqual(
                recent,
                [
                    {"role": "user", "content": "old user"},
                    {"role": "assistant", "content": "old assistant"},
                ],
            )

    async def test_build_messages_does_not_duplicate_current_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            agent = Agent(storage=storage, llm=FakeLLM([]), tool_registry=FakeRegistry())
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="private",
                message_id=1,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="hello",
                addressed_to_self=True,
                owner_user_id=20,
            )
            current_id = await storage.add_message(event.chat_id, "user", event.as_prompt())

            messages = await agent._build_messages(
                event,
                [],
                TaskFrame.fallback(event),
                ActionLedger(),
                exclude_message_id=current_id,
            )

            self.assertEqual(
                sum(event.as_prompt() in item.get("content", "") for item in messages),
                1,
            )

    async def test_summary_runs_when_message_delta_exceeds_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            for index in range(25):
                await storage.add_message(10, "user", f"message {index}")
            llm = SummarizingFakeLLM("新的摘要")
            agent = Agent(storage=storage, llm=llm, tool_registry=FakeRegistry())

            await agent._maybe_summarize(10)

            self.assertEqual(llm.summarize_calls, 1)
            self.assertEqual(await storage.get_summary(10), "新的摘要")
            self.assertEqual(await storage.get_summary_message_count(10), 25)

    def test_task_frame_infers_counting_sequence(self) -> None:
        self.assertEqual(infer_expected_messages("挨个报数，数到10"), [str(i) for i in range(1, 11)])
        event = AgentEvent(
            source="telegram",
            chat_id=10,
            chat_type="group",
            message_id=1,
            sender_id=20,
            sender_username="tester",
            sender_is_bot=False,
            text="挨个报数，数到3",
            addressed_to_self=True,
        )
        task = TaskFrame.from_json_text("{}", event)
        self.assertEqual(task.expected_messages, ["1", "2", "3"])

    def test_action_ledger_records_single_and_batch_messages(self) -> None:
        ledger = ActionLedger()
        ledger.record(
            "send_telegram_message",
            {"text": "1"},
            json.dumps({"ok": True, "result": {"text": "1"}}, ensure_ascii=False),
        )
        ledger.record(
            "send_telegram_messages",
            {"messages": ["2", "3"]},
            json.dumps(
                {"ok": True, "result": [{"text": "2"}, {"text": "3"}]},
                ensure_ascii=False,
            ),
        )
        self.assertEqual(ledger.sent_messages, ["1", "2", "3"])

    def test_finish_guard_rejects_incomplete_expected_messages(self) -> None:
        task = TaskFrame(
            goal="挨个报数，数到3",
            completion_conditions=["必须按顺序成功发送 3 条独立 Telegram 消息"],
            expected_messages=["1", "2", "3"],
        )
        ledger = ActionLedger()
        ledger.record(
            "send_telegram_message",
            {"text": "1"},
            json.dumps({"ok": True, "result": {"text": "1"}}, ensure_ascii=False),
        )
        rejected = deterministic_finish_check(task, ledger, {"status": "done"})
        self.assertFalse(rejected.accepted)
        ledger.record(
            "send_telegram_messages",
            {"messages": ["2", "3"]},
            json.dumps(
                {"ok": True, "result": [{"text": "2"}, {"text": "3"}]},
                ensure_ascii=False,
            ),
        )
        accepted = deterministic_finish_check(task, ledger, {"status": "done"})
        self.assertTrue(accepted.accepted)

    def test_finish_guard_rejects_done_after_tool_failure(self) -> None:
        task = TaskFrame(goal="保存联系人", completion_conditions=["联系人保存成功"])
        ledger = ActionLedger()
        ledger.record(
            "add_contact",
            {"name": "李雷"},
            json.dumps({"ok": False, "error": "bot_username is required"}, ensure_ascii=False),
        )
        decision = deterministic_finish_check(task, ledger, {"status": "done"})
        self.assertFalse(decision.accepted)
        self.assertIn("add_contact", decision.missing_conditions[0])

    def test_finish_guard_allows_blocked_after_question_sent(self) -> None:
        task = TaskFrame(goal="创建提醒", completion_conditions=["需要确认时间"])
        ledger = ActionLedger()
        rejected = deterministic_finish_check(
            task,
            ledger,
            {"status": "blocked", "needs_user_input": True},
        )
        self.assertFalse(rejected.accepted)
        ledger.record(
            "send_telegram_message",
            {"text": "你想几点提醒？"},
            json.dumps({"ok": True, "result": {"text": "你想几点提醒？"}}, ensure_ascii=False),
        )
        accepted = deterministic_finish_check(
            task,
            ledger,
            {"status": "blocked", "needs_user_input": True},
        )
        self.assertTrue(accepted.accepted)

    async def test_storage_backed_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = NotesStorage(str(Path(tmp) / "notes.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            tools = NotesTools(storage)
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

            profile = await tools.invoke(
                "create_note",
                {
                    "title": "个人信息",
                    "content": "我是山西人，老家是晋城高平市，现在在上海读书。",
                },
                context,
            )
            self.assertTrue(profile["ok"])
            self.assertEqual((await storage.search_notes(10, "山西"))[0]["title"], "个人信息")
            self.assertEqual((await storage.search_notes(10, "晋城"))[0]["title"], "个人信息")
            self.assertEqual((await storage.search_notes(10, "上海"))[0]["title"], "个人信息")

            server = await tools.invoke(
                "create_note",
                {
                    "title": "服务器",
                    "content": "本地 LLM API 在 localhost:4000",
                },
                context,
            )
            self.assertTrue(server["ok"])
            self.assertEqual((await storage.search_notes(10, "localhost"))[0]["title"], "服务器")
            self.assertEqual((await storage.search_notes(10, "4000"))[0]["title"], "服务器")

    async def test_reminder_time_is_stored_as_utc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = CalendarStorage(str(Path(tmp) / "calendar.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            tools = CalendarTools(storage)
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
            storage = CalendarStorage(str(Path(tmp) / "calendar.sqlite3"))
            self.addAsyncCleanup(storage.close)
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

            tools = CalendarTools(storage)
            availability = await tools.invoke(
                "find_availability",
                {
                    "start_at": "2026-06-04T09:00:00+08:00",
                    "end_at": "2026-06-04T12:00:00+08:00",
                    "duration_minutes": 30,
                    "limit": 6,
                },
                ToolContext(chat_id=10, user_id=20, owner_user_id=20),
            )
            self.assertTrue(availability["ok"])
            self.assertIn("display", availability["result"][0])
            self.assertEqual(availability["result"][0]["display"], "2026-06-04 09:00-09:30")

    def test_windows_are_merged_before_intersection(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        base = datetime(2026, 6, 4, 9, 0, tzinfo=tz)
        merged = merge_windows(
            [
                TimeWindow(base, base.replace(hour=10)),
                TimeWindow(base.replace(hour=9, minute=30), base.replace(hour=11)),
                TimeWindow(base.replace(hour=11), base.replace(hour=11, minute=30)),
            ]
        )
        self.assertEqual(merged, [TimeWindow(base, base.replace(hour=11, minute=30))])

        common = intersect_windows(
            [
                TimeWindow(base, base.replace(hour=10)),
                TimeWindow(base.replace(hour=9, minute=30), base.replace(hour=10, minute=30)),
            ],
            [
                TimeWindow(base.replace(hour=9, minute=15), base.replace(hour=9, minute=45)),
                TimeWindow(base.replace(hour=9, minute=40), base.replace(hour=10, minute=15)),
            ],
            30,
        )
        self.assertEqual(
            common,
            [TimeWindow(base.replace(hour=9, minute=15), base.replace(hour=9, minute=45))],
        )

    async def test_reminder_send_failure_is_marked_and_does_not_raise(self) -> None:
        app = FakeSchedulerApp(send_error=Forbidden("bot was blocked by the user"))
        registry = FakeHiddenRegistry()
        context = ToolContext(chat_id=0, user_id=0)

        with self.assertLogs("herobot.core.scheduler", level="ERROR"):
            await _deliver_reminder(
                app,
                registry,
                context,
                {"id": 7, "chat_id": 10, "content": "喝水"},
            )

        self.assertEqual(len(app.bot.calls), 1)
        self.assertEqual(registry.hidden_calls, [("mark_reminder_sent", {"reminder_id": 7})])

    async def test_mcp_registry_lists_and_calls_business_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["HEROBOT_NOTES_DB_PATH"] = str(Path(tmp) / "notes.sqlite3")
            env["HEROBOT_CALENDAR_DB_PATH"] = str(Path(tmp) / "calendar.sqlite3")
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            registry = MCPToolRegistry(
                [
                    MCPServerConfig(
                        name="notes",
                        command=sys.executable,
                        args=["-m", "herobot.mcp.builtin.notes.server"],
                        env=env,
                    ),
                    MCPServerConfig(
                        name="calendar",
                        command=sys.executable,
                        args=["-m", "herobot.mcp.builtin.calendar.server"],
                        env=env,
                        hidden_tools=["list_due_reminders", "mark_reminder_sent"],
                    ),
                ]
            )
            await registry.start()
            try:
                schemas = registry.openai_tool_schemas()
                tool_names = {item["function"]["name"] for item in schemas}
                self.assertIn("create_note", tool_names)
                self.assertIn("create_calendar_event", tool_names)
                self.assertNotIn("list_due_reminders", tool_names)
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
                hidden = json.loads(
                    await registry.call_hidden(
                        "list_due_reminders",
                        {"now_iso": "2026-01-01T00:00:00+00:00"},
                        context,
                    )
                )
                self.assertTrue(hidden["ok"])
            finally:
                await registry.close()

    async def test_builtin_mcp_command_falls_back_to_python_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["HEROBOT_NOTES_DB_PATH"] = str(Path(tmp) / "notes.sqlite3")
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            env["PATH"] = ""
            registry = MCPToolRegistry(
                MCPServerConfig(
                    name="notes",
                    command="herobot-mcp-notes",
                    env=env,
                )
            )
            await registry.start()
            try:
                schemas = registry.openai_tool_schemas()
                self.assertIn("create_note", {item["function"]["name"] for item in schemas})
            finally:
                await registry.close()

    def test_builtin_mcp_command_resolution_uses_child_env_path(self) -> None:
        connection = _MCPServerConnection(
            MCPServerConfig(
                name="notes",
                command="herobot-mcp-notes",
                args=["--example"],
            )
        )

        command, args = connection._resolved_command({"PATH": ""})

        self.assertEqual(command, sys.executable)
        self.assertEqual(args, ["-m", "herobot.mcp.builtin.notes.server", "--example"])

    async def test_mcp_connection_call_times_out(self) -> None:
        connection = _MCPServerConnection(
            MCPServerConfig(name="slow", command="unused", timeout_seconds=0.01)
        )
        connection._session = FakeSlowSession()

        with self.assertRaises(asyncio.TimeoutError):
            await connection.call("slow_tool", {})

    async def test_mcp_registry_returns_error_on_tool_timeout(self) -> None:
        registry = MCPToolRegistry([])
        registry._servers = [FakeTimeoutMCPConnection()]
        registry._bindings = {
            "slow_tool": _ToolBinding("slow", FakeMCPTool(), hidden=False),
        }

        with self.assertLogs("herobot.mcp.registry", level="WARNING"):
            result = json.loads(
                await registry.call("slow_tool", {}, ToolContext(chat_id=10, user_id=20))
            )

        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])

    async def test_agent_runtime_uses_mcp_and_finish_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            core_db_path = str(Path(tmp) / "core.sqlite3")
            notes_db_path = str(Path(tmp) / "notes.sqlite3")
            env["HEROBOT_NOTES_DB_PATH"] = notes_db_path
            env["PYTHONPATH"] = str(Path.cwd() / "src")
            storage = ConversationStore(core_db_path)
            self.addAsyncCleanup(storage.close)
            await storage.init()
            registry = MCPToolRegistry(
                MCPServerConfig(
                    name="notes",
                    command=sys.executable,
                    args=["-m", "herobot.mcp.builtin.notes.server"],
                    env=env,
                )
            )
            await registry.start()
            try:
                llm = FakeLLM(
                    [
                        json.dumps(
                            {
                                "goal": "记录学校",
                                "subtasks": ["保存笔记", "回复用户"],
                                "completion_conditions": ["笔记保存成功并回复用户"],
                                "expected_messages": [],
                                "requires_user_input": False,
                                "requires_external_response": False,
                            },
                            ensure_ascii=False,
                        ),
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
                        ],
                        json.dumps(
                            {
                                "accepted": True,
                                "reason": "笔记保存成功且已回复用户",
                                "missing_conditions": [],
                            },
                            ensure_ascii=False,
                        ),
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
                notes_storage = NotesStorage(notes_db_path)
                self.addAsyncCleanup(notes_storage.close)
                await notes_storage.init()
                notes = await notes_storage.search_notes(10, "学校")
                self.assertEqual(notes[0]["content"], "上海交通大学")
            finally:
                await registry.close()

    async def test_agent_rejects_finish_until_expected_messages_are_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            registry = FakeRegistry()
            llm = FakeLLM(
                [
                    json.dumps(
                        {
                            "goal": "挨个报数到3",
                            "subtasks": ["发送 1", "发送 2", "发送 3"],
                            "completion_conditions": ["必须发送 1、2、3 三条独立消息"],
                            "expected_messages": ["1", "2", "3"],
                            "requires_user_input": False,
                            "requires_external_response": False,
                        },
                        ensure_ascii=False,
                    ),
                    [
                        FakeToolCall("1", "send_telegram_message", {"text": "1"}),
                        FakeToolCall(
                            "2",
                            "finish_task",
                            {
                                "status": "done",
                                "summary": "counted",
                                "needs_user_input": False,
                            },
                        ),
                    ],
                    [
                        FakeToolCall(
                            "3",
                            "send_telegram_messages",
                            {"messages": ["2", "3"]},
                        ),
                        FakeToolCall(
                            "4",
                            "finish_task",
                            {
                                "status": "done",
                                "summary": "counted to 3",
                                "needs_user_input": False,
                            },
                        ),
                    ],
                ]
            )
            agent = Agent(storage=storage, llm=llm, tool_registry=registry, max_steps=4)
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="group",
                message_id=1,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="挨个报数，数到3",
                addressed_to_self=True,
            )
            platform = RecordingPlatformTools()
            await agent.handle_event(event, platform)
            self.assertTrue(platform.finished)
            self.assertEqual(
                [args for name, args in platform.calls if name == "send_telegram_message"],
                [{"text": "1"}],
            )
            self.assertEqual(
                [args for name, args in platform.calls if name == "send_telegram_messages"],
                [{"messages": ["2", "3"]}],
            )
            self.assertEqual(
                len([name for name, _ in platform.calls if name == "finish_task"]),
                1,
            )

    async def test_agent_max_steps_does_not_warn_after_user_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            registry = FakeRegistry()
            llm = FakeLLM(
                [
                    json.dumps(
                        {
                            "goal": "回答天气是否适合出门",
                            "subtasks": ["回复用户"],
                            "completion_conditions": ["给用户可观察回复"],
                            "expected_messages": [],
                            "requires_user_input": False,
                            "requires_external_response": False,
                        },
                        ensure_ascii=False,
                    ),
                    [
                        FakeToolCall(
                            "1",
                            "send_telegram_message",
                            {"text": "上海闵行今天适合出门，注意防晒。"},
                        )
                    ],
                ]
            )
            agent = Agent(storage=storage, llm=llm, tool_registry=registry, max_steps=1)
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="group",
                message_id=1,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="今天上海闵行天气怎么样？适合出门吗",
                addressed_to_self=True,
            )
            platform = RecordingPlatformTools()

            await agent.handle_event(event, platform)

            sent = [args["text"] for name, args in platform.calls if name == "send_telegram_message"]
            finishes = [args for name, args in platform.calls if name == "finish_task"]
            self.assertEqual(sent, ["上海闵行今天适合出门，注意防晒。"])
            self.assertEqual(finishes[-1]["status"], "done")
            self.assertFalse(finishes[-1]["needs_user_input"])

    async def test_agent_max_steps_does_not_warn_after_response_with_tool_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            registry = FakeFailingWeatherRegistry()
            llm = FakeLLM(
                [
                    json.dumps(
                        {
                            "goal": "查询天气并回复用户",
                            "subtasks": ["查询天气", "回复用户"],
                            "completion_conditions": ["给用户可观察回复"],
                            "expected_messages": [],
                            "requires_user_input": False,
                            "requires_external_response": False,
                        },
                        ensure_ascii=False,
                    ),
                    [
                        FakeToolCall("1", "get_weather", {"location": "上海交大"}),
                        FakeToolCall(
                            "2",
                            "send_telegram_message",
                            {"text": "上海交大今天适合出门，注意防晒。"},
                        ),
                    ],
                ]
            )
            agent = Agent(storage=storage, llm=llm, tool_registry=registry, max_steps=1)
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="group",
                message_id=1,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="帮我查一下今天上海交大天气怎么样",
                addressed_to_self=True,
            )
            platform = RecordingPlatformTools()

            await agent.handle_event(event, platform)

            sent = [args["text"] for name, args in platform.calls if name == "send_telegram_message"]
            finishes = [args for name, args in platform.calls if name == "finish_task"]
            self.assertEqual(sent, ["上海交大今天适合出门，注意防晒。"])
            self.assertEqual(finishes[-1]["status"], "failed")
            self.assertFalse(finishes[-1]["needs_user_input"])

    async def test_agent_finish_after_user_response_skips_llm_finish_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            registry = FakeRegistry()
            llm = FakeLLM(
                [
                    json.dumps(
                        {
                            "goal": "回复天气",
                            "subtasks": ["回复用户"],
                            "completion_conditions": ["已回复用户"],
                            "expected_messages": [],
                            "requires_user_input": False,
                            "requires_external_response": False,
                        },
                        ensure_ascii=False,
                    ),
                    [
                        FakeToolCall(
                            "1",
                            "send_telegram_message",
                            {"text": "上海交大今天适合出门。"},
                        ),
                        FakeToolCall(
                            "2",
                            "finish_task",
                            {
                                "status": "done",
                                "summary": "weather answered",
                                "needs_user_input": False,
                            },
                        ),
                    ],
                ]
            )
            agent = Agent(storage=storage, llm=llm, tool_registry=registry, max_steps=3)
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="group",
                message_id=1,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="帮我查一下今天上海交大天气怎么样",
                addressed_to_self=True,
            )
            platform = RecordingPlatformTools()

            await agent.handle_event(event, platform)

            self.assertTrue(platform.finished)
            self.assertEqual(llm.calls, 2)

    async def test_agent_max_steps_warns_when_expected_messages_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            registry = FakeRegistry()
            llm = FakeLLM(
                [
                    json.dumps(
                        {
                            "goal": "挨个报数到2",
                            "subtasks": ["发送 1", "发送 2"],
                            "completion_conditions": ["必须发送 1、2 两条独立消息"],
                            "expected_messages": ["1", "2"],
                            "requires_user_input": False,
                            "requires_external_response": False,
                        },
                        ensure_ascii=False,
                    ),
                    [FakeToolCall("1", "send_telegram_message", {"text": "1"})],
                ]
            )
            agent = Agent(storage=storage, llm=llm, tool_registry=registry, max_steps=1)
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="group",
                message_id=1,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="挨个报数，数到2",
                addressed_to_self=True,
            )
            platform = RecordingPlatformTools()

            await agent.handle_event(event, platform)

            sent = [args["text"] for name, args in platform.calls if name == "send_telegram_message"]
            finishes = [args for name, args in platform.calls if name == "finish_task"]
            self.assertEqual(sent[0], "1")
            self.assertIn("执行步数达到了上限", sent[1])
            self.assertEqual(finishes[-1]["status"], "failed")
            self.assertTrue(finishes[-1]["needs_user_input"])

    async def test_agent_recovers_when_llm_fails_after_side_effect_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "core.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            registry = FakeSideEffectRegistry()
            llm = FakeLLM(
                [
                    json.dumps(
                        {
                            "goal": "记录用户个人信息",
                            "subtasks": ["保存笔记", "回复用户"],
                            "completion_conditions": ["信息保存成功并回复用户"],
                            "expected_messages": [],
                            "requires_user_input": False,
                            "requires_external_response": False,
                        },
                        ensure_ascii=False,
                    ),
                    [
                        FakeToolCall(
                            "1",
                            "create_note",
                            {
                                "title": "个人信息",
                                "content": "用户是山西人，老家是晋城高平市，现在在上海读书。",
                            },
                        )
                    ],
                    RuntimeError("upstream chat-completions failed after tool result"),
                ]
            )
            agent = Agent(storage=storage, llm=llm, tool_registry=registry, max_steps=3)
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="group",
                message_id=1,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="记住，我是山西人，我的老家是晋城高平市，我现在在上海读书",
                addressed_to_self=True,
            )
            platform = RecordingPlatformTools()

            with self.assertLogs("herobot.agent.runtime", level="ERROR"):
                await agent.handle_event(event, platform)

            self.assertEqual(registry.calls[0][0], "create_note")
            sent = [args["text"] for name, args in platform.calls if name == "send_telegram_message"]
            finishes = [args for name, args in platform.calls if name == "finish_task"]
            self.assertEqual(sent, ["已记录。"])
            self.assertEqual(finishes[-1]["status"], "done")
            self.assertFalse(finishes[-1]["needs_user_input"])

    async def test_telegram_platform_retries_when_reply_message_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "platform.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="group",
                message_id=99,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="hello",
                addressed_to_self=True,
                owner_user_id=20,
                message_thread_id=7,
            )
            fake_context = FakeTelegramContext()
            platform = TelegramPlatformTools(fake_context, storage, event)
            result = json.loads(await platform.call("send_telegram_message", {"text": "你好"}))
            self.assertTrue(result["ok"])
            self.assertEqual(len(fake_context.bot.calls), 2)
            self.assertEqual(fake_context.bot.calls[0]["reply_to_message_id"], 99)
            self.assertNotIn("reply_to_message_id", fake_context.bot.calls[1])
            self.assertEqual(fake_context.bot.calls[1]["message_thread_id"], 7)

    async def test_telegram_platform_can_send_multiple_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "platform.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="group",
                message_id=99,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="挨个报数",
                addressed_to_self=True,
                owner_user_id=20,
            )
            fake_context = FakeTelegramContext()
            platform = TelegramPlatformTools(fake_context, storage, event)
            fake_context.bot.fail_first_reply = False
            result = json.loads(
                await platform.call("send_telegram_messages", {"messages": ["1", "2", "3"]})
            )
            self.assertTrue(result["ok"])
            self.assertEqual([item["text"] for item in fake_context.bot.calls], ["1", "2", "3"])

    def test_split_telegram_text_respects_message_limit(self) -> None:
        chunks = split_telegram_text("a" * 4090 + "\n" + "b" * 20)

        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        self.assertEqual("".join(chunks), "a" * 4090 + "b" * 20)

    async def test_telegram_platform_splits_long_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = ConversationStore(str(Path(tmp) / "platform.sqlite3"))
            self.addAsyncCleanup(storage.close)
            await storage.init()
            event = AgentEvent(
                source="telegram",
                chat_id=10,
                chat_type="private",
                message_id=99,
                sender_id=20,
                sender_username="tester",
                sender_is_bot=False,
                text="long",
                addressed_to_self=True,
                owner_user_id=20,
            )
            fake_context = FakeTelegramContext()
            fake_context.bot.fail_first_reply = False
            platform = TelegramPlatformTools(fake_context, storage, event)

            result = json.loads(
                await platform.call("send_telegram_message", {"text": "x" * 4100})
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["chunks"], 2)
            self.assertEqual(len(fake_context.bot.calls), 2)
            self.assertTrue(all(len(item["text"]) <= 4096 for item in fake_context.bot.calls))


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
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages, tools=None, tool_choice="auto"):
        del messages, tools, tool_choice
        self.calls += 1
        if not self.responses:
            return FakeResponse(FakeMessage([]))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            return FakeResponse(FakeMessage(content=response))
        return FakeResponse(FakeMessage(response))

    async def summarize(self, messages, previous_summary=""):
        del messages
        return previous_summary


class SummarizingFakeLLM(FakeLLM):
    def __init__(self, summary: str) -> None:
        super().__init__([])
        self.summary = summary
        self.summarize_calls = 0

    async def summarize(self, messages, previous_summary=""):
        del messages, previous_summary
        self.summarize_calls += 1
        return self.summary


class FakeCompletions:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.last_kwargs = {}

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeOpenAIChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeOpenAIClient:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = FakeOpenAIChat(completions)


class FakeStatusError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeConversationStore:
    pass


class FakeRegistry:
    tool_names = set()

    def openai_tool_schemas(self):
        return []

    async def start(self):
        return None

    async def close(self):
        return None


class FakeSideEffectRegistry(FakeRegistry):
    tool_names = {"create_note"}

    def __init__(self) -> None:
        self.calls = []

    def openai_tool_schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "create_note",
                    "description": "Create a personal note.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["title", "content"],
                    },
                },
            }
        ]

    async def call(self, name, arguments, context):
        self.calls.append((name, arguments, context))
        return json.dumps(
            {
                "ok": True,
                "result": {
                    "id": 1,
                    "title": arguments.get("title"),
                    "content": arguments.get("content"),
                },
            },
            ensure_ascii=False,
        )


class FakeFailingWeatherRegistry(FakeRegistry):
    tool_names = {"get_weather"}

    def openai_tool_schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather by location.",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                    },
                },
            }
        ]

    async def call(self, name, arguments, context):
        del name, arguments, context
        return json.dumps(
            {"ok": False, "error": "weather provider returned partial data"},
            ensure_ascii=False,
        )


class FakeHiddenRegistry:
    hidden_tool_names = {"list_due_reminders", "mark_reminder_sent"}

    def __init__(self) -> None:
        self.hidden_calls = []

    async def call_hidden(self, name, arguments, _context):
        self.hidden_calls.append((name, arguments))
        return json.dumps({"ok": True, "result": {"marked": True}}, ensure_ascii=False)


class FakeSlowSession:
    async def call_tool(self, name, payload):
        del name, payload
        await asyncio.sleep(1)


class FakeTimeoutMCPConnection:
    config = MCPServerConfig(name="slow", command="unused", timeout_seconds=0.01)

    async def call(self, name, payload):
        del name, payload
        raise asyncio.TimeoutError()


class FakeMCPTool:
    description = ""
    inputSchema = {}


class FakeSchedulerApp:
    def __init__(self, send_error=None) -> None:
        self.bot = FakeSchedulerBot(send_error)


class FakeSchedulerBot:
    def __init__(self, send_error=None) -> None:
        self.send_error = send_error
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.send_error is not None:
            raise self.send_error
        return FakeSentMessage()


class FakeSentMessage:
    chat_id = 10
    message_id = 123


class FakeBot:
    def __init__(self) -> None:
        self.calls = []
        self.fail_first_reply = True

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first_reply and len(self.calls) == 1:
            raise BadRequest("Message to be replied not found")
        return FakeSentMessage()


class FakeTelegramContext:
    def __init__(self) -> None:
        self.bot = FakeBot()


if __name__ == "__main__":
    unittest.main()
