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
    # 3: applications and the answer behind every field
    """
    CREATE TABLE applications (
        id             TEXT PRIMARY KEY,
        opportunity_id TEXT NOT NULL REFERENCES opportunities (id),
        resume_id      TEXT NOT NULL REFERENCES resumes (id),
        apply_type     TEXT,
        status         TEXT NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'in_progress', 'needs_input', 'ready', 'submitted', 'failed')),
        note           TEXT,
        last_report    TEXT,        -- the agent's last report, minus field values
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        submitted_at   TEXT
    );

    CREATE TABLE application_items (
        application_id TEXT NOT NULL REFERENCES applications (id),
        key            TEXT NOT NULL,   -- the form field's normalized label
        label          TEXT NOT NULL,
        field_type     TEXT,
        options        TEXT,
        required       INTEGER NOT NULL DEFAULT 0,
        page           INTEGER,
        kind           TEXT NOT NULL CHECK (kind IN ('profile', 'long_form', 'file')),
        state          TEXT NOT NULL
                       CHECK (state IN ('filled', 'needs_review', 'needs_answer', 'approved', 'skipped')),
        value          TEXT,
        source         TEXT,            -- bank, draft, user
        bank_key       TEXT,
        inbox_id       INTEGER,
        meta           TEXT,            -- draft details: resume_points, char limit
        seq            INTEGER NOT NULL,
        updated_at     TEXT NOT NULL,
        PRIMARY KEY (application_id, key)
    );
    """,
    # 4: the company portal an external application goes through, once resolved
    """
    ALTER TABLE applications ADD COLUMN apply_url TEXT;
    """,
    # 5: a resume's text can come from a different file than the one uploaded
    #    (upload the PDF, read the Word version, which extracts more reliably)
    """
    ALTER TABLE resumes ADD COLUMN text_source TEXT;
    """,
    # 6: one role posted once per city is one opportunity with several locations
    """
    ALTER TABLE opportunities ADD COLUMN role_key TEXT;
    CREATE INDEX opportunities_role_key ON opportunities (role_key);
    CREATE TABLE opportunity_locations (
        posting_id     TEXT PRIMARY KEY,     -- the other city's LinkedIn job id
        opportunity_id TEXT NOT NULL REFERENCES opportunities (id),
        location       TEXT,
        url            TEXT,
        apply_type     TEXT,
        data           TEXT NOT NULL,        -- that posting's full posting + match JSON
        added_at       TEXT NOT NULL
    );
    ALTER TABLE applications ADD COLUMN posting_id TEXT;    -- which city's posting it applies through
    """,
    # 7: 'skipped' - hands-off Easy Apply passed on a job rather than guess an answer.
    #    SQLite can't change a CHECK constraint, so the table is rebuilt.
    """
    CREATE TABLE applications_new (
        id             TEXT PRIMARY KEY,
        opportunity_id TEXT NOT NULL REFERENCES opportunities (id),
        resume_id      TEXT NOT NULL REFERENCES resumes (id),
        apply_type     TEXT,
        status         TEXT NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'in_progress', 'needs_input', 'ready', 'submitted', 'failed',
                                         'skipped')),
        note           TEXT,
        last_report    TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        submitted_at   TEXT,
        apply_url      TEXT,
        posting_id     TEXT
    );
    INSERT INTO applications_new SELECT id, opportunity_id, resume_id, apply_type, status, note, last_report,
        created_at, updated_at, submitted_at, apply_url, posting_id FROM applications;
    DROP TABLE applications;
    ALTER TABLE applications_new RENAME TO applications;
    """,
    # 8: Easy Apply selects this resume from LinkedIn's saved list (by its file name) instead of uploading
    """
    ALTER TABLE resumes ADD COLUMN linkedin_name TEXT;
    """,
    # 9: a saved answer can list other acceptable wordings ("Man", "Male", "Cisgender male");
    #    the agent uses whichever one a form offers
    """
    ALTER TABLE answer_bank ADD COLUMN alternatives TEXT;
    """,
]


def path():
    return Path(os.environ["TRACKER_DB"]) if os.environ.get("TRACKER_DB") else DEFAULT_PATH


def connect(db_path=None):
    p = Path(db_path) if db_path else path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    # Foreign keys off while migrating: rebuilding a table (to change a CHECK) drops it
    # while other tables still reference it. Checked again once migrations are done.
    conn.execute("PRAGMA foreign_keys = OFF")
    migrate(conn)
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise sqlite3.IntegrityError("foreign key violations after migrating jobs.db")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {i}")
