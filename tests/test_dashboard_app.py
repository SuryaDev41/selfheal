"""Smoke-test the Streamlit dashboard without calling external observability APIs."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from selfheal.dashboard import data, history


def test_dashboard_renders_without_langsmith_network(monkeypatch):
    monkeypatch.setattr(data, "langsmith_settings", lambda _root: (None, "Not configured for test."))
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)

    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)

    assert not app.exception
    assert app.title[0].value == "SelfHeal Control"
    assert app.sidebar.button[0].label == "Run Selected Suite"


def test_dashboard_history_filters_include_both_websites(monkeypatch):
    profiles = [
        {"name": name, "environments": {"prod": f"https://{name}.test"}, "path": Path(f"{name}.yaml")}
        for name in ("automationexercise", "demowebshop")
    ]
    monkeypatch.setattr(data, "discover_profiles", lambda _root: profiles)
    monkeypatch.setattr(data, "discover_suites", lambda _root: [])
    monkeypatch.setattr(data, "load_runs", lambda _root: [])
    monkeypatch.setattr(data, "load_cache_entries", lambda _root: [{"app": "demowebshop"}])
    monkeypatch.setattr(data, "load_healing_events", lambda _root: [
        {"app": "automationexercise", "event_type": "ai_review_required"}
    ])
    monkeypatch.setattr(data, "langsmith_settings", lambda _root: ("selfheal", None))
    monkeypatch.setattr(data, "load_langsmith_runs", lambda _project, root: [
        {"application": "automationexercise", "suite_run_id": "run_1", "trace_url": None},
        {"application": "demowebshop", "suite_run_id": "run_2", "trace_url": None},
    ])
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)

    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)
    app.radio[0].set_value("Cache History").run(timeout=10)
    assert not app.exception
    assert {"automationexercise", "demowebshop"}.issubset(set(app.selectbox[0].options))

    app.radio[0].set_value("LangSmith").run(timeout=10)
    assert not app.exception
    assert {"automationexercise", "demowebshop"}.issubset(set(app.selectbox[0].options))
