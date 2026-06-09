from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from herobot.core.conversation_store import utc_now


class NotesStorage:
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
                """
            )
            await db.commit()

    async def create_todo(self, chat_id: int, user_id: int, title: str) -> dict[str, Any]:
        now = utc_now()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO todos (chat_id, user_id, title, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, user_id, title, now),
            )
            await db.commit()
            todo_id = cursor.lastrowid
        return {"id": todo_id, "title": title, "status": "open"}

    async def list_todos(self, chat_id: int, status: str = "open") -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
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
        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
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
        clauses = []
        params: list[Any] = [chat_id]
        for term in cleaned:
            pattern = f"%{term}%"
            clauses.append("(title LIKE ? OR content LIKE ?)")
            params.extend([pattern, pattern])
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                f"""
                SELECT id, title, content, updated_at FROM notes
                WHERE chat_id = ? AND ({' OR '.join(clauses)})
                ORDER BY updated_at DESC
                LIMIT 10
                """,
                params,
            )
        return [dict(row) for row in rows]
