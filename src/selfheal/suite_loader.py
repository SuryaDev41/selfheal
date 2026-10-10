"""Read spreadsheet test cases into the runner's normalized format."""

from __future__ import annotations

from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = {"TestCase ID", "Title", "Test Step", "Test Data", "Expected Result"}


def read_excel_tests(file_path: str | Path) -> list[dict[str, Any]]:
    """Return enabled test cases from a workbook, preserving spreadsheet row numbers."""
    import openpyxl

    workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        header_row = next(sheet.iter_rows(min_row=1, max_row=1))
        headers = [str(cell.value or "").strip() for cell in header_row]
        missing = REQUIRED_COLUMNS.difference(headers)
        if missing:
            raise ValueError("Missing Excel columns: " + ", ".join(sorted(missing)))

        tests: list[dict[str, Any]] = []
        for row_number, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
            values = dict(zip(headers, row, strict=False))
            if not values.get("TestCase ID"):
                continue
            if str(values.get("Execute") or "").strip().casefold() in {"no", "skip", "false"}:
                continue
            tests.append(
                {
                    "id": str(values["TestCase ID"]),
                    "title": str(values.get("Title") or ""),
                    "step": str(values.get("Test Step") or ""),
                    "data": values.get("Test Data"),
                    "expected": str(values.get("Expected Result") or ""),
                    "row": row_number,
                }
            )
        return tests
    finally:
        workbook.close()
