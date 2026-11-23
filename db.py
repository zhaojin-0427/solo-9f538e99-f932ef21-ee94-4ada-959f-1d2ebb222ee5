"""SQLite storage for the battery follow-up ledger and survival summaries.

Three pieces of state are written together by every import:

``battery_ledger``
    The auditable ledger: one current row per battery.
``battery_summary``
    The compact per-battery summary used to maintain the aggregates.  It is a
    single row per battery, updated in place on a correction and deleted on a
    delete.
``battery_summary_usage``
    Lifetime count of summary records written for a battery (insert, correction
    and delete all count).  A battery may produce at most
    ``MAX_SUMMARY_RECORDS_PER_BATTERY`` (6) of them; further changes are
    rejected with an actionable error.  This counter survives deletion so a
    delete/re-insert cycle still counts against the same budget.
``summary_counts``
    Pre-aggregated counts keyed by activation date and day offsets.  The HTML
    analysis page reads only this table, never the ledger.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

MAX_SUMMARY_RECORDS_PER_BATTERY = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS battery_ledger (
    battery_no      TEXT PRIMARY KEY,
    activation_date TEXT NOT NULL,
    followup_start  TEXT NOT NULL,
    followup_end    TEXT NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN ('failure', 'censored')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- One compact summary record per battery.  Corrections overwrite this row and
-- bump ``revision`` instead of appending, which keeps the per-battery summary
-- record count at one (well under the six-record bound).
CREATE TABLE IF NOT EXISTS battery_summary (
    battery_no      TEXT PRIMARY KEY,
    activation_date TEXT NOT NULL,
    entry_age       INTEGER NOT NULL CHECK (entry_age >= 0),
    end_age         INTEGER NOT NULL,
    event           INTEGER NOT NULL CHECK (event IN (0, 1)),
    revision        INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK (end_age >= entry_age)
);

-- Lifetime summary-record usage per battery.  Insert/correct/delete each
-- count once; the counter is never reset, including after a delete.
CREATE TABLE IF NOT EXISTS battery_summary_usage (
    battery_no      TEXT PRIMARY KEY,
    summary_records INTEGER NOT NULL CHECK (summary_records >= 0)
);

-- Aggregate counts read by the survival query.  The activation date stays in
-- the key because a subject's age on a calendar cutoff depends on its own
-- activation date.  ``entry_age``/``end_age`` are whole days from activation.
CREATE TABLE IF NOT EXISTS summary_counts (
    activation_date TEXT NOT NULL,
    entry_age       INTEGER NOT NULL,
    end_age         INTEGER NOT NULL,
    event           INTEGER NOT NULL CHECK (event IN (0, 1)),
    subject_count   INTEGER NOT NULL CHECK (subject_count > 0),
    PRIMARY KEY (activation_date, entry_age, end_age, event)
);

CREATE TABLE IF NOT EXISTS import_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    imported_at     TEXT NOT NULL,
    total_rows      INTEGER NOT NULL,
    inserted        INTEGER NOT NULL,
    updated         INTEGER NOT NULL,
    deleted         INTEGER NOT NULL DEFAULT 0,
    unchanged       INTEGER NOT NULL,
    summary_records INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class SummaryRecord:
    activation_date: str
    entry_age: int
    end_age: int
    event: int


def summary_from_values(values: tuple[str, str, str, str, str]) -> SummaryRecord:
    """Map ledger values to a summary record (day offsets from activation)."""

    _, activation_text, start_text, end_text, outcome = values
    activation = date.fromisoformat(activation_text)
    return SummaryRecord(
        activation_date=activation_text,
        entry_age=(date.fromisoformat(start_text) - activation).days,
        end_age=(date.fromisoformat(end_text) - activation).days,
        event=1 if outcome == "failure" else 0,
    )


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        parent = Path(self.path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            # Multiple gunicorn workers may initialize at the same time; retry
            # the idempotent schema setup instead of failing worker startup.
            for attempt in range(6):
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                    conn.executescript(SCHEMA)
                    _migrate(conn)
                    return
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        raise
                    if attempt == 5:
                        raise
                    time.sleep(0.2 * (attempt + 1))
        finally:
            conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after an initial deployment, if needed."""

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(import_batches)")
    }
    if "deleted" not in columns:
        conn.execute(
            "ALTER TABLE import_batches ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
        )


