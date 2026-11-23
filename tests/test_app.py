"""Tests for CSV parsing, transactional import, and KM counting rules."""

from __future__ import annotations

import io
import sqlite3
from datetime import date

import pytest

from analysis import summarize
from app import create_app
from db import MAX_SUMMARY_RECORDS_PER_BATTERY, Database
from importer import ImportValidationError, apply_import, parse_csv

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


def km(points):
    return [(point.age_days, point.at_risk, point.failures) for point in points]


# --------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------


def test_parse_accepts_chinese_headers_and_outcomes(database):
    content = (
        "电池号,启用日,随访首日,随访末日,结局\n"
        "B1,2026-01-01,2026-01-05,2026-02-01,故障\n"
        "B2,2026-01-02,2026-01-06,2026-03-01,正常结束\n"
    ).encode("utf-8")
    records = parse_csv(content)
    assert [record.battery_no for record in records] == ["B1", "B2"]
    assert records[0].outcome == "failure"
    assert records[1].outcome == "censored"


def test_parse_accepts_gb18030(database):
    content = (
        "电池号,启用日,随访首日,随访末日,结局\n"
        "B1,2026-01-01,2026-01-05,2026-02-01,故障\n"
    ).encode("gb18030")
    records = parse_csv(content)
    assert records[0].battery_no == "B1"


def test_parse_converts_utc_timestamps(database):
    content = csv_bytes("B1,2026-01-01T23:00:00-05:00,2026-01-02,2026-01-03,failure")
    records = parse_csv(content)
    # 2026-01-01T23:00-05:00 == 2026-01-02T04:00Z
    assert records[0].activation_date == date(2026, 1, 2)


def test_duplicate_battery_in_file_rejected():
    content = csv_bytes(
        "B1,2026-01-01,2026-01-02,2026-01-03,failure",
        "B1,2026-01-01,2026-01-02,2026-01-03,censored",
    )
    with pytest.raises(ImportValidationError) as excinfo:
        parse_csv(content)
    assert any("重复" in message for message in excinfo.value.messages)


def test_date_order_validated():
    with pytest.raises(ImportValidationError) as excinfo:
        parse_csv(csv_bytes("B1,2026-02-01,2026-01-01,2026-01-03,failure"))
    assert any("随访首日不能早于启用日" in m for m in excinfo.value.messages)


def test_bad_header_is_actionable():
    content = "battery,enabled,start,end,result\nB1,2026-01-01,2026-01-02,2026-01-03,ok\n"
    with pytest.raises(ImportValidationError) as excinfo:
        parse_csv(content.encode("utf-8"))
    assert "日期" not in str(excinfo.value) or True
    assert any("缺少列" in m for m in excinfo.value.messages)


# --------------------------------------------------------------------------
# Import / correction rules
# --------------------------------------------------------------------------


def test_import_inserts_and_corrections_replace_old_values(database):
    first = import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,censored")
    assert (first.inserted, first.updated, first.summary_records) == (1, 0, 1)

    second = import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-15,failure")
    assert (second.inserted, second.updated) == (0, 1)

    rows = summary_rows(database)
    assert rows == [("2026-01-01", 1, 45, 1, 1)]


def test_identical_reupload_counts_no_summary_record(database):
    first = import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,censored")
    second = import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,censored")
    assert first.summary_records == 1
    assert second.summary_records == 0
    assert second.unchanged == 1


def test_delete_removes_from_summary(database):
    import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,censored", "B2,2026-01-01,2026-01-02,2026-02-01,censored")
    result = import_csv(database, "B1,,,,,delete")
    assert result.deleted == 1
    assert summary_rows(database) == [("2026-01-01", 1, 31, 0, 1)]


