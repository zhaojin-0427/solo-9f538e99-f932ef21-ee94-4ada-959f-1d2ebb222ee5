"""CSV parsing and transactional importing for battery follow-up records."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from db import (
    MAX_SUMMARY_RECORDS_PER_BATTERY,
    apply_summary_changes,
    record_ledger_change,
    record_usage,
    summary_from_values,
    usage_for,
)

MAX_ROWS = 200_000
MAX_BATTERY_NO_LENGTH = 128
MAX_ERRORS_SHOWN = 100
_CHUNK_SIZE = 400

REQUIRED_FIELDS = (
    "battery_no",
    "activation_date",
    "followup_start",
    "followup_end",
    "outcome",
)

# Accept both the canonical snake_case headers used by the template and the
# Chinese labels used in the operations spreadsheet.  Matching is case- and
# space-insensitive; a leading UTF-8 BOM is stripped when decoding.
HEADER_ALIASES = {
    "battery_no": {
        "battery_no",
        "battery",
        "battery_id",
        "battery_number",
        "battery_no.",
        "电池号",
        "电池编号",
        "电池id",
        "电池序号",
        "编号",
    },
    "activation_date": {
        "activation_date",
        "activation",
        "enabled_date",
        "enable_date",
        "start_date",
        "commission_date",
        "启用日",
        "启用日期",
        "投运日",
        "投运日期",
        "开始使用日期",
    },
    "followup_start": {
        "followup_start",
        "followup_start_date",
        "follow_up_start",
        "follow_up_start_date",
        "first_followup",
        "first_followup_date",
        "随访首日",
        "随访开始日",
        "随访开始日期",
        "首次随访日",
        "首次随访日期",
    },
    "followup_end": {
        "followup_end",
        "followup_end_date",
        "follow_up_end",
        "follow_up_end_date",
        "last_followup",
        "last_followup_date",
        "随访末日",
        "随访结束日",
        "随访结束日期",
        "末次随访日",
        "末次随访日期",
    },
    "outcome": {
        "outcome",
        "result",
        "status",
        "ending",
        "结局",
        "结果",
        "状态",
        "随访结局",
    },
    "action": {
        "action",
        "operation",
        "op",
        "操作",
        "动作",
        "变更类型",
    },
}

OUTCOME_ALIASES = {
    "failure": {
        "failure",
        "failed",
        "fault",
        "faulted",
        "event",
        "broken",
        "故障",
        "发生故障",
        "失效",
        "故障结束",
        "故障失效",
        "1",
        "true",
        "yes",
        "y",
    },
    "censored": {
        "censored",
        "censor",
        "right_censored",
        "normal",
        "normal_end",
        "ended",
        "end",
        "no_failure",
        "正常",
        "正常结束",
        "结束",
        "未故障",
        "未发生故障",
        "完好",
        "0",
        "false",
        "no",
        "n",
    },
}

ACTION_ALIASES = {
    "auto": {"", "auto", "upsert", "自动", "自动判断"},
    "insert": {"insert", "new", "add", "新增", "补录", "插入"},
    "update": {"update", "correct", "correction", "modify", "更正", "修改", "更新"},
    "delete": {"delete", "remove", "删除", "作废"},
}

FIELD_LABELS = {
    "battery_no": "电池号",
    "activation_date": "启用日",
    "followup_start": "随访首日",
    "followup_end": "随访末日",
    "outcome": "结局",
}


class ImportValidationError(Exception):
    """The whole CSV is rejected and no database change is made."""

    def __init__(self, messages: Sequence[str] | str) -> None:
        if isinstance(messages, str):
            messages = [messages]
        self.messages = list(messages)
        super().__init__("；".join(self.messages))

    @property
    def shown_messages(self) -> list[str]:
        shown = self.messages[:MAX_ERRORS_SHOWN]
        if len(self.messages) > MAX_ERRORS_SHOWN:
            shown.append(
                f"……另有 {len(self.messages) - MAX_ERRORS_SHOWN} 条错误未显示，"
                "请先修正以上问题后重新上传。"
            )
        return shown


@dataclass(frozen=True)
class FollowupRecord:
    battery_no: str
    activation_date: date | None
    followup_start: date | None
    followup_end: date | None
    outcome: str | None
    action: str
    line_no: int

    def ledger_values(self) -> tuple[str, str, str, str, str]:
        if not (
            self.activation_date and self.followup_start and self.followup_end
            and self.outcome
        ):
            raise ValueError("incomplete record")
        return (
            self.battery_no,
            self.activation_date.isoformat(),
            self.followup_start.isoformat(),
            self.followup_end.isoformat(),
            self.outcome,
        )


@dataclass(frozen=True)
class ImportResult:
    total_rows: int
    inserted: int
    updated: int
    deleted: int
    unchanged: int
    summary_records: int
    batch_id: int


@dataclass
class _Plan:
    record: FollowupRecord
    operation: str  # insert / update / delete / unchanged
    old_values: tuple[str, str, str, str, str] | None = None


def _normalize_token(value: str) -> str:
    value = value.strip().lstrip("﻿").lower()
    value = re.sub(r"[\s\-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value


def _alias_lookup(aliases: dict[str, set[str]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, values in aliases.items():
        for value in values:
            lookup[_normalize_token(value)] = canonical
    return lookup


_HEADER_LOOKUP = _alias_lookup(HEADER_ALIASES)
_OUTCOME_LOOKUP = _alias_lookup(OUTCOME_ALIASES)
_ACTION_LOOKUP = _alias_lookup(ACTION_ALIASES)


def _decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportValidationError("CSV 必须是 UTF-8 或 GB18030 编码的文本文件。")


def parse_date(raw: str, *, field: str, line_no: int) -> date:
    """Parse a UTC calendar date.  Timestamps are converted to UTC first."""

    text = raw.strip()
    if not text:
        raise ImportValidationError(f"第 {line_no} 行：{field}不能为空。")

    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", text):
            year, month, day = (int(part) for part in text.split("/"))
            return date(year, month, day)
        if re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", text):
            year, month, day = (int(part) for part in text.split("."))
            return date(year, month, day)

        iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date()
    except (ValueError, OverflowError):
        raise ImportValidationError(
            f"第 {line_no} 行：{field}“{raw}”不是有效日期，请使用 YYYY-MM-DD（UTC）。"
        ) from None


def _parse_outcome(raw: str, line_no: int) -> str:
    token = _normalize_token(raw)
    outcome = _OUTCOME_LOOKUP.get(token)
    if outcome is None:
        # Be forgiving about decorated labels such as "故障（已更换）" or
        # "正常结束/退役"; the exact aliases above win first.  Negations are
        # checked before the generic "故障" keyword.
        if any(word in token for word in ("无故障", "未故障", "未发生故障", "正常")):
            outcome = "censored"
        elif "故障" in token or "失效" in token:
            outcome = "failure"
        elif "结束" in token:
            outcome = "censored"
    if outcome is None:
        raise ImportValidationError(
            f"第 {line_no} 行：结局“{raw}”无法识别，请填写“故障”或“正常结束”。"
        )
    return outcome


def _parse_action(raw: str, line_no: int) -> str:
    token = _normalize_token(raw)
    action = _ACTION_LOOKUP.get(token)
    if action is None:
        if "删除" in token or "作废" in token:
            action = "delete"
        elif "更正" in token or "修改" in token or "更新" in token:
            action = "update"
        elif "补录" in token or "新增" in token:
            action = "insert"
    if action is None:
        raise ImportValidationError(
            f"第 {line_no} 行：操作“{raw}”无法识别，可用 补录/更正/删除（或留空自动判断）。"
        )
    return action


def parse_csv(data: bytes, *, max_rows: int = MAX_ROWS) -> list[FollowupRecord]:
    """Parse and validate a CSV without touching the database."""

    if not data or not data.strip():
        raise ImportValidationError("上传的文件为空。")

    text = _decode_csv(data)
    reader = csv.reader(io.StringIO(text, newline=""))
    header: list[str] | None = None
    rows: list[tuple[int, list[str]]] = []

    for raw_row in reader:
        if not any(cell.strip() for cell in raw_row):
            continue
        if header is None:
            header = raw_row
            continue
        rows.append((reader.line_num, raw_row))

    if header is None:
        raise ImportValidationError("CSV 缺少表头。")
    if not rows:
        raise ImportValidationError("CSV 没有数据行。")
    if len(rows) > max_rows:
        raise ImportValidationError(f"CSV 数据行超过上限（最多 {max_rows} 行）。")

    columns: dict[str, int] = {}
    errors: list[str] = []
    for index, name in enumerate(header):
        canonical = _HEADER_LOOKUP.get(_normalize_token(name))
        if canonical is None:
            continue
        if canonical in columns:
            errors.append(f"表头第 {index + 1} 列“{name}”与前面的列重复。")
        else:
            columns[canonical] = index

    missing = [field for field in REQUIRED_FIELDS if field not in columns]
    if missing:
        errors.append(
            "CSV 缺少列：" + "、".join(FIELD_LABELS[field] for field in missing) + "。"
        )
    if errors:
        raise ImportValidationError(errors)

    records: list[FollowupRecord] = []
    seen: set[str] = set()
    for line_no, row in rows:
        if len(row) > len(header):
            extra = [cell for cell in row[len(header) :] if cell.strip()]
            if extra:
                errors.append(f"第 {line_no} 行：列数多于表头，请检查逗号是否未加引号。")
                continue
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))

        def cell(field: str) -> str:
            index = columns.get(field)
            return row[index] if index is not None and index < len(row) else ""

        battery_no = cell("battery_no").strip()
        if not battery_no:
            errors.append(f"第 {line_no} 行：电池号不能为空。")
            continue
        if len(battery_no) > MAX_BATTERY_NO_LENGTH:
            errors.append(
                f"第 {line_no} 行：电池号超过 {MAX_BATTERY_NO_LENGTH} 个字符。"
            )
            continue
        if any(ord(char) < 32 for char in battery_no):
            errors.append(f"第 {line_no} 行：电池号含有不可见控制字符。")
            continue
        if battery_no in seen:
            errors.append(
                f"第 {line_no} 行：电池号“{battery_no}”在文件内重复；"
                "同一文件内电池号必须唯一。"
            )
            continue
        seen.add(battery_no)

        try:
            action = _parse_action(cell("action"), line_no)
        except ImportValidationError as exc:
            errors.extend(exc.messages)
            continue
        if action == "delete":
            records.append(
                FollowupRecord(
                    battery_no=battery_no,
                    activation_date=None,
                    followup_start=None,
                    followup_end=None,
                    outcome=None,
                    action=action,
                    line_no=line_no,
                )
            )
            continue

        try:
            activation = parse_date(
                cell("activation_date"),
                field=FIELD_LABELS["activation_date"],
                line_no=line_no,
            )
            followup_start = parse_date(
                cell("followup_start"),
                field=FIELD_LABELS["followup_start"],
                line_no=line_no,
            )
            followup_end = parse_date(
                cell("followup_end"),
                field=FIELD_LABELS["followup_end"],
                line_no=line_no,
            )
            outcome = _parse_outcome(cell("outcome"), line_no)
        except ImportValidationError as exc:
            errors.extend(exc.messages)
            continue

        if activation > followup_start:
            errors.append(f"第 {line_no} 行：随访首日不能早于启用日。")
            continue
        if followup_start > followup_end:
            errors.append(f"第 {line_no} 行：随访末日不能早于随访首日。")
            continue

        records.append(
            FollowupRecord(
                battery_no=battery_no,
                activation_date=activation,
                followup_start=followup_start,
                followup_end=followup_end,
                outcome=outcome,
                action=action,
                line_no=line_no,
            )
        )

    if errors:
        raise ImportValidationError(errors)
    if not records:
        raise ImportValidationError("CSV 没有可导入的数据行。")
    return records


def _chunks(values: Sequence[str], size: int = _CHUNK_SIZE) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _load_existing(
    conn, battery_nos: Sequence[str]
) -> tuple[dict[str, tuple[str, str, str, str, str]], dict[str, int]]:
    existing: dict[str, tuple[str, str, str, str, str]] = {}
    for chunk in _chunks(battery_nos):
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            "SELECT battery_no, activation_date, followup_start, followup_end, outcome "
            f"FROM battery_ledger WHERE battery_no IN ({placeholders})",
            list(chunk),
        ):
            existing[row[0]] = tuple(row)
    return existing, usage_for(conn, battery_nos)


def _plan_imports(
    records: Sequence[FollowupRecord],
    existing: dict[str, tuple[str, str, str, str, str]],
    usage: dict[str, int],
) -> list[_Plan]:
    plans: list[_Plan] = []
    errors: list[str] = []

    for record in records:
        old = existing.get(record.battery_no)
        if record.action == "delete":
            if old is None:
                errors.append(
                    f"第 {record.line_no} 行：电池号“{record.battery_no}”不存在，无法删除。"
                )
                continue
            if usage.get(record.battery_no, 0) >= MAX_SUMMARY_RECORDS_PER_BATTERY:
                errors.append(_cap_message(record))
                continue
            plans.append(_Plan(record=record, operation="delete", old_values=old))
            continue

        new_values = record.ledger_values()
        if old is None:
            if record.action == "update":
                errors.append(
                    f"第 {record.line_no} 行：电池号“{record.battery_no}”不存在，无法更正。"
                )
                continue
            operation = "insert"
        else:
            if record.action == "insert":
                errors.append(
                    f"第 {record.line_no} 行：电池号“{record.battery_no}”已存在；"
                    "如需修正请留空操作、使用“更正”，或先“删除”。"
                )
                continue
            if old == new_values:
                plans.append(_Plan(record=record, operation="unchanged", old_values=old))
                continue
            operation = "update"

        if usage.get(record.battery_no, 0) >= MAX_SUMMARY_RECORDS_PER_BATTERY:
            errors.append(_cap_message(record))
            continue
        plans.append(_Plan(record=record, operation=operation, old_values=old))

    if errors:
        raise ImportValidationError(errors)
    return plans


def _cap_message(record: FollowupRecord) -> str:
    return (
        f"第 {record.line_no} 行：电池号“{record.battery_no}”的汇总记录已达 "
        f"{MAX_SUMMARY_RECORDS_PER_BATTERY} 条上限（补录/更正/删除合计），"
        "不能再写入。"
    )


def apply_import(conn, filename: str, records: Sequence[FollowupRecord]) -> ImportResult:
    """Apply a parsed file atomically.

    Any validation or database error rolls back the ledger, the per-battery
    summary records, the aggregate counts and the batch row together, so a
    previously queryable curve stays intact.
    """

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    battery_nos = [record.battery_no for record in records]

    conn.execute("BEGIN IMMEDIATE")
    committed = False
    inserted = updated = deleted = unchanged = summary_records = 0
    batch_id = 0
    try:
        existing, usage = _load_existing(conn, battery_nos)
        plans = _plan_imports(records, existing, usage)

        batch_id = int(
            conn.execute(
                "INSERT INTO import_batches "
                "(filename, imported_at, total_rows, inserted, updated, deleted, "
                "unchanged, summary_records) VALUES (?, ?, ?, 0, 0, 0, 0, 0)",
                (filename, now, len(records)),
            ).lastrowid
        )

        changes: list[tuple] = []

        for plan in plans:
            record = plan.record
            if plan.operation == "unchanged":
                unchanged += 1
                continue

            if plan.operation == "delete":
                conn.execute(
                    "DELETE FROM battery_ledger WHERE battery_no = ?",
                    (record.battery_no,),
                )
                conn.execute(
                    "DELETE FROM battery_summary WHERE battery_no = ?",
                    (record.battery_no,),
                )
                changes.append((summary_from_values(plan.old_values), None))
                record_usage(conn, record.battery_no)
                record_ledger_change(conn, record.battery_no, "delete", now)
                deleted += 1
                summary_records += 1
                continue

            new_values = record.ledger_values()
            new_summary = summary_from_values(new_values)
            if plan.operation == "insert":
                conn.execute(
                    "INSERT INTO battery_ledger "
                    "(battery_no, activation_date, followup_start, followup_end, outcome, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*new_values, now, now),
                )
                conn.execute(
                    "INSERT INTO battery_summary "
                    "(battery_no, activation_date, entry_age, end_age, event, revision, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        record.battery_no,
                        new_summary.activation_date,
                        new_summary.entry_age,
                        new_summary.end_age,
                        new_summary.event,
                        now,
                        now,
                    ),
                )
                inserted += 1
                changes.append((None, new_summary))
                record_ledger_change(conn, record.battery_no, "insert", now)
            else:
                conn.execute(
                    "UPDATE battery_ledger SET activation_date = ?, followup_start = ?, "
                    "followup_end = ?, outcome = ?, updated_at = ? WHERE battery_no = ?",
                    (
                        new_values[1],
                        new_values[2],
                        new_values[3],
                        new_values[4],
                        now,
                        record.battery_no,
                    ),
                )
                # Overwrite the single summary record in place and bump its
                # revision, so corrections never accumulate extra records.
                conn.execute(
                    "INSERT INTO battery_summary "
                    "(battery_no, activation_date, entry_age, end_age, event, revision, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT (battery_no) DO UPDATE SET "
                    "activation_date = excluded.activation_date, "
                    "entry_age = excluded.entry_age, "
                    "end_age = excluded.end_age, "
                    "event = excluded.event, "
                    "revision = battery_summary.revision + 1, "
                    "updated_at = excluded.updated_at",
                    (
                        record.battery_no,
                        new_summary.activation_date,
                        new_summary.entry_age,
                        new_summary.end_age,
                        new_summary.event,
                        now,
                        now,
                    ),
                )
                updated += 1
                changes.append(
                    (summary_from_values(plan.old_values), new_summary)
                )
                record_ledger_change(conn, record.battery_no, "update", now)
            record_usage(conn, record.battery_no)
            summary_records += 1

        apply_summary_changes(conn, changes)

        conn.execute(
            "UPDATE import_batches SET inserted = ?, updated = ?, deleted = ?, "
            "unchanged = ?, summary_records = ? WHERE id = ?",
            (inserted, updated, deleted, unchanged, summary_records, batch_id),
        )
        conn.execute("COMMIT")
        committed = True
    finally:
        if not committed:
            conn.execute("ROLLBACK")

    return ImportResult(
        total_rows=len(records),
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        unchanged=unchanged,
        summary_records=summary_records,
        batch_id=batch_id,
    )
