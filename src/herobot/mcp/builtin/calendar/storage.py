from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from herobot.core.conversation_store import utc_now


@dataclass(frozen=True)
class Reminder:
    id: int
    chat_id: int
    user_id: int
    content: str
    remind_at: str


class CalendarStorage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        path = Path(self.db_path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    sent_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    bot_username TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS calendar_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    reminder_at TEXT,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduling_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    contact_name TEXT NOT NULL,
                    contact_bot_username TEXT NOT NULL,
                    request_text TEXT NOT NULL,
                    duration_minutes INTEGER NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            await db.commit()

    async def create_reminder(
        self, chat_id: int, user_id: int, content: str, remind_at: str
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO reminders (chat_id, user_id, content, remind_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, content, remind_at, utc_now()),
            )
            await db.commit()
        return {
            "id": cursor.lastrowid,
            "content": content,
            "remind_at": remind_at,
            "sent": False,
        }

    async def list_reminders(self, chat_id: int, include_sent: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT id, content, remind_at, sent_at, created_at FROM reminders
            WHERE chat_id = ?
        """
        params: list[Any] = [chat_id]
        if not include_sent:
            query += " AND sent_at IS NULL"
        query += " ORDER BY remind_at ASC LIMIT 20"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(query, params)
        return [dict(row) for row in rows]

    async def due_reminders(self, now_iso: str) -> list[Reminder]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT id, chat_id, user_id, content, remind_at FROM reminders
                WHERE sent_at IS NULL AND remind_at <= ?
                ORDER BY remind_at ASC
                LIMIT 25
                """,
                (now_iso,),
            )
        return [Reminder(**dict(row)) for row in rows]

    async def mark_reminder_sent(self, reminder_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE reminders SET sent_at = ? WHERE id = ?",
                (utc_now(), reminder_id),
            )
            await db.commit()
        return {"id": reminder_id, "marked": cursor.rowcount > 0}

    async def upsert_contact(self, name: str, bot_username: str, note: str = "") -> dict[str, Any]:
        now = utc_now()
        username = bot_username.strip().lstrip("@")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO contacts (name, bot_username, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    bot_username = excluded.bot_username,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (name.strip(), username, note.strip(), now, now),
            )
            await db.commit()
        return {"name": name.strip(), "bot_username": username, "note": note.strip()}

    async def get_contact(self, name: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, name, bot_username, note, updated_at FROM contacts WHERE name = ?",
                (name.strip(),),
            )
        return dict(rows[0]) if rows else None

    async def list_contacts(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT id, name, bot_username, note, updated_at FROM contacts ORDER BY name ASC"
            )
        return [dict(row) for row in rows]

    async def delete_contact(self, name: str) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM contacts WHERE name = ?", (name.strip(),))
            await db.commit()
        return {"deleted": cursor.rowcount > 0, "name": name.strip()}

    async def create_calendar_event(
        self,
        user_id: int,
        title: str,
        start_at: str,
        end_at: str,
        note: str = "",
        reminder_at: str | None = None,
        status: str = "confirmed",
    ) -> dict[str, Any]:
        now = utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO calendar_events
                    (user_id, title, start_at, end_at, note, reminder_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, title, start_at, end_at, note, reminder_at, status, now, now),
            )
            await db.commit()
        return {
            "id": cursor.lastrowid,
            "title": title,
            "start_at": start_at,
            "end_at": end_at,
            "note": note,
            "reminder_at": reminder_at,
            "status": status,
        }

    async def list_calendar_events(
        self, user_id: int, start_at: str | None = None, end_at: str | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT id, title, start_at, end_at, note, reminder_at, status
            FROM calendar_events
            WHERE user_id = ? AND status != 'cancelled'
        """
        params: list[Any] = [user_id]
        if start_at is not None:
            query += " AND end_at > ?"
            params.append(start_at)
        if end_at is not None:
            query += " AND start_at < ?"
            params.append(end_at)
        query += " ORDER BY start_at ASC LIMIT 50"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(query, params)
        return [dict(row) for row in rows]

    async def create_scheduling_session(
        self,
        chat_id: int,
        owner_user_id: int,
        contact_name: str,
        contact_bot_username: str,
        request_text: str,
        duration_minutes: int,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any]:
        now = utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO scheduling_sessions
                    (chat_id, owner_user_id, contact_name, contact_bot_username, request_text,
                     duration_minutes, window_start, window_end, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    owner_user_id,
                    contact_name,
                    contact_bot_username,
                    request_text,
                    duration_minutes,
                    window_start,
                    window_end,
                    now,
                    now,
                ),
            )
            await db.commit()
        return {"id": cursor.lastrowid, "status": "pending"}

    async def update_scheduling_candidates(
        self, session_id: int, candidates: list[dict[str, Any]], status: str = "awaiting_confirmation"
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE scheduling_sessions
                SET candidates_json = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(candidates, ensure_ascii=False), status, utc_now(), session_id),
            )
            await db.commit()

    async def latest_pending_session(self, owner_user_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                """
                SELECT * FROM scheduling_sessions
                WHERE owner_user_id = ? AND status IN ('pending', 'awaiting_confirmation')
                ORDER BY id DESC LIMIT 1
                """,
                (owner_user_id,),
            )
        if not rows:
            return None
        session = dict(rows[0])
        session["candidates"] = json.loads(session.pop("candidates_json") or "[]")
        return session

    async def get_scheduling_session(self, session_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM scheduling_sessions WHERE id = ?",
                (session_id,),
            )
        if not rows:
            return None
        session = dict(rows[0])
        session["candidates"] = json.loads(session.pop("candidates_json") or "[]")
        return session

    async def set_scheduling_status(self, session_id: int, status: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE scheduling_sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), session_id),
            )
            await db.commit()
