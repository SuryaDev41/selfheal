"""Regression tests for phase totals and cache columns in Excel history."""

import json

from openpyxl import Workbook, load_workbook

from selfheal.telemetry.run_history import HEADERS, append_run_history, repair_run_history


def test_history_places_llm_and_cache_metrics_in_correct_columns(tmp_path):
    run_dir = tmp_path / "run_1"
    run_dir.mkdir()
    results = {
        "run_id": "run_1", "started_at": "2026-10-10T12:00:00+05:30",
        "finished_at": "2026-10-10T12:00:01+05:30", "suite": "suites/converted/case.xlsx",
        "environment": "prod", "browser": "chromium", "headless": False,
        "total": 1, "passed": 1, "failed": 0, "needs_review": 0, "status": "passed",
        "llm_calls": 3, "healing": {"ai_calls": 1, "cache_hits": 2, "resolution_attempts": 3},
    }
    path = tmp_path / "history.xlsx"
    append_run_history(results, {"app_name": "fixture"}, run_dir, path)
    sheet = load_workbook(path, read_only=True, data_only=True)["Runs"]
    assert [sheet.cell(row=2, column=column).value for column in (15, 16, 17, 18, 19)] == [
        3, None, None, None, 2,
    ]
    assert sheet.cell(row=2, column=20).value == 2 / 3


def test_history_repair_uses_saved_result_without_touching_other_columns(tmp_path):
    run_id = "run_20261010_153259_899909"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text(json.dumps({
        "run_id": run_id, "discovery": {"api_calls": 5},
        "healing": {"ai_calls": 18, "cache_hits": 5, "resolution_attempts": 23},
    }), encoding="utf-8")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Runs"
    sheet.append(HEADERS)
    row = [None] * len(HEADERS)
    row[0] = run_id
    row[13] = "failed"
    row[14] = 18
    row[15] = 5
    row[16] = 5 / 23
    sheet.append(row)
    path = tmp_path / "history.xlsx"
    workbook.save(path)

    assert repair_run_history(path, tmp_path / "runs") == 1
    repaired = load_workbook(path, read_only=True, data_only=True)["Runs"]
    assert repaired.cell(row=2, column=14).value == "failed"
    assert repaired.cell(row=2, column=15).value == 18
    assert repaired.cell(row=2, column=16).value is None
    assert repaired.cell(row=2, column=17).value is None
    assert repaired.cell(row=2, column=19).value == 5
    assert repaired.cell(row=2, column=20).value == 5 / 18
    assert repair_run_history(path, tmp_path / "runs") == 0
