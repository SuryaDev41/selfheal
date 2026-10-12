"""Smoke-test the Streamlit dashboard without calling external observability APIs."""

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import openpyxl
from langsmith.utils import LangSmithConnectionError
from streamlit.testing.v1 import AppTest

from selfheal.dashboard import data, history


def test_langsmith_history_reset_filters_remote_runs(monkeypatch, tmp_path):
    captured = {}

    class FakeClient:
        def __init__(self, api_key):
            assert api_key == "test-key"

        def list_runs(self, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setattr("langsmith.Client", FakeClient)
    cutoff = datetime(2026, 10, 11, 12, 30, tzinfo=UTC)
    marker = tmp_path / "data" / "dashboard" / "langsmith_history_cutoff.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text(cutoff.isoformat(), encoding="utf-8")

    assert data.load_langsmith_runs("selfheal_framework", root=tmp_path) == []
    assert captured["start_time"] == cutoff

    marker.unlink()
    captured.clear()
    assert data.load_langsmith_runs("selfheal_framework", root=tmp_path) == []
    assert "start_time" not in captured


def test_clear_langsmith_history_hides_loaded_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "project_root", lambda: tmp_path)
    monkeypatch.setattr(data, "langsmith_settings", lambda _root: ("selfheal_framework", None))
    monkeypatch.setattr(data, "load_runs", lambda _root: [])
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)

    def fake_runs(_project, root):
        if data.langsmith_history_cutoff(root) is not None:
            return []
        return [{
            "started_at": "2026-10-11 10:00 IST", "application": "site",
            "name": "Old conversion", "model": "test-model", "status": "completed",
            "total_tokens": 10, "cost_usd": 0, "latency_s": 1,
            "suite_run_id": "run_old", "test_case_id": "TC_001", "trace_url": None,
        }]

    monkeypatch.setattr(data, "load_langsmith_runs", fake_runs)
    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)
    app.radio[0].set_value("LLM Call History").run(timeout=10)

    assert "Old conversion" in app.dataframe[0].value.to_string()
    next(button for button in app.button if button.label == "Clear LLM Call History").click().run(timeout=10)

    assert not app.exception
    assert not app.dataframe
    assert "No LLM calls to show." in [message.value for message in app.info]
    assert data.langsmith_history_cutoff(tmp_path) is not None


def test_langsmith_connection_error_does_not_show_sdk_details(monkeypatch):
    monkeypatch.setattr(data, "langsmith_settings", lambda _root: ("selfheal_framework", None))
    monkeypatch.setattr(data, "load_runs", lambda _root: [])
    monkeypatch.setattr(data, "langsmith_history_cutoff", lambda _root: None)
    monkeypatch.setattr(data, "load_langsmith_runs", lambda _project, root: (
        _ for _ in ()
    ).throw(LangSmithConnectionError("API Key: secret-test-key")))
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)

    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)
    app.radio[0].set_value("LLM Call History").run(timeout=10)

    assert not app.exception
    assert "LangSmith is unreachable" in app.error[0].value
    assert "secret-test-key" not in app.error[0].value


def test_dashboard_renders_without_langsmith_network(monkeypatch):
    monkeypatch.setattr(data, "langsmith_settings", lambda _root: (None, "Not configured for test."))
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)

    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)

    assert not app.exception
    assert app.title[0].value == "SelfHeal Control"
    assert app.sidebar.button[0].label == "Discover and Run"
    assert app.sidebar.text_input[0].label == "Website URL"
    assert app.sidebar.file_uploader[0].label == "Test cases (.xlsx)"
    assert app.sidebar.toggle[0].value is False
    assert not app.sidebar.segmented_control
    assert "Cache Hits" not in [metric.label for metric in app.metric]


