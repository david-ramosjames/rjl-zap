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

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    send_after REAL NOT NULL,
    text TEXT NOT NULL,
    sent_at REAL,
    check_replies_first INTEGER NOT NULL DEFAULT 0,
    done_keyword TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS channel_lifecycle (
    channel_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    new_case_fired_at REAL,
    case_setup_fired_at REAL,
    doc_verification_fired_at REAL,
    calendar_sol_fired_at REAL,
    client_intake_fired_at REAL,
    attorney_intro_fired_for TEXT,
    paralegal_intro_fired_for TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflows_open ON workflows(completed_at, last_reminded_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_pending ON scheduled_messages(sent_at, send_after);
CREATE INDEX IF NOT EXISTS idx_lifecycle_pending ON channel_lifecycle(created_at);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Migration: add columns introduced after initial deploy
        for table, col, defn in [
            ("scheduled_messages", "check_replies_first", "INTEGER NOT NULL DEFAULT 0"),
            ("scheduled_messages", "done_keyword", "TEXT"),
            ("channel_lifecycle",  "attorney_intro_fired_for", "TEXT"),
            ("channel_lifecycle",  "paralegal_intro_fired_for", "TEXT"),
            ("channel_lifecycle",  "calendar_sol_fired_at", "REAL"),
            ("channel_lifecycle",  "client_intake_fired_at", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            except Exception:
                pass  # column already exists


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


def schedule_message(
    channel_id: str,
    thread_ts: str,
    send_after: float,
    text: str,
    check_replies_first: bool = False,
    done_keyword: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO scheduled_messages "
            "(channel_id, thread_ts, send_after, text, check_replies_first, done_keyword) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, thread_ts, send_after, text, int(check_replies_first), done_keyword),
        )


def due_scheduled_messages(now: float) -> List[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_messages WHERE sent_at IS NULL AND send_after <= ? ORDER BY send_after",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_scheduled_sent(message_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE scheduled_messages SET sent_at = ? WHERE id = ?",
            (time.time(), message_id),
        )


def get_config(key: str, env_fallback: str = "", default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        if row and row["value"]:
            return row["value"]
    if env_fallback:
        import os
        return os.getenv(env_fallback, default)
    return default


def set_config(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_all_config() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
        return {r["key"]: r["value"] for r in rows}


def record_channel_created(channel_id: str, created_at: float | None = None) -> None:
    """Idempotent — won't overwrite an existing row's timestamps."""
    ts = created_at if created_at is not None else time.time()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channel_lifecycle (channel_id, created_at) VALUES (?, ?)",
            (channel_id, ts),
        )


def channel_lifecycle(channel_id: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM channel_lifecycle WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        return dict(row) if row else None


def mark_new_case_fired(channel_id: str) -> bool:
    """Returns True if this call set the timestamp; False if it was already set."""
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE channel_lifecycle SET new_case_fired_at = ? "
            "WHERE channel_id = ? AND new_case_fired_at IS NULL",
            (now, channel_id),
        )
        return cur.rowcount > 0


def lifecycle_due(now: float, kind: str, delay_seconds: float) -> List[dict]:
    """Channels where `created_at + delay_seconds <= now` and the kind hasn't fired yet."""
    col = _LIFECYCLE_COLS[kind]
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM channel_lifecycle "
            f"WHERE {col} IS NULL AND (created_at + ?) <= ?",
            (delay_seconds, now),
        ).fetchall()
        return [dict(r) for r in rows]


_LIFECYCLE_COLS = {
    "case_setup": "case_setup_fired_at",
    "doc_verification": "doc_verification_fired_at",
    "calendar_sol": "calendar_sol_fired_at",
    "client_intake": "client_intake_fired_at",
}


def set_intro_fired_for(channel_id: str, role: str, user_id: str) -> bool:
    """Record that we've fired an attorney_intro or paralegal_intro for `user_id`.
    Returns True if the row needed updating (i.e. the user_id changed)."""
    col = {"attorney": "attorney_intro_fired_for",
           "paralegal": "paralegal_intro_fired_for"}[role]
    with connect() as conn:
        # Ensure a lifecycle row exists — channels created before the bot deployed
        # won't have one.
        conn.execute(
            "INSERT OR IGNORE INTO channel_lifecycle (channel_id, created_at) VALUES (?, ?)",
            (channel_id, time.time()),
        )
        cur = conn.execute(
            f"UPDATE channel_lifecycle SET {col} = ? "
            f"WHERE channel_id = ? AND (({col}) IS NULL OR ({col}) != ?)",
            (user_id, channel_id, user_id),
        )
        return cur.rowcount > 0


def mark_lifecycle_fired(channel_id: str, kind: str) -> bool:
    col = _LIFECYCLE_COLS[kind]
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE channel_lifecycle SET {col} = ? "
            f"WHERE channel_id = ? AND {col} IS NULL",
            (now, channel_id),
        )
        return cur.rowcount > 0
