"""Flask application: battery follow-up CSV import and KM survival page."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from analysis import summarize
from db import MAX_SUMMARY_RECORDS_PER_BATTERY, Database, ledger_rows
from importer import (
    ImportValidationError,
    apply_import,
    parse_csv,
)

MAX_UPLOAD_BYTES = 16 * 1024 * 1024


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get(
            "BATTERY_DB", str(Path(app.root_path) / "data" / "battery.db")
        ),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SECRET_KEY=os.environ.get("SECRET_KEY", "battery-survival-local-dev"),
    )
    if config:
        app.config.update(config)

    database = Database(app.config["DATABASE"])
    database.initialize()

    @app.context_processor
    def inject_globals() -> dict:
        return {
            "max_summary_records": MAX_SUMMARY_RECORDS_PER_BATTERY,
            "today_utc": _utc_today().isoformat(),
        }

    @app.get("/")
    def index():
        return redirect(url_for("survival_page"))

    @app.get("/upload")
    def upload_page():
        return render_template(
            "upload.html",
            result=None,
            errors=[],
            filename="",
            batches=_recent_batches(database),
        )

    @app.post("/upload")
    def upload():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return (
                render_template(
                    "upload.html",
                    result=None,
                    errors=["请选择一个 CSV 文件再上传。"],
                    filename="",
                    batches=_recent_batches(database),
                ),
                400,
            )

        filename = secure_filename(uploaded.filename) or "upload.csv"
        errors: list[str] = []
        try:
            data = uploaded.read()
            records = parse_csv(data)
            conn = database.connect()
            try:
                result = apply_import(conn, filename, records)
            finally:
                conn.close()
        except ImportValidationError as exc:
            errors = exc.shown_messages
            result = None
        except sqlite3.Error as exc:
            app.logger.exception("import failed")
            errors = [f"写入数据库失败，未做任何更改：{exc}"]
            result = None
        except Exception:  # pragma: no cover - defensive; transaction rolled back
            app.logger.exception("unexpected import failure")
            errors = ["导入过程中发生未预期错误，未做任何更改，原有曲线仍可查询。"]
            result = None
        if result is not None:
            flash(
                f"导入成功：新增 {result.inserted}，更正 {result.updated}，"
                f"删除 {result.deleted}，未变化 {result.unchanged}；"
                f"本次写入汇总记录 {result.summary_records} 条。"
            )
            return redirect(url_for("upload_page"))

        return (
            render_template(
                "upload.html",
                result=None,
                errors=errors,
                filename=filename,
                batches=_recent_batches(database),
            ),
            400,
        )

    @app.get("/survival")
    def survival_page():
        cutoff_text = request.args.get("cutoff", "").strip()
        cutoff: date | None = None
        errors: list[str] = []
        if cutoff_text:
            try:
                cutoff = date.fromisoformat(cutoff_text)
            except ValueError:
                errors.append("截止日必须是 YYYY-MM-DD 格式的 UTC 日期。")
        else:
            cutoff = _utc_today()

        rows: list = []
        result = None
        if cutoff is not None:
            conn = database.connect()
            try:
                rows = _load_summary(conn)
            finally:
                conn.close()
            result = summarize(rows, cutoff)

        return render_template(
            "survival.html",
            cutoff=cutoff,
            cutoff_text=cutoff_text,
            result=result,
            rows=rows,
            errors=errors,
            total_subjects=sum(row[4] for row in rows),
        )

    @app.get("/ledger")
    def ledger_page():
        conn = database.connect()
        try:
            rows = list(ledger_rows(conn))
        finally:
            conn.close()
        return render_template("ledger.html", rows=rows)

    @app.get("/template.csv")
    def template_csv():
        path = Path(app.root_path) / "static" / "template.csv"
        return send_file(path, mimetype="text/csv", as_attachment=True,
                         download_name="battery_followup_template.csv")

    @app.get("/healthz")
    def healthz():
        conn = database.connect()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return {"status": "ok"}

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_exc):
        return (
            render_template(
                "upload.html",
                result=None,
                errors=[f"文件超过大小上限（{MAX_UPLOAD_BYTES // (1024 * 1024)} MB）。"],
                filename="",
                batches=_recent_batches(database),
            ),
            413,
        )

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", code=404, message="页面不存在。"), 404

    return app


def _load_summary(conn: sqlite3.Connection):
    return [
        (
            row["activation_date"],
            int(row["entry_age"]),
            int(row["end_age"]),
            int(row["event"]),
            int(row["subject_count"]),
        )
        for row in conn.execute(
            "SELECT activation_date, entry_age, end_age, event, subject_count "
            "FROM summary_counts"
        )
    ]


def _recent_batches(database: Database):
    conn = database.connect()
    try:
        return list(
            conn.execute(
                "SELECT id, filename, imported_at, total_rows, inserted, updated, "
                "deleted, unchanged, summary_records FROM import_batches "
                "ORDER BY id DESC LIMIT 10"
            )
        )
    finally:
        conn.close()


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
