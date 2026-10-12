"""Offline coverage for the dashboard's read-only operational data layer."""

from __future__ import annotations

import json
from pathlib import Path

import langsmith
from openpyxl import Workbook

from selfheal.dashboard.data import (
    clear_cache_entries,
    configured_sites,
    discover_profiles,
    load_cache_entries,
    load_healing_events,
    load_langsmith_runs,
    load_run_details,
    load_runs,
)
from selfheal.telemetry.healing_log import HealingEventStore


def _write_results(root: Path) -> None:
    run_dir = root / "data" / "runs" / "run_20260101_120000_000001"
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "started_at": "2026-01-01T12:00:00",
                "status": "passed",
                "suite": "suites/smoke.xlsx",
                "environment": "prod",
                "base_url": "https://example.test",
                "total": 2,
                "passed": 2,
                "failed": 0,
                "needs_review": 0,
                "healing": {"ai_calls": 1, "cache_hits": 2},
                "tests": [],
            }
        ),
        encoding="utf-8",
    )


def test_dashboard_discovers_profiles_and_run_data(tmp_path):
    config_dir = tmp_path / "config" / "apps"
    config_dir.mkdir(parents=True)
    (config_dir / "example.yaml").write_text(
        "app_name: example\ndashboard:\n  visible: true\nenvironments:\n  prod:\n    base_url: https://example.test\n",
        encoding="utf-8",
    )
    (config_dir / "internal.yaml").write_text(
        "app_name: internal\ndashboard:\n  visible: false\nenvironments:\n  prod:\n    base_url: https://internal.test\n",
        encoding="utf-8",
    )
    _write_results(tmp_path)

    profiles = discover_profiles(tmp_path)
    runs = load_runs(tmp_path)

    assert [profile["name"] for profile in profiles] == ["example"]
    assert profiles[0]["environments"] == {"prod": "https://example.test"}
    assert runs[0]["pass_rate"] == 100.0
    assert runs[0]["cache_hits"] == 2
    assert load_run_details(runs[0]["run_id"], tmp_path)["status"] == "passed"
    assert load_run_details("../outside", tmp_path) is None


def test_configured_sites_require_finished_run_and_saved_suite(tmp_path):
    profile = tmp_path / "config" / "apps" / "generated" / "example.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "app_name: example\ndashboard:\n  visible: true\nenvironments:\n"
        "  prod:\n    base_url: https://example.test\n", encoding="utf-8",
    )
    suite = tmp_path / "suites" / "converted" / "example.xlsx"
    suite.parent.mkdir(parents=True)
    book = Workbook()
    book.active.append(["TestCase ID", "Title", "Test Step", "Test Data", "Expected Result"])
    book.active.append(["TC_001", "Home", "OPEN | /\nASSERT_URL | /", "", ""])
    book.save(suite)
    book.close()
    run_dir = tmp_path / "data" / "runs" / "run_20260101_120000_000001"
    run_dir.mkdir(parents=True)
    results = {
        "run_id": run_dir.name, "started_at": "2026-01-01T12:00:00",
        "status": "passed", "suite": str(suite), "environment": "prod",
        "base_url": "https://example.test", "total": 1, "passed": 1,
        "discovery": {"profile": str(profile), "api_calls": 1},
        "tests": [{"id": "TC_001", "steps": [{"status": "passed"}]}],
    }
    results_file = run_dir / "results.json"
    results_file.write_text(json.dumps(results), encoding="utf-8")
    assert configured_sites(tmp_path) == []

    results["finished_at"] = "2026-01-01T12:01:00"
    results_file.write_text(json.dumps(results), encoding="utf-8")
    sites = configured_sites(tmp_path)
    assert len(sites) == 1
    assert sites[0]["suite"] == suite
    assert sites[0]["profile"] == profile

    suite.unlink()
    assert configured_sites(tmp_path) == []


def test_dashboard_reads_cache_and_healing_events(tmp_path):
    cache_path = tmp_path / "data" / "store" / "elements.db"
    cache_path.parent.mkdir(parents=True)
    import sqlite3

    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            """CREATE TABLE elements (
                app TEXT, env TEXT, page TEXT, target TEXT, locator TEXT, created_at TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO elements VALUES (?, ?, ?, ?, ?, ?)",
            ("example", "prod", "/", "click:save", "#save", "2026-01-01"),
        )

    events = HealingEventStore(tmp_path / "data" / "store" / "healing_events.db")
    events.record(
        run_id="run_1",
        test_case_id="TC_001",
        app="example",
        environment="prod",
        page="/",
        target="Save",
        event_type="cache_hit",
        selector="#save",
    )

    assert load_cache_entries(tmp_path)[0]["locator"] == "#save"
    assert load_cache_entries(tmp_path, limit=None, app="example")[0]["locator"] == "#save"
    assert load_cache_entries(tmp_path, limit=None, app="other") == []
    assert load_healing_events(tmp_path)[0]["event_type"] == "cache_hit"
    assert clear_cache_entries(tmp_path, "example") == 1
    assert load_cache_entries(tmp_path) == []


def test_dashboard_handles_langsmith_records_without_serialized_payload(monkeypatch, tmp_path):
    config_dir = tmp_path / "config" / "apps"
    config_dir.mkdir(parents=True)
    (config_dir / "example.yaml").write_text(
        "app_name: example\ndashboard:\n  visible: true\nenvironments:\n  prod:\n"
        "    base_url: https://example.test\n",
        encoding="utf-8",
    )
    _write_results(tmp_path)

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        @staticmethod
        def list_runs(**_kwargs):
            return iter([{
                "name": "chat.completions",
                "extra": {"metadata": {
                    "suite_run_id": "run_20260101_120000_000001", "test_case_id": "TC_001"
                }},
                "serialized": None,
                "total_tokens": 12,
            }])

        @staticmethod
        def get_run_url(**_kwargs):
            return "https://smith.example/run"

    monkeypatch.setattr(langsmith, "Client", FakeClient)
    monkeypatch.setenv("LANGSMITH_API_KEY", "local-test-key")

    records = load_langsmith_runs("selfheal", limit=1, root=tmp_path)

    assert records[0]["model"] == ""
    assert records[0]["test_case_id"] == "TC_001"
    assert records[0]["application"] == "example"
