"""On-demand calibration of the pre-aggregated summary against the ledger.

The survival page reads only ``summary_counts``; every import keeps that table
(and the compact ``battery_summary``) in sync incrementally inside its own
transaction.  Calibration is the recovery path for when those counts ever
diverge from the auditable ledger.

Concurrency model
-----------------
The run is split into two phases so a calibration never blocks follow-up
imports while it scans:

1. **Snapshot phase (read-only, no write lock).** Read a watermark from
   ``ledger_changes``, then scan ``battery_ledger`` exactly once to build the
   expected group counts and a per-battery snapshot, and read the current
   aggregates.  Imports commit freely during this phase.
2. **Apply phase (one ``BEGIN IMMEDIATE`` transaction).** Re-read only the
   batteries whose ``ledger_changes`` seq is above the watermark (primary-key
   lookups, never a second full scan), replace their snapshot contribution with
   their latest ledger state, then rewrite only the mismatched groups and
   repair the affected per-battery summaries.  Imports queue behind this short
   write transaction and commit as soon as it ends.

The commit is atomic: a survival query sees either the complete pre-calibration
counts or the complete post-calibration counts, never a partial mix.  Any error
in the apply phase rolls the whole calibration back, while imports that
committed earlier stay committed.  Calibration never writes
``battery_summary_usage``, so it does not consume a battery's six-record quota.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Sequence

from db import SummaryRecord, ledger_records_for, summary_from_values


class CalibrationError(Exception):
    """Calibration could not be completed; its transaction was rolled back."""


@dataclass(frozen=True)
class GroupFix:
    """One corrected aggregate group and its before/after subject counts."""

    activation_date: str
    entry_age: int
    end_age: int
    event: int
    before_count: int  # zero means the summary had no row for this group
    after_count: int  # zero means the ledger has no subject in this group

    @property
    def key(self) -> tuple[str, int, int, int]:
        return (self.activation_date, self.entry_age, self.end_age, self.event)


@dataclass(frozen=True)
class BatterySummaryFix:
    battery_no: str
    action: str  # insert / update / delete


@dataclass
class CalibrationResult:
    run_id: int = 0
    ledger_batteries: int = 0
    summary_batteries: int = 0
    mismatched_groups: int = 0
    group_fixes: list[GroupFix] = field(default_factory=list)
    battery_summary_fixes: list[BatterySummaryFix] = field(default_factory=list)
    changes_during_scan: int = 0

    @property
    def corrected_groups(self) -> int:
        return len(self.group_fixes)

    @property
    def corrected_battery_summaries(self) -> int:
        return len(self.battery_summary_fixes)

    @property
    def changed_anything(self) -> bool:
        return bool(self.group_fixes or self.battery_summary_fixes)


def _record_key(record: SummaryRecord) -> tuple[str, int, int, int]:
    return (record.activation_date, record.entry_age, record.end_age, record.event)


def _watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger_changes").fetchone()
    return int(row[0])


def _count_table(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _scan_ledger(conn: sqlite3.Connection) -> tuple[Counter, dict[str, SummaryRecord]]:
    """The single full ledger scan of the whole calibration run."""

    counts: Counter = Counter()
    per_battery: dict[str, SummaryRecord] = {}
    for row in conn.execute(
        "SELECT battery_no, activation_date, followup_start, followup_end, outcome "
        "FROM battery_ledger"
    ):
        record = summary_from_values(tuple(row))
        per_battery[row[0]] = record
        counts[_record_key(record)] += 1
    return counts, per_battery


def _summary_group_counts(conn: sqlite3.Connection) -> Counter:
    counts: Counter = Counter()
    for row in conn.execute(
        "SELECT activation_date, entry_age, end_age, event, subject_count "
        "FROM summary_counts"
    ):
        counts[(row[0], int(row[1]), int(row[2]), int(row[3]))] = int(row[4])
    return counts


def _battery_snapshot(conn: sqlite3.Connection) -> dict[str, SummaryRecord]:
    snapshot: dict[str, SummaryRecord] = {}
    for row in conn.execute(
        "SELECT battery_no, activation_date, entry_age, end_age, event "
        "FROM battery_summary"
    ):
        snapshot[row[0]] = SummaryRecord(
            activation_date=row[1],
            entry_age=int(row[2]),
            end_age=int(row[3]),
            event=int(row[4]),
        )
    return snapshot


def _changed_batteries(
    conn: sqlite3.Connection, watermark: int
) -> tuple[list[str], int]:
    rows = conn.execute(
        "SELECT DISTINCT battery_no FROM ledger_changes WHERE seq > ?",
        (watermark,),
    ).fetchall()
    change_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM ledger_changes WHERE seq > ?", (watermark,)
        ).fetchone()[0]
    )
    return [row[0] for row in rows], change_count


def _write_group_counts(
    conn: sqlite3.Connection, fixes: Sequence[GroupFix]
) -> None:
    for fix in fixes:
        key = fix.key
        if fix.after_count == 0:
            conn.execute(
                "DELETE FROM summary_counts WHERE activation_date = ? AND entry_age = ? "
                "AND end_age = ? AND event = ?",
                key,
            )
        else:
            conn.execute(
                "INSERT INTO summary_counts "
                "(activation_date, entry_age, end_age, event, subject_count) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (activation_date, entry_age, end_age, event) DO UPDATE "
                "SET subject_count = excluded.subject_count",
                (*key, fix.after_count),
            )


def _reconcile_battery_summaries(
    conn: sqlite3.Connection,
    candidates: set[str],
    affected: set[str],
    battery_rows: dict[str, SummaryRecord],
    snapshot_ledger: dict[str, SummaryRecord],
    live_ledger: dict[str, tuple[str, str, str, str, str]],
    now: str,
) -> list[BatterySummaryFix]:
    def current_record(battery_no: str) -> SummaryRecord | None:
        if battery_no in affected:
            values = live_ledger.get(battery_no)
            return summary_from_values(values) if values is not None else None
        return snapshot_ledger.get(battery_no)

    fixes: list[BatterySummaryFix] = []
    for battery_no in sorted(candidates):
        old = battery_rows.get(battery_no)
        new = current_record(battery_no)
        if new is None:
            if old is not None:
                conn.execute(
                    "DELETE FROM battery_summary WHERE battery_no = ?", (battery_no,)
                )
                fixes.append(BatterySummaryFix(battery_no, "delete"))
            continue

        if old is None:
            conn.execute(
                "INSERT INTO battery_summary "
                "(battery_no, activation_date, entry_age, end_age, event, revision, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    battery_no,
                    new.activation_date,
                    new.entry_age,
                    new.end_age,
                    new.event,
                    now,
                    now,
                ),
            )
            fixes.append(BatterySummaryFix(battery_no, "insert"))
        elif _record_key(old) != _record_key(new):
            conn.execute(
                "UPDATE battery_summary SET activation_date = ?, entry_age = ?, "
                "end_age = ?, event = ?, revision = revision + 1, updated_at = ? "
                "WHERE battery_no = ?",
                (
                    new.activation_date,
                    new.entry_age,
                    new.end_age,
                    new.event,
                    now,
                    battery_no,
                ),
            )
            fixes.append(BatterySummaryFix(battery_no, "update"))
    return fixes


def _verify(conn: sqlite3.Connection, expected: Counter) -> None:
    """Defensive post-write check; any discrepancy aborts and rolls back."""

    actual = _summary_group_counts(conn)
    if dict(actual) != dict(expected):
        raise CalibrationError("校准后的复核与台账口径不一致，已撤销本次修正。")


def calibrate_summary(
    conn: sqlite3.Connection,
    *,
    on_snapshot: Callable[[], None] | None = None,
    on_after_snapshot: Callable[[], None] | None = None,
    on_before_apply: Callable[[], None] | None = None,
    on_before_commit: Callable[[], None] | None = None,
) -> CalibrationResult:
    """Calibrate ``summary_counts``/``battery_summary`` against the ledger.

    ``on_snapshot`` runs right after the single ledger scan, while the read
    transaction is still open and no write lock is held.
    ``on_after_snapshot`` runs after the read transaction has been committed
    and before the write transaction starts.
    ``on_before_apply`` runs immediately before the write transaction acquires
    its lock, so a signalled import is already committed and is guaranteed to
    be part of the ledger state the calibration takes effect on.
    ``on_before_commit`` runs inside the write transaction just before commit
    to simulate a failure that must roll the calibration back.
    """

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = CalibrationResult()

    # ---- Phase 1: read-only snapshot, imports remain fully available. ----
    conn.execute("BEGIN")
    try:
        watermark = _watermark(conn)
        expected, snapshot_ledger = _scan_ledger(conn)  # the only full ledger scan
        actual = _summary_group_counts(conn)
        battery_rows = _battery_snapshot(conn)
        result.ledger_batteries = _count_table(conn, "battery_ledger")
        result.summary_batteries = _count_table(conn, "battery_summary")
        if on_snapshot is not None:
            on_snapshot()
    finally:
        # Ends the read transaction; nothing was written in this phase.
        conn.execute("COMMIT")

    if on_after_snapshot is not None:
        on_after_snapshot()
    if on_before_apply is not None:
        on_before_apply()

    # ---- Phase 2: one short write transaction; imports queue behind it. ----
    conn.execute("BEGIN IMMEDIATE")
    committed = False
    try:
        affected_nos, change_count = _changed_batteries(conn, watermark)
        affected = set(affected_nos)
        result.changes_during_scan = change_count
        live_ledger = ledger_records_for(conn, affected_nos)

        # Imports that committed during the scan already maintained
        # ``battery_summary`` themselves, so refresh their rows before
        # reconciling instead of trusting the phase-1 snapshot.
        if affected_nos:
            placeholders = ",".join("?" for _ in affected_nos)
            for row in conn.execute(
                "SELECT battery_no, activation_date, entry_age, end_age, event "
                f"FROM battery_summary WHERE battery_no IN ({placeholders})",
                affected_nos,
            ):
                battery_rows[row[0]] = SummaryRecord(
                    activation_date=row[1],
                    entry_age=int(row[2]),
                    end_age=int(row[3]),
                    event=int(row[4]),
                )

        # Replace the snapshot contribution of every battery touched after the
        # watermark with its latest ledger state.
        for battery_no in affected_nos:
            old_record = snapshot_ledger.pop(battery_no, None)
            if old_record is not None:
                key = _record_key(old_record)
                expected[key] -= 1
                if expected[key] <= 0:
                    del expected[key]
            values = live_ledger.get(battery_no)
            if values is not None:
                expected[_record_key(summary_from_values(values))] += 1

        group_fixes: list[GroupFix] = []
        for key in sorted(set(expected) | set(actual)):
            after = int(expected.get(key, 0))
            before = int(actual.get(key, 0))
            if before != after:
                group_fixes.append(
                    GroupFix(
                        activation_date=key[0],
                        entry_age=key[1],
                        end_age=key[2],
                        event=key[3],
                        before_count=before,
                        after_count=after,
                    )
                )
        result.mismatched_groups = len(group_fixes)
        _write_group_counts(conn, group_fixes)

        # Compact per-battery summaries: repair batteries touched during the
        # scan, plus any row whose presence or values disagree with the ledger
        # snapshot (all resolved from data already in memory, no second scan).
        candidates = set(affected)
        candidates.update(battery_rows.keys() - snapshot_ledger.keys())  # orphans
        candidates.update(snapshot_ledger.keys() - battery_rows.keys())  # missing
        for battery_no in snapshot_ledger.keys() & battery_rows.keys():
            if _record_key(battery_rows[battery_no]) != _record_key(
                snapshot_ledger[battery_no]
            ):
                candidates.add(battery_no)
        battery_fixes = _reconcile_battery_summaries(
            conn,
            candidates,
            affected,
            battery_rows,
            snapshot_ledger,
            live_ledger,
            now,
        )

        _verify(conn, expected)
        if on_before_commit is not None:
            on_before_commit()

        run_id = int(
            conn.execute(
                "INSERT INTO calibration_runs "
                "(ran_at, mismatched_groups, corrected_groups, "
                "corrected_battery_summaries, changes_during_scan) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    now,
                    result.mismatched_groups,
                    len(group_fixes),
                    len(battery_fixes),
                    change_count,
                ),
            ).lastrowid
        )
        conn.executemany(
            "INSERT INTO calibration_group_changes "
            "(run_id, activation_date, entry_age, end_age, event, "
            "before_count, after_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    fix.activation_date,
                    fix.entry_age,
                    fix.end_age,
                    fix.event,
                    fix.before_count,
                    fix.after_count,
                )
                for fix in group_fixes
            ],
        )
        conn.executemany(
            "INSERT INTO calibration_battery_changes (run_id, battery_no, action) "
            "VALUES (?, ?, ?)",
            [(run_id, fix.battery_no, fix.action) for fix in battery_fixes],
        )
        conn.execute("COMMIT")
        committed = True
    finally:
        if not committed:
            conn.execute("ROLLBACK")

    result.run_id = run_id
    result.group_fixes = group_fixes
    result.battery_summary_fixes = battery_fixes
    return result


def calibration_history(conn: sqlite3.Connection, limit: int = 10):
    return list(
        conn.execute(
            "SELECT id, ran_at, mismatched_groups, corrected_groups, "
            "corrected_battery_summaries, changes_during_scan "
            "FROM calibration_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    )
