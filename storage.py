import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterable, List, Optional, Tuple

DB_PATH = os.getenv("BOT_DB_PATH", "bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    parent_ts TEXT NOT NULL,
    trigger_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_reminded_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE(channel_id, parent_ts)
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    item_text TEXT NOT NULL,
    item_ts TEXT NOT NULL UNIQUE,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_workflows_open ON workflows(completed_at, last_reminded_at);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_workflow(
    channel_id: str,
    parent_ts: str,
    trigger_name: str,
    items: Iterable[Tuple[str, str]],
) -> int:
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO workflows (channel_id, parent_ts, trigger_name, created_at, last_reminded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, parent_ts, trigger_name, now, now),
        )
        workflow_id = cur.lastrowid
        for text, ts in items:
            conn.execute(
                "INSERT INTO items (workflow_id, item_text, item_ts) VALUES (?, ?, ?)",
                (workflow_id, text, ts),
            )
        return workflow_id


def workflow_by_thread(channel_id: str, parent_ts: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM workflows WHERE channel_id = ? AND parent_ts = ?",
            (channel_id, parent_ts),
        ).fetchone()
        return dict(row) if row else None


def workflow_by_id(workflow_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        return dict(row) if row else None


def workflow_open_items(workflow_id: int) -> List[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE workflow_id = ? AND completed_at IS NULL ORDER BY id",
            (workflow_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_item_complete(item_ts: str) -> Optional[int]:
    with connect() as conn:
        row = conn.execute("SELECT workflow_id FROM items WHERE item_ts = ?", (item_ts,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE items SET completed_at = ? WHERE item_ts = ? AND completed_at IS NULL",
            (time.time(), item_ts),
        )
        return row["workflow_id"]


def mark_item_incomplete(item_ts: str) -> Optional[int]:
    with connect() as conn:
        row = conn.execute("SELECT workflow_id FROM items WHERE item_ts = ?", (item_ts,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE items SET completed_at = NULL WHERE item_ts = ?", (item_ts,))
        return row["workflow_id"]


def force_complete_workflow(workflow_id: int) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            "UPDATE items SET completed_at = ? WHERE workflow_id = ? AND completed_at IS NULL",
            (now, workflow_id),
        )
        conn.execute("UPDATE workflows SET completed_at = ? WHERE id = ?", (now, workflow_id))


def mark_workflow_complete(workflow_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE workflows SET completed_at = ? WHERE id = ? AND completed_at IS NULL",
            (time.time(), workflow_id),
        )


def open_workflows_due_for_reminder(cutoff_ts: float) -> List[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM workflows WHERE completed_at IS NULL AND last_reminded_at < ?",
            (cutoff_ts,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_last_reminded(workflow_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE workflows SET last_reminded_at = ? WHERE id = ?",
            (time.time(), workflow_id),
        )
