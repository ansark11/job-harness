"""SQLite schema for jobs.db. Migrations run in order, tracked by PRAGMA user_version."""

import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE / "data" / "jobs.db"

MIGRATIONS = [
    # 1: opportunities and their review history, and daily runs
    """
    CREATE TABLE opportunities (
        id                TEXT PRIMARY KEY,          -- LinkedIn job id
        source            TEXT NOT NULL,
        title             TEXT,
        company           TEXT,
        location          TEXT,
        url               TEXT,
        company_logo      TEXT,
        posted_at         TEXT,
        apply_type        TEXT,
        score             INTEGER,
        fresh             INTEGER NOT NULL DEFAULT 0, -- posted within 24h of being fetched
        data              TEXT NOT NULL,              -- full posting + match JSON
        status            TEXT NOT NULL DEFAULT 'new'
                          CHECK (status IN ('new', 'kept', 'discarded', 'expired')),
        fetched_at        TEXT NOT NULL,              -- first ingest, UTC ISO
        run_id            TEXT,
        status_changed_at TEXT
    );
    CREATE INDEX opportunities_status ON opportunities (status, fetched_at);

    CREATE TABLE status_events (
        opportunity_id TEXT NOT NULL REFERENCES opportunities (id),
        at             TEXT NOT NULL,
        from_status    TEXT,
        to_status      TEXT NOT NULL,
        reason         TEXT
    );

    CREATE TABLE runs (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'failed')),
        stats       TEXT
    );
    """,
]


def path():
    return Path(os.environ["TRACKER_DB"]) if os.environ.get("TRACKER_DB") else DEFAULT_PATH


def connect(db_path=None):
    p = Path(db_path) if db_path else path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {i}")