def _adjust_count(conn: sqlite3.Connection, record: SummaryRecord, delta: int) -> None:
    key = (record.activation_date, record.entry_age, record.end_age, record.event)
    if delta > 0:
        conn.execute(
            "INSERT INTO summary_counts "
            "(activation_date, entry_age, end_age, event, subject_count) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (activation_date, entry_age, end_age, event) DO UPDATE "
            "SET subject_count = subject_count + excluded.subject_count",
            (*key, delta),
        )
    elif delta < 0:
        # Drop a bucket when the decrement would empty it (the positive-count
        # CHECK forbids storing zero), then decrement the remaining buckets.
        conn.execute(
            "DELETE FROM summary_counts WHERE activation_date = ? AND entry_age = ? "
            "AND end_age = ? AND event = ? AND subject_count <= ?",
            (*key, -delta),
        )
        conn.execute(
            "UPDATE summary_counts SET subject_count = subject_count + ? "
            "WHERE activation_date = ? AND entry_age = ? AND end_age = ? AND event = ?",
            (delta, *key),
        )


def apply_summary_changes(
    conn: sqlite3.Connection,
    changes: list[tuple[SummaryRecord | None, SummaryRecord | None]],
) -> None:
    """Incrementally apply (old, new) summary transitions.

    Runs inside the caller's import transaction, so any later failure rolls the
    aggregates back together with the ledger and the per-battery summaries.
    """

    # Release old buckets before acquiring new ones.  A same-key correction may
    # briefly empty a bucket, but it is re-inserted later in this transaction.
    for old, _ in changes:
        if old is not None:
            _adjust_count(conn, old, -1)
    for _, new in changes:
        if new is not None:
            _adjust_count(conn, new, 1)


def usage_for(
    conn: sqlite3.Connection, battery_nos: Sequence[str]
) -> dict[str, int]:
    """Lifetime summary-record counts for the given batteries."""

    usage: dict[str, int] = {}
    nos = list(battery_nos)
    for start in range(0, len(nos), 400):
        chunk = nos[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            "SELECT battery_no, summary_records FROM battery_summary_usage "
            f"WHERE battery_no IN ({placeholders})",
            chunk,
        ):
            usage[row[0]] = int(row[1])
    return usage


def record_usage(conn: sqlite3.Connection, battery_no: str) -> int:
    """Consume one summary-record slot and return the new usage count."""

    conn.execute(
        "INSERT INTO battery_summary_usage (battery_no, summary_records) "
        "VALUES (?, 1) "
        "ON CONFLICT (battery_no) DO UPDATE "
        "SET summary_records = battery_summary_usage.summary_records + 1",
        (battery_no,),
    )
    return int(
        conn.execute(
            "SELECT summary_records FROM battery_summary_usage WHERE battery_no = ?",
            (battery_no,),
        ).fetchone()[0]
    )


def rebuild_summary(conn: sqlite3.Connection) -> None:
    """Recompute ``battery_summary`` and ``summary_counts`` from the ledger.

    Maintenance/recovery helper; normal imports update both incrementally.
    """

    from collections import Counter

    conn.execute("DELETE FROM battery_summary")
    conn.execute("DELETE FROM summary_counts")

    counts: Counter[tuple[str, int, int, int]] = Counter()
    now = conn.execute("SELECT datetime('now')").fetchone()[0]
    for row in conn.execute(
        "SELECT battery_no, activation_date, followup_start, followup_end, outcome, "
        "created_at FROM battery_ledger ORDER BY battery_no"
    ):
        record = summary_from_values(tuple(row[:5]))
        conn.execute(
            "INSERT INTO battery_summary "
            "(battery_no, activation_date, entry_age, end_age, event, revision, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (
                row[0],
                record.activation_date,
                record.entry_age,
                record.end_age,
                record.event,
                row[5] or now,
                now,
            ),
        )
        counts[
            (
                record.activation_date,
                record.entry_age,
                record.end_age,
                record.event,
            )
        ] += 1

    conn.executemany(
        "INSERT INTO summary_counts "
        "(activation_date, entry_age, end_age, event, subject_count) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (activation_date, entry_age, end_age, event, count)
            for (activation_date, entry_age, end_age, event), count in sorted(
                counts.items()
            )
        ],
    )


def ledger_rows(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT battery_no, activation_date, followup_start, followup_end, outcome "
        "FROM battery_ledger ORDER BY battery_no"
    )
