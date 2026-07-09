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
    done_keyword TEXT,
    skip_if_complete_parent_ts TEXT
);

CREATE TABLE IF NOT EXISTS deferred_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_user_id TEXT,
    fire_after REAL NOT NULL,
    fired_at REAL
);

CREATE INDEX IF NOT EXISTS idx_deferred_pending ON deferred_actions(fired_at, fire_after);

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

CREATE TABLE IF NOT EXISTS client_contact_alerts (
    case_no TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    last_interaction TEXT NOT NULL DEFAULT '',
    alerted_at REAL NOT NULL,
    PRIMARY KEY (case_no, threshold)
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
            ("scheduled_messages", "skip_if_complete_parent_ts", "TEXT"),
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
    skip_if_complete_parent_ts: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO scheduled_messages "
            "(channel_id, thread_ts, send_after, text, check_replies_first, "
            " done_keyword, skip_if_complete_parent_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel_id, thread_ts, send_after, text, int(check_replies_first),
             done_keyword, skip_if_complete_parent_ts),
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


def schedule_deferred_action(channel_id: str, kind: str,
                             target_user_id: Optional[str],
                             fire_after: float) -> None:
    """Queue a workflow-level action (e.g. attorney_intro) to fire in the
    future. Unlike schedule_message (which just posts text), the reminder
    loop dispatches deferred actions to a kind-specific handler in app.py."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO deferred_actions (channel_id, kind, target_user_id, fire_after) "
            "VALUES (?, ?, ?, ?)",
            (channel_id, kind, target_user_id, fire_after),
        )


def due_deferred_actions(now: float) -> List[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM deferred_actions WHERE fired_at IS NULL AND fire_after <= ? "
            "ORDER BY fire_after",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_deferred_action_fired(action_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE deferred_actions SET fired_at = ? WHERE id = ?",
            (time.time(), action_id),
        )


def should_send_client_contact_alert(case_no: str, threshold: int,
                                     last_interaction: str) -> bool:
    """True iff no (case, threshold) alert has been recorded for the given
    last_interaction value. Re-arms whenever last_interaction changes — i.e.
    if someone logs a contact and the case lapses again, the alert can fire
    a second time with the new Last Interaction date."""
    with connect() as conn:
        row = conn.execute(
            "SELECT last_interaction FROM client_contact_alerts "
            "WHERE case_no = ? AND threshold = ?",
            (case_no, threshold),
        ).fetchone()
    return row is None or row["last_interaction"] != last_interaction


def record_client_contact_alert(case_no: str, threshold: int,
                                last_interaction: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO client_contact_alerts "
            "(case_no, threshold, last_interaction, alerted_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(case_no, threshold) DO UPDATE SET "
            "  last_interaction = excluded.last_interaction, "
            "  alerted_at = excluded.alerted_at",
            (case_no, threshold, last_interaction, time.time()),
        )


def alerted_case_numbers() -> set[str]:
    """Every distinct case_no the bot has ever recorded a contact alert for.
    Used by the sweep to detect cases that have dropped off the Client
    Contact Status sheet (i.e. cases the firm has marked inactive)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT case_no FROM client_contact_alerts"
        ).fetchall()
        return {r["case_no"] for r in rows}


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


def lifecycle_due(now: float, kind: str, delay_seconds: float,
                  max_age_seconds: float | None = None) -> List[dict]:
    """Channels where `created_at + delay_seconds <= now` and the kind hasn't
    fired yet. When `max_age_seconds` is given, also require the channel to
    have been created within that window (`created_at >= now - max_age_seconds`)
    so automatic alerts never fire for old / pre-existing channels."""
    col = _LIFECYCLE_COLS[kind]
    clauses = [f"{col} IS NULL", "(created_at + ?) <= ?"]
    params: list = [delay_seconds, now]
    if max_age_seconds is not None:
        clauses.append("created_at >= ?")
        params.append(now - max_age_seconds)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_lifecycle WHERE " + " AND ".join(clauses),
            params,
        ).fetchall()
        return [dict(r) for r in rows]


_LIFECYCLE_COLS = {
    "new_case": "new_case_fired_at",
    "case_setup": "case_setup_fired_at",
    "doc_verification": "doc_verification_fired_at",
    "calendar_sol": "calendar_sol_fired_at",
    "client_intake": "client_intake_fired_at",
}


def set_intro_fired_for(channel_id: str, role: str, user_id: str) -> bool:
    """Record that we've fired an attorney_intro or paralegal_intro for this
    channel. Returns True only on the *first* fire — subsequent calls (even
    with a different user_id) return False, so the intro never re-fires when
    the channel description is changed later."""
    col = {"attorney": "attorney_intro_fired_for",
           "paralegal": "paralegal_intro_fired_for"}[role]
    now = time.time()
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM channel_lifecycle WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if not exists:
            # We're learning about this channel via a topic edit, NOT via
            # channel_created — so we don't know its real creation time.
            # Create the row with every time-based trigger pre-marked as
            # fired, so editing an existing channel's topic never kicks off
            # a brand-new-case schedule (new_case / case_setup / doc_verification
            # / calendar_sol / client_intake).
            conn.execute(
                "INSERT INTO channel_lifecycle "
                "(channel_id, created_at, new_case_fired_at, case_setup_fired_at, "
                " doc_verification_fired_at, calendar_sol_fired_at, client_intake_fired_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (channel_id, now, now, now, now, now, now),
            )
        cur = conn.execute(
            f"UPDATE channel_lifecycle SET {col} = ? "
            f"WHERE channel_id = ? AND {col} IS NULL",
            (user_id, channel_id),
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
