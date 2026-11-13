"""Lightweight sqlite3 access layer. Deliberately no ORM: the schema is
small and the mission asks us to avoid heavy frameworks the MVP doesn't
need. All access goes through short-lived connections (sqlite3 handles
this cheaply) with WAL mode for decent concurrent read/write behavior
between the FastAPI process and itself (single process; the worker only
talks HTTP, never touches the DB file directly).
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path

from . import config

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin_users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS worker_tokens (
    token_hash TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    owner_username TEXT NOT NULL,
    state TEXT NOT NULL,
    awaiting_user_edit INTEGER NOT NULL DEFAULT 0,
    input_path TEXT,
    input_filename TEXT,
    input_duration_sec REAL,
    input_size_bytes INTEGER,
    transcript_json TEXT,
    edit_json TEXT,
    output_path TEXT,
    word_replacement_mode TEXT NOT NULL DEFAULT 'manual',
    voice_model TEXT NOT NULL DEFAULT 'openvoice',
    voice_preset TEXT NOT NULL DEFAULT 'balanced',
    claimed_by TEXT,
    claim_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    error_message TEXT,
    error_detail TEXT,
    retryable INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stage_started_at TEXT NOT NULL,
    state_history_json TEXT NOT NULL DEFAULT '[]',
    finished_at TEXT,
    engine_result_json TEXT
);

CREATE TABLE IF NOT EXISTS stage_stats (
    state TEXT PRIMARY KEY,
    sample_count INTEGER NOT NULL DEFAULT 0,
    total_duration_sec REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs (state);
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs (owner_username);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _local.conn = conn
    return conn


# Columns added after the initial schema. CREATE TABLE IF NOT EXISTS above
# never retrofits columns onto an already-existing table (e.g. an already
# running deployment's DB file) — these ALTER TABLEs make that additive and
# idempotent so upgrading in place never loses existing rows. Each one is
# `table, column, ddl_fragment`; failures other than "duplicate column" are
# re-raised since those indicate a real problem.
_MIGRATIONS = [
    ("jobs", "word_replacement_mode", "ALTER TABLE jobs ADD COLUMN word_replacement_mode TEXT NOT NULL DEFAULT 'manual'"),
    ("jobs", "voice_model", "ALTER TABLE jobs ADD COLUMN voice_model TEXT NOT NULL DEFAULT 'openvoice'"),
    ("jobs", "voice_preset", "ALTER TABLE jobs ADD COLUMN voice_preset TEXT NOT NULL DEFAULT 'balanced'"),
    ("sessions", "last_seen_at", "ALTER TABLE sessions ADD COLUMN last_seen_at TEXT"),
    ("jobs", "finished_at", "ALTER TABLE jobs ADD COLUMN finished_at TEXT"),
    ("jobs", "engine_result_json", "ALTER TABLE jobs ADD COLUMN engine_result_json TEXT"),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    for _table, _column, ddl in _MIGRATIONS:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def _backfill_finished_at(conn: sqlite3.Connection) -> None:
    """One-time (but safely idempotent/re-runnable) backfill for jobs that
    reached a terminal state before `finished_at` existed. `updated_at` is
    a reliable stand-in for those rows specifically: jobs.py never writes
    updated_at again once a job is 'done' or 'error' (heartbeat/advance_stage
    explicitly exclude terminal jobs, and purge deletes the row rather than
    updating it) — so for an already-terminal job, its last updated_at *is*
    the moment it finished. New terminal transitions set finished_at
    directly going forward; this only fills the gap for older rows."""
    conn.execute(
        "UPDATE jobs SET finished_at = updated_at "
        "WHERE state IN ('done', 'error') AND finished_at IS NULL"
    )


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = get_conn()
        conn.executescript(SCHEMA)
        _run_migrations(conn)
        _backfill_finished_at(conn)
        conn.commit()
        _initialized = True


@contextlib.contextmanager
def tx():
    """Context manager yielding a connection; commits on success, rolls
    back on exception. Use for any write that must be atomic."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