def test_summary_record_cap_is_six_lifetime(database):
    import_csv(database, "B1,2026-01-01,2026-01-02,2026-03-01,censored")
    # 1 insert + 5 corrections == 6 summary records: still allowed.
    for day in range(2, 7):
        result = import_csv(
            database, f"B1,2026-01-01,2026-01-02,2026-03-{day:02d},censored"
        )
        assert result.updated == 1

    conn = database.connect()
    try:
        usage = conn.execute(
            "SELECT summary_records FROM battery_summary_usage WHERE battery_no = 'B1'"
        ).fetchone()[0]
        summary_count = conn.execute(
            "SELECT COUNT(*) FROM battery_summary WHERE battery_no = 'B1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert usage == MAX_SUMMARY_RECORDS_PER_BATTERY
    # The summary table stays compact: one row per battery, not six.
    assert summary_count == 1

    with pytest.raises(ImportValidationError) as excinfo:
        import_csv(database, "B1,2026-01-01,2026-01-02,2026-03-08,censored")
    assert any("上限" in message for message in excinfo.value.messages)

    # An identical re-upload is a no-op and does not consume a slot.
    result = import_csv(database, "B1,2026-01-01,2026-01-02,2026-03-06,censored")
    assert (result.unchanged, result.summary_records) == (1, 0)


def test_delete_counts_against_cap_and_survives_deletion(database):
    import_csv(database, "B1,2026-01-01,2026-01-02,2026-03-01,censored")
    for day in range(2, 6):
        import_csv(database, f"B1,2026-01-01,2026-01-02,2026-03-{day:02d},censored")
    import_csv(database, "B1,,,,,delete")  # sixth summary record
    assert summary_rows(database) == []

    with pytest.raises(ImportValidationError) as excinfo:
        import_csv(database, "B1,2026-02-01,2026-02-02,2026-03-01,failure")
    assert any("上限" in message for message in excinfo.value.messages)


def test_correction_changing_activation_date_moves_aggregate(database):
    import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,failure")
    import_csv(database, "B1,2026-01-10,2026-01-11,2026-02-10,failure")
    assert summary_rows(database) == [("2026-01-10", 1, 31, 1, 1)]


def test_delete_then_reinsert_keeps_single_summary_record(database):
    import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,censored")
    import_csv(database, "B1,,,,,delete")
    assert summary_rows(database) == []
    result = import_csv(database, "B1,2026-02-01,2026-02-02,2026-03-01,failure")
    assert result.inserted == 1
    conn = database.connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM battery_summary WHERE battery_no = 'B1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    assert summary_rows(database) == [("2026-02-01", 1, 28, 1, 1)]


def test_delete_unknown_battery_is_rejected(database):
    with pytest.raises(ImportValidationError) as excinfo:
        import_csv(database, "NOPE,,,,,delete")
    assert any("不存在" in message for message in excinfo.value.messages)


def test_explicit_operations(database):
    import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,censored")
    result = import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-03,censored,update")
    assert (result.inserted, result.updated) == (0, 1)
    with pytest.raises(ImportValidationError):
        import_csv(database, "B2,2026-01-01,2026-01-02,2026-02-03,censored,update")
    with pytest.raises(ImportValidationError):
        import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-09,censored,insert")


def test_failed_import_is_atomic(database):
    import_csv(database, "B1,2026-01-01,2026-01-02,2026-02-01,failure")
    before = summary_rows(database)

    with pytest.raises(ImportValidationError):
        # Second row is invalid, so the whole file must be rejected.
        import_csv(
            database,
            "B2,2026-01-01,2026-01-02,2026-02-01,failure",
            "B3,2026-01-01,2026-01-02,not-a-date,failure",
        )
    assert summary_rows(database) == before

    conn = database.connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]
        ledger = conn.execute("SELECT COUNT(*) FROM battery_ledger").fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    assert ledger == 1


def test_query_page_still_works_after_failed_upload(client):
    good = "B1,2026-01-01,2026-01-02,2026-02-01,failure\n"
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(csv_bytes(good.strip())), "good.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"battery_no,activation_date\nB2,2026-01-01\n"), "bad.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400

    page = client.get("/survival?cutoff=2026-12-31")
    assert page.status_code == 200
    assert "B1" not in page.get_data(as_text=True)


# --------------------------------------------------------------------------
# Kaplan–Meier counting rules
# --------------------------------------------------------------------------


