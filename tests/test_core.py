from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from herobot.bot import parse_allowed_user_ids, parse_bool, parse_scheduling_confirmation
from herobot.agent import note_search_terms
from herobot.bot2bot import (
    build_collaboration_prompt,
    build_followup_prompt,
    build_request_message,
    build_synthesis_prompt,
    choose_target_bot,
    clean_user_request,
    first_mentioned_bot,
    is_first_mentioned_bot,
    is_natural_bot_request,
    looks_like_delegation,
    natural_request_body,
    parse_bot_call,
    parse_bot_usernames,
    strip_envelope,
)
from herobot.storage import Storage
from herobot.tools import ToolContext, ToolRunner, calculate
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
        self.assertTrue(looks_like_delegation(text, "super666666_bot"))
        self.assertFalse(looks_like_delegation(text, "other_bot"))
        self.assertEqual(first_mentioned_bot(text), "super666666_bot")
        self.assertTrue(is_first_mentioned_bot(text, "super666666_bot"))
        self.assertFalse(is_first_mentioned_bot(text, "other_bot"))
        self.assertEqual(choose_target_bot(text, "super666666_bot"), "other_bot")
        self.assertEqual(
            clean_user_request(text, "other_bot", "super666666_bot"),
            "这个数学问题",
        )

        request = build_request_message(
            target_username="other_bot",
            from_username="super666666_bot",
            call_id="abc123",
            request="讨论一下这个数学问题",
            max_depth=3,
        )
        call = parse_bot_call(request)
        self.assertIsNone(call)
        self.assertTrue(is_natural_bot_request(request, "other_bot"))
        self.assertIn("讨论一下这个数学问题", natural_request_body(request, "other_bot"))
        self.assertIn("讨论一下这个数学问题", strip_envelope(request))
        self.assertIn("你怎么看", request)
        self.assertNotIn("请回答这个问题或任务", request)
        self.assertNotIn("你正在和另一个 Telegram bot", request)
        self.assertNotIn("[herobot-depth:", request)
        self.assertNotIn("[herobot-call-id:", request)
        collaboration = build_collaboration_prompt("问题")
        self.assertIn("使用纯文本", collaboration)
        self.assertIn("不要 Markdown", collaboration)
        self.assertIn("不要每轮都固定追问", collaboration)
        followup_request = build_request_message(
            target_username="other_bot",
            from_username="super666666_bot",
            call_id="abc123",
            request="继续追问",
            max_depth=3,
            depth=2,
        )
        self.assertNotIn("[herobot-depth:", followup_request)

        followup = build_followup_prompt("other_bot", "问题", "观点")
        self.assertIn("作为发起方参与讨论", followup)
        self.assertIn("不要用问题结尾", followup)
        self.assertIn("不要写最终结论", followup)

        synthesis = build_synthesis_prompt("other_bot", "问题", "观点")
        self.assertIn("不要说", synthesis)
        self.assertIn("150 字以内", synthesis)

    def test_calculate(self) -> None:
        self.assertEqual(calculate("1 + 2 * 3"), "7")

    def test_note_search_terms(self) -> None:
        self.assertIn("护照", note_search_terms("我护照在哪里？"))

    async def test_storage_backed_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage(str(Path(tmp) / "test.sqlite3"))
            await storage.init()
            runner = ToolRunner(storage)
            context = ToolContext(chat_id=10, user_id=20)

            todo = json.loads(await runner.run("create_todo", {"title": "买牛奶"}, context))
            self.assertTrue(todo["ok"])
            todos = await storage.list_todos(10)
            self.assertEqual(todos[0]["title"], "买牛奶")

            note = json.loads(
                await runner.run(
                    "create_note",
                    {"title": "护照", "content": "放在抽屉里"},
                    context,
                )
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
            runner = ToolRunner(storage)
            context = ToolContext(chat_id=10, user_id=20)

            result = json.loads(
                await runner.run(
                    "create_reminder",
                    {"content": "喝水", "remind_at": "2026-05-28T14:30:00+08:00"},
                    context,
                )
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


if __name__ == "__main__":
    unittest.main()