def test_cache_view_shows_only_stored_apps_and_no_locator_activity(monkeypatch):
    profiles = [
        {"name": name, "environments": {"prod": f"https://{name}.test"}, "path": Path(f"{name}.yaml")}
        for name in ("site-one", "site-two")
    ]
    monkeypatch.setattr(data, "discover_profiles", lambda _root: profiles)
    monkeypatch.setattr(data, "load_runs", lambda _root: [])
    monkeypatch.setattr(data, "load_cache_entries", lambda _root: [{"app": "site-two"}])
    monkeypatch.setattr(data, "load_healing_events", lambda _root: (_ for _ in ()).throw(
        AssertionError("Cache inventory must not read locator activity")
    ))
    monkeypatch.setattr(data, "langsmith_settings", lambda _root: ("selfheal", None))
    monkeypatch.setattr(data, "load_langsmith_runs", lambda _project, root: [
        {"application": "site-one", "suite_run_id": "run_1", "trace_url": None},
        {"application": "site-two", "suite_run_id": "run_2", "trace_url": None},
    ])
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)

    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)
    app.radio[0].set_value("Cache History").run(timeout=10)
    assert not app.exception
    assert set(app.selectbox[0].options) == {"All applications", "site-two"}
    headings = [heading.value for heading in app.subheader]
    assert "Cached Selectors" in headings
    assert "Locator Activity" not in headings
    assert not app.metric

    app.radio[0].set_value("LLM Call History").run(timeout=10)
    assert not app.exception
    assert not app.selectbox
    trace_rows = app.dataframe[0].value.to_string()
    assert "site-one" in trace_rows
    assert "site-two" in trace_rows


def test_run_details_identifies_cache_use_and_healed_locator(monkeypatch):
    monkeypatch.setattr(data, "load_runs", lambda _root: [{
        "run_id": "run_1", "total": 1, "passed": 1, "llm_calls": 0,
        "cache_hits": 2,
    }])
    monkeypatch.setattr(data, "load_run_details", lambda _run_id, _root: {
        "suite": "suites/converted/site/tests.xlsx", "base_url": "https://example.test",
        "healing": {"ai_calls": 1},
        "tests": [{"id": "TC_001", "title": "Home", "status": "passed",
                   "cache_hits": 2, "cached_selectors": [
                       {"page": "/", "command": "CLICK", "target": "Continue",
                        "selector": "#continue-v1"},
                   ], "steps": [], "checks": []},
                  {"id": "TC_002", "title": "Login link", "status": "passed",
                   "healing_ai_calls": 1, "healed_steps": ["CLICK | Signup / Login"],
                   "recovery_events": [
                       {"event": "locator_failed", "step": "CLICK | Signup / Login",
                        "selector": "#old-login", "reason": "matched no element on the current page"},
                       {"event": "healing_requested", "step": "CLICK | Signup / Login"},
                       {"event": "healed", "step": "CLICK | Signup / Login",
                        "selector": "a[href='/login']"},
                   ],
                   "steps": [{"text": "CLICK | Signup / Login", "status": "passed",
                              "detail": "Clicked Signup / Login (healed)"}], "checks": []},
                  {"id": "TC_003", "title": "Older healed run", "status": "passed",
                   "steps": [{"text": "CLICK | Continue", "status": "passed",
                              "detail": "Clicked Continue (healed)"}], "checks": []}],
    })
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)

    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)
    app.radio[0].set_value("Run Details").run(timeout=10)

    assert not app.exception
    assert any("Cache used: 2 locator hit(s)" in item.value for item in app.caption)
    assert any(item.value == "Cached selectors used" for item in app.caption)
    assert any(item.value == "Healing AI calls: 1" for item in app.caption)
    assert any(item.value == "HEALED: CLICK | Signup / Login" for item in app.caption)
    assert any(item.value == "HEALED: CLICK | Continue" for item in app.caption)
    assert any(item.value.startswith("LOCATOR FAILED: CLICK | Signup / Login") for item in app.caption)
    assert any(item.value.startswith("HEALING: asking OpenAI") for item in app.caption)
    assert sum(item.value == "HEALED: CLICK | Signup / Login" for item in app.caption) == 1
    assert any("#continue-v1" in frame.value.to_string() for frame in app.dataframe)


