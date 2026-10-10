"""Tests for Excel-backed dashboard launch history."""

from openpyxl import load_workbook

from selfheal.dashboard.history import finish_dashboard_run, start_dashboard_run
from selfheal.time_utils import now_ist


def test_dashboard_history_records_launch_and_completion(tmp_path):
    path = tmp_path / "dashboard_history.xlsx"
    start_dashboard_run(
        {
            "launch_id": "launch_001",
            "started_at": now_ist(),
            "website": "demowebshop",
            "suite": "demowebshop_full.xlsx",
            "environment": "prod",
            "headless": True,
            "cases": ["TC_001"],
        },
        path,
    )
    finish_dashboard_run("launch_001", exit_code=0, output="test completed", path=path)

    workbook = load_workbook(path, data_only=True)
    sheet = workbook["Dashboard Runs"]
    row = [cell.value for cell in sheet[2]]

    assert row[0] == "launch_001"
    assert row[3] == "passed"
    assert row[4] == 0
    assert row[10] == "test completed"
