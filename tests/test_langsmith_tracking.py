"""LangSmith tracing tests that never contact OpenAI or LangSmith."""

import json
from contextlib import contextmanager, nullcontext

import httpx2
import langsmith
import langsmith.wrappers
import pytest
import yaml
from langsmith import Client, trace, tracing_context
from openai import OpenAI

import selfheal.cli as cli
import selfheal.llm.openai_client as openai_module
from selfheal.llm.openai_client import OpenAILLMClient
from selfheal.telemetry.langsmith import LangSmithTracker, _usage_only


def test_tracking_is_off_without_opt_in(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    tracker = LangSmithTracker()
    raw_client = object()

    assert not tracker.enabled
    assert tracker.wrap_openai(raw_client) is raw_client
    with tracker.case_context("run-1", "TC_001", "prod"):
        pass
    tracker.flush()


def test_enabled_tracking_requires_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    with pytest.raises(ValueError, match="LANGSMITH_API_KEY"):
        LangSmithTracker()


def test_tracker_wraps_client_tags_case_and_flushes(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "local-test-key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "qa-healing")
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_options"] = kwargs

        def flush(self, timeout=None):
            captured["flush_timeout"] = timeout

    @contextmanager
    def fake_context(**kwargs):
        captured["context"] = kwargs
        yield

    def fake_wrap(client, **kwargs):
        captured["wrapped"] = client
        captured["wrapper_options"] = kwargs
        return "wrapped-client"

    monkeypatch.setattr(langsmith, "Client", FakeClient)
    monkeypatch.setattr(langsmith, "tracing_context", fake_context)
    monkeypatch.setattr(langsmith.wrappers, "wrap_openai", fake_wrap)

    tracker = LangSmithTracker()
    raw_client = object()
    assert tracker.wrap_openai(raw_client) == "wrapped-client"
    with tracker.case_context("run-1", "TC_004", "prod"):
        pass
    tracker.flush()

    assert captured["wrapped"] is raw_client
    assert captured["wrapper_options"]["tracing_extra"]["client"] is tracker._client
    assert captured["client_options"]["hide_inputs"] is True
    assert captured["client_options"]["hide_outputs"] is _usage_only
    assert captured["context"]["project_name"] == "qa-healing"
    assert captured["context"]["metadata"] == {
        "suite_run_id": "run-1", "test_case_id": "TC_004", "environment": "prod"
    }
    assert captured["flush_timeout"] == 30
    assert _usage_only({"choices": [{"message": {"content": "private"}}],
                        "usage_metadata": {"total_tokens": 17}}) == {
        "usage_metadata": {"total_tokens": 17}
    }

    captured["client_options"]["tracing_error_callback"](RuntimeError("upload failed"))
    with pytest.raises(RuntimeError, match="could not upload"):
        tracker.flush()


def test_real_wrapper_keeps_token_usage_and_case_metadata_offline(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "dummy-local-key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "qa-healing")
    sdk_client = Client(api_key="dummy-local-key", info={},
                        hide_inputs=True, hide_outputs=_usage_only)
    monkeypatch.setattr(langsmith, "Client", lambda **kwargs: sdk_client)

    payload = {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
        "model": "gpt-3.5-turbo",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({
            "found": False, "locator": None, "confidence": 0
        })}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
    }
    requests = []

    def mock_openai(request):
        requests.append(request)
        return httpx2.Response(200, json=payload)

    raw_client = OpenAI(
        api_key="dummy-openai-key",
        http_client=httpx2.Client(transport=httpx2.MockTransport(mock_openai)),
    )
    monkeypatch.setattr(openai_module, "OpenAI", lambda **kwargs: raw_client)
    tracker = LangSmithTracker()
    llm = OpenAILLMClient("dummy-openai-key", langsmith=tracker)

    with tracing_context(enabled="local", client=sdk_client):
        with tracker.case_context("run-local", "TC_020", "prod"):
            with trace("offline-test", run_type="chain") as parent:
                answer = llm.identify_element("[]", "Unknown control")
                children = parent.child_runs

    assert len(requests) == 1
    assert answer["total_tokens"] == 17
    assert len(children) == 1
    child = children[0]
    assert child.run_type == "llm"
    assert child.extra["metadata"]["test_case_id"] == "TC_020"
    assert child.extra["metadata"]["suite_run_id"] == "run-local"
    assert child.extra["metadata"]["usage_metadata"]["total_tokens"] == 17


@pytest.mark.parametrize("upload_fails", [False, True])
def test_browser_runner_tags_case_and_reports_trace_upload_failure(
    monkeypatch, tmp_path, upload_fails
):
    page = tmp_path / "page.html"
    page.write_text("<h1>Ready</h1>", encoding="utf-8")
    config = tmp_path / "app.yaml"
    config.write_text(yaml.safe_dump({
        "app_name": "offline",
        "environments": {"prod": {"base_url": page.as_uri(), "account": {}}},
        "execution": {"browser": "chromium", "timeout_seconds": 5,
                      "screenshot_on_pass": False, "screenshot_on_error": False},
        "resolver": {"ai_enabled": False},
        "healing": {"enabled": False},
    }), encoding="utf-8")
    calls = []

    class SpyTracker:
        enabled = True
        project = "qa-healing"

        def case_context(self, run_id, test_case_id, environment):
            calls.append((run_id, test_case_id, environment))
            return nullcontext()

        def flush(self):
            calls.append("flush")
            if upload_fails:
                raise RuntimeError("upload failed")

    monkeypatch.setattr(cli, "LangSmithTracker", SpyTracker)
    run_dir = cli.run_browser_suite(
        [{"id": "TC_OFFLINE", "title": "Observe page", "step": "Observe the page",
          "expected": "'Ready' is displayed"}],
        page, config_path=config, output_root=tmp_path / "runs", headless=True,
        history_path=tmp_path / "run_history.xlsx",
    )
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))

    assert results["tests"][0]["status"] == "passed"
    assert results["langsmith_tracing"] == {"enabled": True, "project": "qa-healing"}
    assert calls == [(results["run_id"], "TC_OFFLINE", "prod"), "flush"]
    assert results["status"] == ("failed" if upload_fails else "passed")
    assert ("langsmith_error" in results) == upload_fails