def test_configured_website_reruns_saved_suite_without_upload(monkeypatch, tmp_path):
    captured = {}

    class FinishedProcess:
        returncode = 0

        def poll(self):
            return 0

    def fake_popen(command, **_kwargs):
        captured["command"] = command
        return FinishedProcess()

    suite = tmp_path / "suites" / "converted" / "saved.xlsx"
    suite.parent.mkdir(parents=True)
    workbook = openpyxl.Workbook()
    workbook.active.append(["TestCase ID", "Title", "Test Step", "Test Data", "Expected Result"])
    workbook.active.append(["TC_001", "Home", "OPEN | /\nASSERT_URL | /", "", ""])
    workbook.save(suite)
    workbook.close()
    profile = tmp_path / "config" / "apps" / "generated" / "site.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("app_name: site\n", encoding="utf-8")
    monkeypatch.setattr(data, "project_root", lambda: tmp_path)
    monkeypatch.setattr(data, "load_runs", lambda _root: [])
    monkeypatch.setattr(data, "configured_sites", lambda _root: [{
        "name": "site", "url": "https://example.test", "profile": profile,
        "suite": suite, "last_run": "run_1", "status": "passed", "passed": 1, "total": 1,
    }])
    monkeypatch.setattr(data, "load_cache_entries", lambda _root, limit=250, app=None: [{
        "app": "site", "page": "/", "target": "generic-click:continue",
        "locator": "#continue-v1", "created_at": "2026-10-12 10:00 IST",
    }] if app == "site" and limit is None else [])
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)
    monkeypatch.setattr(history, "start_dashboard_run", lambda *_args: None)
    monkeypatch.setattr(history, "finish_dashboard_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)
    app.radio[0].set_value("Configured Websites").run(timeout=10)
    assert not app.exception
    assert any("#continue-v1" in frame.value.to_string() for frame in app.dataframe)
    next(button for button in app.button if button.label == "Rerun Saved Suite").click().run(timeout=10)

    assert not app.exception
    assert captured["command"][1:4] == ["-m", "selfheal.cli", "run"]
    assert captured["command"][4] == str(suite)
    assert captured["command"][5:9] == ["--env", "prod", "--config", str(profile)]
    assert "onboard" not in captured["command"]


def test_new_website_upload_launches_onboarding(monkeypatch, tmp_path):
    captured = {}

    class FinishedProcess:
        returncode = 0

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FinishedProcess()

    monkeypatch.setattr(data, "project_root", lambda: tmp_path)
    monkeypatch.setattr(data, "langsmith_settings", lambda _root: (None, "Not configured"))
    monkeypatch.setattr(history, "migrate_legacy_logs", lambda _directory: None)
    monkeypatch.setattr(history, "start_dashboard_run", lambda *_args: None)
    monkeypatch.setattr(history, "finish_dashboard_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "max")
    (tmp_path / ".env").write_text(
        "OPENAI_MODEL=gpt-6-sol\nOPENAI_REASONING_EFFORT=xhigh\n", encoding="utf-8",
    )

    workbook = openpyxl.Workbook()
    workbook.active.append(["TestCase ID", "Title", "Test Step", "Test Data", "Expected Result"])
    workbook.active.append(["TC_001", "Home", "Open home page", "", "Home page is visible"])
    content = BytesIO()
    workbook.save(content)

    app = AppTest.from_file(Path(__file__).parents[1] / "src" / "selfheal" / "dashboard" / "app.py")
    app.run(timeout=10)
    app.sidebar.text_input[0].set_value("https://example.test")
    app.sidebar.file_uploader[0].set_value((
        "site.xlsx", content.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ))
    app.run(timeout=10)
    assert not app.exception
    next(button for button in app.sidebar.button if button.label == "Discover and Run").click().run(timeout=10)

    assert not app.exception
    assert captured["command"][1:4] == ["-m", "selfheal.cli", "onboard"]
    assert captured["command"][4] == "https://example.test"
    assert Path(captured["command"][5]).is_file()
    assert "--review-upload" in captured["command"]
    assert captured["env"]["OPENAI_MODEL"] == "gpt-6-sol"
    assert captured["env"]["OPENAI_REASONING_EFFORT"] == "xhigh"
