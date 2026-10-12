"""Verify model-specific request options without spending API credits."""

from types import SimpleNamespace

import httpx2 as httpx
import pytest
from openai import BadRequestError

from selfheal.llm.openai_client import OpenAILLMClient
from selfheal.llm.openai_pricing import estimate_cost_usd


class NoTrace:
    def wrap_openai(self, client):
        return client


def _bad_request(*, code="unsupported_value", param="reasoning_effort", message="Supported values are: 'none', 'low', 'medium', 'high', and 'xhigh'."):
    return BadRequestError(
        message,
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
        body={"error": {"code": code, "param": param, "message": message}},
    )


def test_gpt6_sol_uses_max_reasoning_for_conversion_discovery_and_healing(monkeypatch):
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            format_name = kwargs.get("response_format", {}).get("json_schema", {}).get("name")
            if format_name == "converted_browser_cases":
                content = '{"cases": []}'
            elif format_name == "site_locator_discovery":
                content = '{"site_name": "Fixture", "locators": []}'
            else:
                content = '{"found": false, "locator": null, "confidence": 0}'
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=content, refusal=None),
                )],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            )

    class StubOpenAI:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "max")
    monkeypatch.setattr("selfheal.llm.openai_client.OpenAI", StubOpenAI)
    client = OpenAILLMClient("offline-key", model="gpt-6-sol", langsmith=NoTrace())

    assert client.convert_site([], []) == {"cases": []}
    assert client.discover_site([], []) == {"site_name": "Fixture", "locators": []}
    assert client.identify_element("[]", "Login")["found"] is False

    assert len(calls) == 3
    for call in calls:
        assert call["model"] == "gpt-6-sol"
        assert call["reasoning_effort"] == "max"
        assert call["max_completion_tokens"] > 0
        assert "max_tokens" not in call
        assert "temperature" not in call
        assert "service_tier" not in call
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert "uploaded workbook is read-only" in calls[0]["messages"][0]["content"]
    assert calls[1]["response_format"]["type"] == "json_schema"


def test_legacy_chat_model_keeps_existing_sampling_options(monkeypatch):
    class StubOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: None))

    monkeypatch.setattr("selfheal.llm.openai_client.OpenAI", StubOpenAI)
    client = OpenAILLMClient("offline-key", model="gpt-4o-mini", langsmith=NoTrace())
    assert client._generation_options(500, 0.2) == {"max_tokens": 500, "temperature": 0.2}
    assert estimate_cost_usd("gpt-6-sol", 1_000_000, 1_000_000) == 12.0


def test_max_rejection_falls_back_once_and_reuses_xhigh_for_all_workflows(monkeypatch):
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise _bad_request()
            format_name = kwargs.get("response_format", {}).get("json_schema", {}).get("name")
            content = {
                "converted_browser_cases": '{"cases": []}',
                "site_locator_discovery": '{"site_name": "Fixture", "locators": []}',
            }.get(format_name, '{"found": false, "locator": null, "confidence": 0}')
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=content, refusal=None),
                )],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            )

    class StubOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "max")
    monkeypatch.setattr("selfheal.llm.openai_client.OpenAI", StubOpenAI)
    client = OpenAILLMClient("offline-key", model="gpt-6-sol", langsmith=NoTrace())

    assert client.convert_site([], []) == {"cases": []}
    assert client.discover_site([], []) == {"site_name": "Fixture", "locators": []}
    assert client.identify_element("[]", "Login")["found"] is False

    assert [call["reasoning_effort"] for call in calls] == ["max", "xhigh", "xhigh", "xhigh"]
    assert all(call["model"] == "gpt-6-sol" for call in calls)
    assert client.reasoning_effort == "xhigh"


def test_unrelated_bad_request_is_not_retried(monkeypatch):
    calls = []

    class StubOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            calls.append(kwargs)
            raise _bad_request(code="invalid_request_error", param="response_format")

    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "max")
    monkeypatch.setattr("selfheal.llm.openai_client.OpenAI", StubOpenAI)
    client = OpenAILLMClient("offline-key", model="gpt-6-sol", langsmith=NoTrace())

    with pytest.raises(BadRequestError):
        client.convert_site([], [])
    assert len(calls) == 1