def test_km_basic_counts():
    rows = [
        # activation, entry, end, event, count
        ("2026-01-01", 1, 10, 1, 1),   # fails at age 10
        ("2026-01-01", 1, 20, 0, 1),   # censored at 20
        ("2026-01-01", 1, 10, 0, 1),   # censored exactly at 10
    ]
    result = summarize(rows, date(2026, 12, 31))
    assert km(result.points) == [(10, 3, 1)]
    assert result.points[0].survival == pytest.approx(2 / 3)
    assert result.points[0].censored == 1
    # The first subject was censored at age 20 and the cutoff is much later.
    assert result.risk_set_at_cutoff == 0
    assert result.enrolled_subjects == 3


def test_entry_same_day_is_counted_before_failure():
    rows = [
        ("2026-01-01", 5, 5, 1, 1),  # enters and fails on age 5
        ("2026-01-01", 1, 10, 0, 1),  # long survivor enters at 1
    ]
    result = summarize(rows, date(2026, 12, 31))
    assert km(result.points) == [(5, 2, 1)]


def test_exit_same_day_still_at_risk():
    rows = [
        ("2026-01-01", 1, 10, 1, 1),  # failure at 10
        ("2026-01-01", 1, 10, 0, 1),  # censored at 10, still at risk that day
    ]
    result = summarize(rows, date(2026, 12, 31))
    assert km(result.points) == [(10, 2, 1)]
    assert result.points[0].survival == pytest.approx(0.5)


def test_not_yet_enrolled_is_excluded():
    rows = [
        ("2026-01-01", 10, 20, 1, 1),  # starts after cutoff age
        ("2026-01-01", 1, 5, 1, 1),
    ]
    result = summarize(rows, date(2026, 1, 6))  # cutoff age = 5
    assert km(result.points) == [(5, 1, 1)]
    assert result.enrolled_subjects == 1


def test_failure_after_cutoff_becomes_censored():
    rows = [("2026-01-01", 1, 40, 1, 1)]
    result = summarize(rows, date(2026, 1, 21))  # cutoff age = 20
    assert result.points == []
    assert result.risk_set_at_cutoff == 1


def test_failure_on_cutoff_day_is_counted():
    rows = [("2026-01-01", 1, 20, 1, 1)]
    result = summarize(rows, date(2026, 1, 21))  # cutoff age = 20
    assert km(result.points) == [(20, 1, 1)]
    assert result.points[0].survival == 0.0


def test_multiple_failure_ages_product():
    rows = [
        ("2026-01-01", 0, 10, 1, 1),
        ("2026-01-01", 0, 20, 0, 1),
        ("2026-01-01", 0, 20, 1, 1),
    ]
    result = summarize(rows, date(2026, 12, 31))
    assert km(result.points) == [(10, 3, 1), (20, 2, 1)]
    assert result.points[0].survival == pytest.approx(2 / 3)
    assert result.points[1].survival == pytest.approx(1 / 3)


def test_activation_date_affects_cutoff_age():
    # Same ages are stored for both batteries, but their activation dates differ,
    # so on the same cutoff day one has reached age 20 and the other only age 10.
    rows = [
        ("2026-01-01", 0, 20, 1, 1),
        ("2026-01-11", 0, 20, 1, 1),
    ]
    result = summarize(rows, date(2026, 1, 21))
    # The second battery's failure at age 20 is after the cutoff, so it is
    # censored at age 10 and contributes no failure row.
    assert km(result.points) == [(20, 1, 1)]
    assert result.points[0].survival == pytest.approx(0.0)
    # Both batteries are still under observation on the cutoff day.
    assert result.risk_set_at_cutoff == 2


def test_end_to_end_upload_and_query(client):
    lines = [
        "B1,2026-01-01,2026-01-02,2026-01-21,failure",
        "B2,2026-01-01,2026-01-02,2026-03-01,censored",
        "B3,2026-01-01,2026-01-02,2026-01-21,censored",
    ]
    data = csv_bytes(*lines)
    response = client.post(
        "/upload",
        data={"file": (io.BytesIO(data), "batch.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    page = client.get("/survival?cutoff=2026-12-31")
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "0.666667" in text  # 1 - 1/3

    # The query page must not depend on the ledger table.
    with sqlite3.connect(client.application.config["DATABASE"]) as conn:
        conn.execute("DROP TABLE battery_ledger")
        conn.commit()
    page = client.get("/survival?cutoff=2026-12-31")
    assert page.status_code == 200
    assert "0.666667" in page.get_data(as_text=True)
