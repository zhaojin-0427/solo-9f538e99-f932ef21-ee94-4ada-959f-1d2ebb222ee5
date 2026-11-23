"""Tests for on-demand summary calibration against the ledger."""

from __future__ import annotations

import io
import threading

import pytest

from app import create_app
from calibration import CalibrationError, calibrate_summary
from db import Database
from importer import apply_import, parse_csv

CSV_HEADER = (
    "battery_no,activation_date,followup_start,followup_end,outcome,action\n"
)


def csv_bytes(*lines: str) -> bytes:
    return (CSV_HEADER + "\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "battery.db"


@pytest.fixture()
def database(db_path):
    database = Database(db_path)
    database.initialize()
    return database


@pytest.fixture()
def client(database, db_path):
    app = create_app({"TESTING": True, "DATABASE": str(db_path), "SECRET_KEY": "t"})
    with app.test_client() as client:
        yield client


def import_csv(database, *lines: str, filename: str = "batch.csv"):
    records = parse_csv(csv_bytes(*lines))
    conn = database.connect()
    try:
        return apply_import(conn, filename, records)
    finally:
        conn.close()


def summary_rows(database):
    conn = database.connect()
    try:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT activation_date, entry_age, end_age, event, subject_count "
                "FROM summary_counts ORDER BY 1, 2, 3, 4"
            )
        ]
    finally:
        conn.close()


