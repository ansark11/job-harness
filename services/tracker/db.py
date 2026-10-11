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
    # 2: resumes, the answer bank (+ aliases and inbox), approved long-form answers
    """
    CREATE TABLE resumes (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        file_path  TEXT NOT NULL,     -- stored copy under data/resumes/
        text_path  TEXT NOT NULL,     -- extracted plain text, for the model
        sha256     TEXT NOT NULL UNIQUE,
        is_default INTEGER NOT NULL DEFAULT 0,
        added_at   TEXT NOT NULL
    );

    CREATE TABLE answer_bank (
        key          TEXT PRIMARY KEY,
        question     TEXT NOT NULL,   -- canonical wording
        grp          TEXT NOT NULL,
        options      TEXT,            -- JSON list of suggested answers, if standard
        jurisdiction TEXT NOT NULL DEFAULT 'any' CHECK (jurisdiction IN ('any', 'CA', 'US')),
        sensitive    INTEGER NOT NULL DEFAULT 0,   -- self-identification: never sent to the model
        always_ask   INTEGER NOT NULL DEFAULT 0,
        answer       TEXT,            -- NULL until the user answers
        custom       INTEGER NOT NULL DEFAULT 0,   -- created from an inbox question
        updated_at   TEXT
    );

    CREATE TABLE answer_aliases (
        question_norm TEXT PRIMARY KEY,
        key           TEXT NOT NULL REFERENCES answer_bank (key),
        question      TEXT NOT NULL,
        added_at      TEXT NOT NULL
    );

    CREATE TABLE inbox (
        id                  INTEGER PRIMARY KEY,
        question            TEXT NOT NULL,
        question_norm       TEXT NOT NULL,
        field_type          TEXT,
        options             TEXT,
        jurisdiction        TEXT,
        application_id      TEXT,
        proposed_key        TEXT,
        proposed_confidence REAL,
        proposed_reason     TEXT,
        status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'dismissed')),
        resolved_key        TEXT,
        created_at          TEXT NOT NULL,
        resolved_at         TEXT
    );
    CREATE UNIQUE INDEX inbox_open_question ON inbox (question_norm) WHERE status = 'open';

    CREATE TABLE approved_answers (
        id          INTEGER PRIMARY KEY,
        question    TEXT NOT NULL,
        answer      TEXT NOT NULL,
        job_id      TEXT,
        company     TEXT,
        approved_at TEXT NOT NULL
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
