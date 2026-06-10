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
        self._connection: aiosqlite.Connection | None = None

    async def _db(self) -> aiosqlite.Connection:
        if self._connection is None:
            path = Path(self.db_path)
            if path.parent != Path("."):
                path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = await aiosqlite.connect(self.db_path)
            self._connection.row_factory = aiosqlite.Row
        return self._connection

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection = None

    async def init(self) -> None:
        db = await self._db()
        await db.executescript(
            """
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS conversations (
                chat_id INTEGER PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                summary_message_count INTEGER NOT NULL DEFAULT 0,
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
        await self._ensure_schema(db)
        await db.commit()

    async def _ensure_schema(self, db: aiosqlite.Connection) -> None:
        rows = await db.execute_fetchall("PRAGMA table_info(conversations)")
        columns = {row[1] for row in rows}
        if "summary_message_count" not in columns:
            await db.execute(
                """
                ALTER TABLE conversations
                ADD COLUMN summary_message_count INTEGER NOT NULL DEFAULT 0
                """
            )

    async def reset_conversation(self, chat_id: int) -> None:
        db = await self._db()
        await db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
        await db.commit()

    async def add_message(self, chat_id: int, role: str, content: str) -> int:
        now = utc_now()
        db = await self._db()
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
        db = await self._db()
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
        db = await self._db()
        row = await db.execute_fetchall(
            "SELECT summary FROM conversations WHERE chat_id = ?",
            (chat_id,),
        )
        return row[0][0] if row else ""

    async def get_summary_message_count(self, chat_id: int) -> int:
        db = await self._db()
        row = await db.execute_fetchall(
            "SELECT summary_message_count FROM conversations WHERE chat_id = ?",
            (chat_id,),
        )
        return int(row[0][0]) if row else 0

    async def set_summary(self, chat_id: int, summary: str, message_count: int) -> None:
        now = utc_now()
        db = await self._db()
        await db.execute(
            """
            INSERT INTO conversations (chat_id, summary, summary_message_count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE
            SET summary = excluded.summary,
                summary_message_count = excluded.summary_message_count,
                updated_at = excluded.updated_at
            """,
            (chat_id, summary, message_count, now),
        )
        await db.commit()

    async def message_count(self, chat_id: int) -> int:
        db = await self._db()
        row = await db.execute_fetchall(
            """
            SELECT COUNT(*) FROM messages
            WHERE chat_id = ? AND role IN ('user', 'assistant')
            """,
            (chat_id,),
        )
        return int(row[0][0]) if row else 0
