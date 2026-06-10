from __future__ import annotations

import re
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

            CREATE TABLE IF NOT EXISTS note_terms (
                note_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY (note_id, term)
            );

            CREATE INDEX IF NOT EXISTS idx_note_terms_chat_term
            ON note_terms (chat_id, term);

            DROP TRIGGER IF EXISTS notes_ai;
            DROP TRIGGER IF EXISTS notes_ad;
            DROP TRIGGER IF EXISTS notes_au;
            """
        )
        await self._rebuild_note_terms(db)
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
        note_id = int(cursor.lastrowid)
        await self._set_note_terms(db, note_id, chat_id, title, content)
        await db.commit()
        return {"id": note_id, "title": title, "content": content}

    async def search_notes(self, chat_id: int, query: str) -> list[dict[str, Any]]:
        return await self.search_notes_by_terms(chat_id, [query])

    async def search_notes_by_terms(self, chat_id: int, terms: list[str]) -> list[dict[str, Any]]:
        query_terms = _query_terms(terms)
        if not query_terms:
            return []
        placeholders = ", ".join("?" for _ in query_terms)
        db = await self._db()
        rows = await db.execute_fetchall(
            f"""
            SELECT
                notes.id,
                notes.title,
                notes.content,
                notes.updated_at,
                COUNT(DISTINCT note_terms.term) AS score
            FROM note_terms
            JOIN notes ON note_terms.note_id = notes.id
            WHERE note_terms.chat_id = ?
              AND note_terms.term IN ({placeholders})
            GROUP BY notes.id
            ORDER BY score DESC, notes.updated_at DESC
            LIMIT 10
            """,
            [chat_id, *query_terms],
        )
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    async def _rebuild_note_terms(self, db: aiosqlite.Connection) -> None:
        await db.execute("DELETE FROM note_terms")
        rows = await db.execute_fetchall(
            "SELECT id, chat_id, title, content FROM notes",
        )
        for row in rows:
            await self._set_note_terms(
                db,
                int(row["id"]),
                int(row["chat_id"]),
                str(row["title"]),
                str(row["content"]),
            )

    async def _set_note_terms(
        self,
        db: aiosqlite.Connection,
        note_id: int,
        chat_id: int,
        title: str,
        content: str,
    ) -> None:
        await db.execute("DELETE FROM note_terms WHERE note_id = ?", (note_id,))
        terms = sorted(_index_terms(f"{title}\n{content}"))
        if not terms:
            return
        await db.executemany(
            "INSERT OR IGNORE INTO note_terms (note_id, chat_id, term) VALUES (?, ?, ?)",
            [(note_id, chat_id, term) for term in terms],
        )


_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+-]*")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def _index_terms(text: str) -> set[str]:
    terms: set[str] = set()
    terms.update(_ascii_terms(text))
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        for size in range(1, min(4, len(run)) + 1):
            terms.update(run[index : index + size] for index in range(0, len(run) - size + 1))
    return terms


def _query_terms(raw_terms: list[str]) -> list[str]:
    terms: set[str] = set()
    for raw in raw_terms:
        text = raw.strip()
        if not text:
            continue
        terms.update(_ascii_terms(text))
        for match in _CJK_RUN_RE.finditer(text):
            run = match.group(0)
            if len(run) == 1:
                terms.add(run)
                continue
            max_size = min(4, len(run))
            for size in range(2, max_size + 1):
                terms.update(run[index : index + size] for index in range(0, len(run) - size + 1))
    return sorted(terms)


def _ascii_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for match in _ASCII_TOKEN_RE.finditer(text):
        token = match.group(0).strip("._:/+-").lower()
        if len(token) >= 2:
            terms.add(token)
        for part in re.split(r"[^A-Za-z0-9]+", match.group(0).lower()):
            if len(part) >= 2:
                terms.add(part)
    return terms