def usage(database, battery_no):
    conn = database.connect()
    try:
        row = conn.execute(
            "SELECT summary_records FROM battery_summary_usage WHERE battery_no = ?",
            (battery_no,),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def seed_three_batteries(database):
    import_csv(
        database,
        "B1,2026-01-01,2026-01-02,2026-02-01,failure",
        "B2,2026-01-01,2026-01-02,2026-02-01,failure",
        "B3,2026-01-01,2026-01-02,2026-03-01,censored",
    )


def drift_summary(database):
    """Tamper with summary_counts to mimic three kinds of divergence."""

    conn = database.connect()  # autocommit connection
    try:
        # 1. Existing bucket inflated: real count is 2, summary says 5.
        conn.execute(
            "UPDATE summary_counts SET subject_count = 5 "
            "WHERE activation_date = '2026-01-01' AND entry_age = 1 "
            "AND end_age = 31 AND event = 1"
        )
        # 2. Real bucket missing on the summary side (before count must read 0).
        conn.execute(
            "DELETE FROM summary_counts "
            "WHERE activation_date = '2026-01-01' AND entry_age = 1 "
            "AND end_age = 59 AND event = 0"
        )
        # 3. Phantom bucket with no ledger backing (after count must read 0).
        conn.execute(
            "INSERT INTO summary_counts "
            "(activation_date, entry_age, end_age, event, subject_count) "
            "VALUES ('2025-12-31', 0, 9, 1, 4)"
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Repair semantics
# --------------------------------------------------------------------------


def test_calibration_repairs_only_mismatched_groups_with_zero_fill(database):
    seed_three_batteries(database)
    drift_summary(database)

    conn = database.connect()
    try:
        result = calibrate_summary(conn)
    finally:
        conn.close()

    assert result.mismatched_groups == 3
    assert result.corrected_groups == 3
    fixes = {(fix.activation_date, fix.entry_age, fix.end_age, fix.event): fix
             for fix in result.group_fixes}

    inflated = fixes[("2026-01-01", 1, 31, 1)]
    assert (inflated.before_count, inflated.after_count) == (5, 2)

    missing = fixes[("2026-01-01", 1, 59, 0)]
    assert (missing.before_count, missing.after_count) == (0, 1)

    phantom = fixes[("2025-12-31", 0, 9, 1)]
    assert (phantom.before_count, phantom.after_count) == (4, 0)

    # The summary now equals a fresh aggregation of the ledger.
    assert summary_rows(database) == [
        ("2026-01-01", 1, 31, 1, 2),
        ("2026-01-01", 1, 59, 0, 1),
    ]


def test_calibration_is_idempotent_and_audited(database):
    seed_three_batteries(database)
    drift_summary(database)

    conn = database.connect()
    try:
        first = calibrate_summary(conn)
        second = calibrate_summary(conn)
        run_rows = list(
            conn.execute("SELECT id, mismatched_groups, corrected_groups "
                         "FROM calibration_runs ORDER BY id")
        )
        detail_count = int(
            conn.execute("SELECT COUNT(*) FROM calibration_group_changes").fetchone()[0]
        )
    finally:
        conn.close()

    assert first.corrected_groups == 3
    assert second.corrected_groups == 0
    assert second.mismatched_groups == 0
    assert [tuple(row) for row in run_rows] == [
        (first.run_id, 3, 3),
        (second.run_id, 0, 0),
    ]
    # Only the first run carries before/after detail rows.
    assert detail_count == 3


def test_calibration_does_not_consume_summary_record_quota(database):
    seed_three_batteries(database)
    drift_summary(database)
    before = {no: usage(database, no) for no in ("B1", "B2", "B3")}

    conn = database.connect()
    try:
        calibrate_summary(conn)
    finally:
        conn.close()

    after = {no: usage(database, no) for no in ("B1", "B2", "B3")}
    assert before == after == {"B1": 1, "B2": 1, "B3": 1}


def test_calibration_repairs_per_battery_summary_rows(database):
    seed_three_batteries(database)
    conn = database.connect()
    try:
        # Drop a compact summary row (missing side) and add an orphan.
        conn.execute("DELETE FROM battery_summary WHERE battery_no = 'B3'")
        conn.execute(
            "INSERT INTO battery_summary "
            "(battery_no, activation_date, entry_age, end_age, event, revision, "
            "created_at, updated_at) VALUES "
            "('GHOST', '2026-01-01', 1, 2, 1, 1, 'x', 'x')"
        )
    finally:
        conn.close()

    conn = database.connect()
    try:
        result = calibrate_summary(conn)
        actions = {fix.battery_no: fix.action for fix in result.battery_summary_fixes}
        b3 = conn.execute(
            "SELECT entry_age, end_age, event, revision FROM battery_summary "
            "WHERE battery_no = 'B3'"
        ).fetchone()
        ghost = conn.execute(
            "SELECT 1 FROM battery_summary WHERE battery_no = 'GHOST'"
        ).fetchone()
    finally:
        conn.close()

    assert actions.get("B3") == "insert"
    assert actions.get("GHOST") == "delete"
    assert tuple(b3) == (1, 59, 0, 1)
    assert ghost is None


def test_calibration_scans_the_ledger_exactly_once(database):
    seed_three_batteries(database)
    drift_summary(database)
    conn = database.connect()
    full_scans = 0

    def tracer(sql):
        nonlocal full_scans
        if (
            "FROM battery_ledger" in sql
            and "WHERE" not in sql
            and "COUNT" not in sql
        ):
            full_scans += 1

    conn.set_trace_callback(tracer)
    try:
        calibrate_summary(conn)
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert full_scans == 1


# --------------------------------------------------------------------------
# Concurrency: imports during the scan, rollback on failure, threads
# --------------------------------------------------------------------------


def test_import_committed_during_scan_is_included_in_calibration(database):
    seed_three_batteries(database)
    drift_summary(database)
    start = threading.Event()
    committed = threading.Event()

    def import_during_scan():
        assert start.wait(timeout=5)
        import_csv(
            database,
            "B4,2026-01-01,2026-01-02,2026-02-01,failure",
        )
        committed.set()

    worker = threading.Thread(target=import_during_scan)
    worker.start()

    conn = database.connect()
    try:
        result = calibrate_summary(
            conn,
            on_snapshot=lambda: start.set(),
            on_before_apply=lambda: committed.wait(timeout=5),
        )
    finally:
        conn.close()
    worker.join(timeout=5)

    # The late insert landed after the watermark and was folded into the fix.
    assert result.changes_during_scan >= 1
    rows = summary_rows(database)
    assert ("2026-01-01", 1, 31, 1, 3) in rows  # B1, B2 plus the late B4
    assert ("2026-01-01", 1, 59, 0, 1) in rows
    assert all(row[0] != "2025-12-31" for row in rows)


def test_correction_during_scan_uses_latest_ledger_state(database):
    import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,censored")
    start = threading.Event()
    committed = threading.Event()

    def correct_during_scan():
        assert start.wait(timeout=5)
        import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-15,failure")
        committed.set()

    worker = threading.Thread(target=correct_during_scan)
    worker.start()
    conn = database.connect()
    try:
        result = calibrate_summary(
            conn,
            on_snapshot=lambda: start.set(),
            on_before_apply=lambda: committed.wait(timeout=5),
        )
    finally:
        conn.close()
    worker.join(timeout=5)

    assert result.changes_during_scan >= 1
    assert summary_rows(database) == [("2026-01-01", 1, 45, 1, 1)]


def test_failed_calibration_rolls_back_but_keeps_prior_imports(database):
    seed_three_batteries(database)
    drift_summary(database)
    drifted_rows = summary_rows(database)

    conn = database.connect()
    try:
        with pytest.raises(RuntimeError):
            calibrate_summary(conn, on_before_commit=lambda: (_ for _ in ()).throw(
                RuntimeError("boom")))
        runs = conn.execute("SELECT COUNT(*) FROM calibration_runs").fetchone()[0]
        group_audit = conn.execute(
            "SELECT COUNT(*) FROM calibration_group_changes"
        ).fetchone()[0]
    finally:
        conn.close()

    # Every calibration write was undone; the earlier imports remain intact.
    assert summary_rows(database) == drifted_rows
    assert runs == 0
    assert group_audit == 0
    conn = database.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM battery_ledger").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1
    finally:
        conn.close()


def test_post_write_verification_failure_rolls_back(database, monkeypatch):
    seed_three_batteries(database)
    drift_summary(database)
    drifted_rows = summary_rows(database)

    import calibration as calibration_module

    def broken_write(conn, fixes):
        # Write nothing, so the post-write verification must catch the drift.
        return None

    monkeypatch.setattr(calibration_module, "_write_group_counts", broken_write)

    conn = database.connect()
    try:
        with pytest.raises(CalibrationError):
            calibrate_summary(conn)
    finally:
        conn.close()

    assert summary_rows(database) == drifted_rows


def test_concurrent_import_and_calibration_end_in_consistent_state(database):
    seed_three_batteries(database)
    drift_summary(database)
    start = threading.Event()
    committed = threading.Event()
    errors: list[Exception] = []

    def importer():
        try:
            assert start.wait(timeout=5)
            for day in range(2, 12):
                import_csv(
                    database,
                    f"B{day},2026-01-01,2026-01-02,2026-03-01,censored",
                )
        except Exception as exc:  # pragma: no cover - failure signaller
            errors.append(exc)
        finally:
            committed.set()

    worker = threading.Thread(target=importer)
    worker.start()
    conn = database.connect()
    try:
        result = calibrate_summary(
            conn,
            on_snapshot=lambda: start.set(),
            on_before_apply=lambda: committed.wait(timeout=15),
        )
    finally:
        conn.close()
    worker.join(timeout=15)

    assert errors == []
    # Recompute the ledger truth independently and compare every group.
    conn = database.connect()
    try:
        truth = {}
        for row in conn.execute(
            "SELECT activation_date, "
            "julianday(followup_start) - julianday(activation_date), "
            "julianday(followup_end) - julianday(activation_date), "
            "CASE outcome WHEN 'failure' THEN 1 ELSE 0 END, COUNT(*) "
            "FROM battery_ledger GROUP BY 1, 2, 3, 4"
        ):
            truth[(row[0], int(row[1]), int(row[2]), int(row[3]))] = int(row[4])
        stored = {
            (row[0], row[1], row[2], row[3]): row[4]
            for row in conn.execute(
                "SELECT activation_date, entry_age, end_age, event, subject_count "
                "FROM summary_counts"
            )
        }
        ledger_total = conn.execute(
            "SELECT COUNT(*) FROM battery_ledger"
        ).fetchone()[0]
    finally:
        conn.close()

    assert stored == truth
    # B2/B3 are corrections of seeded batteries; B4..B11 are eight new ones.
    assert sum(stored.values()) == ledger_total == 11
    assert result.run_id > 0


# --------------------------------------------------------------------------
# Web surface and whole-set read consistency
# --------------------------------------------------------------------------


def test_web_calibrate_shows_before_after_and_stays_off_survival_page(client):
    data = csv_bytes(
        "B1,2026-01-01,2026-01-02,2026-02-01,failure",
        "B2,2026-01-01,2026-01-02,2026-02-01,failure",
    )
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(data), "batch.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    with client.application.app_context():
        db = client.application.config["DATABASE"]
    import sqlite3

    with sqlite3.connect(db) as raw:
        raw.execute(
            "UPDATE summary_counts SET subject_count = 9 "
            "WHERE activation_date = '2026-01-01'"
        )
        raw.commit()

    ledger_page = client.get("/ledger")
    assert "校准汇总" in ledger_page.get_data(as_text=True)
    survival_page = client.get("/survival?cutoff=2026-12-31")
    assert "校准汇总" not in survival_page.get_data(as_text=True)

    response = client.post("/calibrate")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "校准前计数" in text and "校准后计数" in text
    assert ">9<" in text and ">2<" in text

    # A second run reports nothing to fix.
    response = client.post("/calibrate")
    assert "没有需要修正的分组" in response.get_data(as_text=True)


def test_survival_reads_see_a_whole_set_never_a_partial_mix(client):
    import sqlite3

    data = csv_bytes("B1,2026-01-01,2026-01-02,2026-02-01,failure")
    client.post(
        "/upload",
        data={"file": (io.BytesIO(data), "a.csv")},
        content_type="multipart/form-data",
    )
    db_path = client.application.config["DATABASE"]

    with sqlite3.connect(db_path) as raw:
        raw.execute(
            "INSERT INTO summary_counts "
            "(activation_date, entry_age, end_age, event, subject_count) "
            "VALUES ('2025-12-31', 0, 9, 1, 7)"
        )
        raw.commit()

    import calibration as calibration_module

    hold = threading.Event()
    release = threading.Event()

    def hold_commit():
        hold.set()
        assert release.wait(timeout=5)

    def run_calibration():
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            calibration_module.calibrate_summary(conn, on_before_commit=hold_commit)
        finally:
            conn.close()

    worker = threading.Thread(target=run_calibration)
    worker.start()
    assert hold.wait(timeout=5)

    # While the calibration write transaction is open, the survival page must
    # still return the complete pre-calibration set (phantom group visible).
    page = client.get("/survival?cutoff=2026-12-31")
    assert page.status_code == 200
    pre = client.get("/ledger").get_data(as_text=True)
    assert "8" in pre  # 1 real + 7 phantom, pre-calibration total

    release.set()
    worker.join(timeout=10)

    post = client.get("/ledger").get_data(as_text=True)
    assert "寿命汇总电池数" in post
    # Phantom group is gone; only the single real battery remains.
    with sqlite3.connect(db_path) as raw:
        rows = list(raw.execute("SELECT * FROM summary_counts"))
    assert len(rows) == 1
    assert rows[0][-1] == 1
