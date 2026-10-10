"""Append suite-level run summaries to a readable Excel workbook."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from selfheal.time_utils import to_ist

DEFAULT_HISTORY_PATH = Path("data/logs/run_history.xlsx")
LEGACY_HISTORY_PATH = Path("data/logs/run_history.jsonl")
SHEET_NAME = "Runs"
HEADERS = (
    "Run ID",
    "Started At",
    "Finished At",
    "Duration (s)",
    "App",
    "Suite",
    "Environment",
    "Browser",
    "Headless",
    "Total",
    "Passed",
    "Failed",
    "Needs Review",
    "Status",
    "LLM Calls",
    "Total Tokens",
    "LLM Cost (USD)",
    "Model Usage",
    "Cache Hits",
    "Cache Hit Rate",
    "LangSmith Project",
    "Results File",
    "Run Error",
)

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)
COLUMN_WIDTHS = (30, 21, 21, 14, 20, 30, 14, 14, 11, 10, 10, 10, 15, 15, 12, 14, 17, 22, 13, 16, 20, 55, 40)


def _parse_timestamp(value: Any) -> datetime | None:
    timestamp = to_ist(value if isinstance(value, (str, datetime)) else None)
    return timestamp.replace(tzinfo=None) if timestamp else None


def _duration_seconds(started_at: Any, finished_at: Any) -> float | None:
    start = _parse_timestamp(started_at)
    finish = _parse_timestamp(finished_at)
    if start is None or finish is None:
        return None
    return round(max(0.0, (finish - start).total_seconds()), 3)


def _apply_sheet_layout(sheet) -> None:
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    for index, (header, width) in enumerate(zip(HEADERS, COLUMN_WIDTHS, strict=True), 1):
        cell = sheet.cell(row=1, column=index, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 24


def _update_table_range(sheet) -> None:
    last_column = get_column_letter(len(HEADERS))
    sheet.auto_filter.ref = f"A1:{last_column}{max(1, sheet.max_row)}"
    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row):
        for cell in row:
            cell.alignment = Alignment(vertical="center")
    for column in (2, 3):
        for cell in sheet.iter_cols(min_col=column, max_col=column, min_row=2, max_row=sheet.max_row):
            cell[0].number_format = "yyyy-mm-dd hh:mm:ss"
    for cell in sheet.iter_cols(min_col=4, max_col=4, min_row=2, max_row=sheet.max_row):
        cell[0].number_format = "0.000"
    for cell in sheet.iter_cols(min_col=17, max_col=17, min_row=2, max_row=sheet.max_row):
        cell[0].number_format = '$0.000000'
    for cell in sheet.iter_cols(min_col=20, max_col=20, min_row=2, max_row=sheet.max_row):
        cell[0].number_format = "0.0%"


def _legacy_rows(path: Path) -> list[tuple[Any, ...]]:
    if not path.is_file():
        return []

    rows: list[tuple[Any, ...]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        timestamp = _parse_timestamp(record.get("timestamp"))
        rows.append(
            (
                record.get("run_id"),
                timestamp,
                None,
                record.get("duration_seconds"),
                record.get("app"),
                record.get("suite_name"),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                record.get("llm_calls"),
                record.get("total_tokens"),
                record.get("total_cost_usd"),
                json.dumps(record.get("model_usage", {}), sort_keys=True),
                record.get("cache_hits"),
                record.get("cache_hit_rate"),
                None,
                None,
                None,
            )
        )
    return rows


def _result_row(results: dict[str, Any], config: dict[str, Any], run_dir: Path) -> tuple[Any, ...]:
    tracing = results.get("langsmith_tracing") or {}
    healing = results.get("healing") or {}
    return (
        results.get("run_id"),
        _parse_timestamp(results.get("started_at")),
        _parse_timestamp(results.get("finished_at")),
        _duration_seconds(results.get("started_at"), results.get("finished_at")),
        config.get("app_name"),
        results.get("suite"),
        results.get("environment"),
        results.get("browser"),
        results.get("headless"),
        results.get("total"),
        results.get("passed"),
        results.get("failed"),
        results.get("needs_review"),
        results.get("status"),
        healing.get("ai_calls"),
        healing.get("cache_hits"),
        (healing.get("cache_hits", 0) / healing["resolution_attempts"])
        if healing.get("resolution_attempts")
        else 0,
        None,
        None,
        None,
        tracing.get("project") if tracing.get("enabled") else None,
        str(run_dir / "results.json"),
        results.get("run_error"),
    )


def _existing_result_rows(root: Path, config: dict[str, Any]) -> list[tuple[Any, ...]]:
    if not root.is_dir():
        return []

    rows: list[tuple[Any, ...]] = []
    for results_file in sorted(root.glob("run_*/results.json")):
        try:
            results = json.loads(results_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append(_result_row(results, config, results_file.parent))
    return rows


def _new_workbook(legacy_path: Path, results_root: Path | None, config: dict[str, Any]) -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_NAME
    _apply_sheet_layout(sheet)
    for row in _legacy_rows(legacy_path):
        sheet.append(row)
    if results_root is not None:
        for row in _existing_result_rows(results_root, config):
            sheet.append(row)
    _update_table_range(sheet)
    return workbook


def append_run_history(
    results: dict[str, Any],
    config: dict[str, Any],
    run_dir: str | Path,
    history_path: str | Path = DEFAULT_HISTORY_PATH,
    legacy_path: str | Path = LEGACY_HISTORY_PATH,
    results_root: str | Path | None = None,
) -> Path:
    """Append one finished suite run, importing old JSONL rows on first use."""
    path = Path(history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    migrate_legacy = path.resolve() == DEFAULT_HISTORY_PATH.resolve()
    workbook = (
        load_workbook(path)
        if path.is_file()
        else _new_workbook(
            Path(legacy_path) if migrate_legacy else Path(),
            Path(results_root) if results_root else None,
            config,
        )
    )
    sheet = workbook[SHEET_NAME]
    run_id = results.get("run_id")
    known_run_ids = {sheet.cell(row=row, column=1).value for row in range(2, sheet.max_row + 1)}
    if run_id not in known_run_ids:
        sheet.append(_result_row(results, config, Path(run_dir)))
    _update_table_range(sheet)
    workbook.save(path)
    return path
