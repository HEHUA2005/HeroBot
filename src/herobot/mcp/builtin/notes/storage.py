from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from herobot.core.conversation_store import utc_now


class NotesStorage:
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

            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                title,
                content,
                content='notes',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                INSERT INTO notes_fts(rowid, title, content)
                VALUES (new.id, new.title, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, title, content)
                VALUES('delete', old.id, old.title, old.content);
            END;

            CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, title, content)
                VALUES('delete', old.id, old.title, old.content);
                INSERT INTO notes_fts(rowid, title, content)
                VALUES (new.id, new.title, new.content);
            END;
            """
        )
        await db.execute("INSERT INTO notes_fts(notes_fts) VALUES('rebuild')")
        await db.commit()

    async def create_todo(self, chat_id: int, user_id: int, title: str) -> dict[str, Any]:
        now = utc_now()
        db = await self._db()
        cursor = await db.execute(
            "INSERT INTO todos (chat_id, user_id, title, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, title, now),
        )
        await db.commit()
        todo_id = cursor.lastrowid
        return {"id": todo_id, "title": title, "status": "open"}

    async def list_todos(self, chat_id: int, status: str = "open") -> list[dict[str, Any]]:
        db = await self._db()
        if status == "all":
            rows = await db.execute_fetchall(
                """
                SELECT id, title, status, created_at, completed_at FROM todos
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT 20
                """,
                (chat_id,),
            )
        else:
            rows = await db.execute_fetchall(
                """
                SELECT id, title, status, created_at, completed_at FROM todos
                WHERE chat_id = ? AND status = ?
                ORDER BY id DESC
                LIMIT 20
                """,
                (chat_id, status),
            )
        return [dict(row) for row in rows]

    async def complete_todo(self, chat_id: int, todo_id: int) -> dict[str, Any]:
        now = utc_now()
        db = await self._db()
        cursor = await db.execute(
            """
            UPDATE todos SET status = 'done', completed_at = ?
            WHERE chat_id = ? AND id = ?
            """,
            (now, chat_id, todo_id),
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise ValueError(f"todo not found: {todo_id}")
        return {"id": todo_id, "status": "done"}

    async def create_note(
        self, chat_id: int, user_id: int, title: str, content: str
    ) -> dict[str, Any]:
        now = utc_now()
        db = await self._db()
        cursor = await db.execute(
            """
            INSERT INTO notes (chat_id, user_id, title, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, title, content, now, now),
        )
        await db.commit()
        note_id = cursor.lastrowid
        return {"id": note_id, "title": title, "content": content}

    async def search_notes(self, chat_id: int, query: str) -> list[dict[str, Any]]:
        return await self.search_notes_by_terms(chat_id, [query])

    async def search_notes_by_terms(self, chat_id: int, terms: list[str]) -> list[dict[str, Any]]:
        cleaned = [term.strip() for term in terms if term.strip()]
        if not cleaned:
            return []
        fts_query = " OR ".join(_quote_fts_term(term) for term in cleaned)
        db = await self._db()
        rows = await db.execute_fetchall(
            """
            SELECT notes.id, notes.title, notes.content, notes.updated_at
            FROM notes_fts
            JOIN notes ON notes_fts.rowid = notes.id
            WHERE notes.chat_id = ? AND notes_fts MATCH ?
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            (chat_id, fts_query),
        )
        return [dict(row) for row in rows]


def _quote_fts_term(term: str) -> str:
    escaped = term.replace('"', '""')
    return f'"{escaped}"'
