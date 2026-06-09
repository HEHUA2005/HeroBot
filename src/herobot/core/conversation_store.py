from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class ConversationStore:
    """Core runtime storage for conversation history only."""

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

                CREATE TABLE IF NOT EXISTS conversations (
                    chat_id INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            await db.commit()

    async def reset_conversation(self, chat_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            await db.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
            await db.commit()

    async def add_message(self, chat_id: int, role: str, content: str) -> int:
        now = utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, role, content, now),
            )
            await db.execute(
                """
                INSERT INTO conversations (chat_id, summary, updated_at)
                VALUES (?, '', ?)
                ON CONFLICT(chat_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (chat_id, now),
            )
            await db.commit()
        return int(cursor.lastrowid)

    async def recent_messages(
        self,
        chat_id: int,
        limit: int = 12,
        exclude_message_id: int | None = None,
    ) -> list[dict[str, str]]:
        excluded_clause = "AND id != ?" if exclude_message_id is not None else ""
        params: list[Any] = [chat_id]
        if exclude_message_id is not None:
            params.append(exclude_message_id)
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                f"""
                SELECT role, content FROM messages
                WHERE chat_id = ?
                  AND role IN ('user', 'assistant')
                  {excluded_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            )
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    async def get_summary(self, chat_id: int) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            row = await db.execute_fetchall(
                "SELECT summary FROM conversations WHERE chat_id = ?",
                (chat_id,),
            )
        return row[0][0] if row else ""

    async def set_summary(self, chat_id: int, summary: str) -> None:
        now = utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO conversations (chat_id, summary, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE
                SET summary = excluded.summary, updated_at = excluded.updated_at
                """,
                (chat_id, summary, now),
            )
            await db.commit()

    async def message_count(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await db.execute_fetchall(
                """
                SELECT COUNT(*) FROM messages
                WHERE chat_id = ? AND role IN ('user', 'assistant')
                """,
                (chat_id,),
            )
        return int(row[0][0]) if row else 0
