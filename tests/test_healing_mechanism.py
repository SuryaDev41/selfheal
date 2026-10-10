"""Offline browser integration test for the selector-healing lifecycle."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright

from selfheal.executor.browser import BrowserExecutor
from selfheal.healing.cache import LocatorCache
from selfheal.llm.openai_client import OpenAILLMClient
from selfheal.telemetry.healing_log import HealingEventStore

CASE = {
    "step": "Click 'Continue'",
    "expected": "'Done' is displayed",
}


class SuggestedLocatorClient:
    """A controlled stand-in for the external LLM used by this offline test."""

    def __init__(self, locator: str):
        self.locator = locator
        self.calls: list[tuple[str, str, float]] = []

    def identify_element(self, page_tree: str, target: str, confidence_threshold: float):
        self.calls.append((page_tree, target, confidence_threshold))
        return {"found": True, "confidence": 0.99, "locator": self.locator}


class FailIfCalledClient:
    """Makes a cache hit fail loudly if it unexpectedly attempts an LLM call."""

    def identify_element(self, *_args, **_kwargs):
        raise AssertionError("A valid cached locator must not call the LLM")


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True, channel="chromium")
        yield instance
        instance.close()


def _config(url: str) -> dict:
    return {
        "app_name": "healing-fixture",
        "environments": {"prod": {"base_url": url, "account": {}}},
        "execution": {"browser": "chromium", "timeout_seconds": 1},
        "resolver": {"ai_enabled": True, "confidence_threshold": 0.85},
        "healing": {"enabled": True, "auto_approve": True, "max_heals_per_run": 5},
    }


def _write_page(path: Path, button_id: str) -> None:
    path.write_text(
        "<!doctype html><html><body>"
        f"<button id='{button_id}' onclick=\"document.querySelector('#result').textContent='Done'\">"
        "Go</button><p id='result'></p>"
        "</body></html>",
        encoding="utf-8",
    )


def _run_case(browser, executor: BrowserExecutor) -> dict:
    context = browser.new_context()
    try:
        return executor.run_case(context.new_page(), CASE)
    finally:
        context.close()


def test_healing_lifecycle_reuses_cache_and_recovers_stale_locator(browser, tmp_path, monkeypatch):
    """Heal an unknown control, reuse it, then repair it after a selector change."""
    page_file = tmp_path / "healing.html"
    cache = LocatorCache(tmp_path / "healing-cache.db")
    config = _config(page_file.as_uri())

    _write_page(page_file, "continue-v1")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    initial_client = SuggestedLocatorClient("#continue-v1")
    initial_executor = BrowserExecutor(config, "prod", page_file)
    initial_executor.healer.cache = cache
    initial_executor.healer.client = initial_client

    initial = _run_case(browser, initial_executor)
    assert initial["status"] == "passed", initial
    assert "(ai)" in initial["steps"][0]["detail"]
    assert len(initial_client.calls) == 1

    page_key = urlparse(page_file.as_uri()).path
    assert cache.get("healing-fixture", "prod", page_key, "click:continue") == "#continue-v1"

    monkeypatch.delenv("OPENAI_API_KEY")
    cached_executor = BrowserExecutor(config, "prod", page_file)
    cached_executor.healer.cache = cache
    cached_executor.healer.client = FailIfCalledClient()

    cached = _run_case(browser, cached_executor)
    assert cached["status"] == "passed", cached
    assert "(cache)" in cached["steps"][0]["detail"]

    _write_page(page_file, "continue-v2")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    healed_client = SuggestedLocatorClient("#continue-v2")
    healed_executor = BrowserExecutor(config, "prod", page_file)
    healed_executor.healer.cache = cache
    healed_executor.healer.client = healed_client

    healed = _run_case(browser, healed_executor)
    assert healed["status"] == "passed", healed
    assert "(healed)" in healed["steps"][0]["detail"]
    assert len(healed_client.calls) == 1
    assert cache.get("healing-fixture", "prod", page_key, "click:continue") == "#continue-v2"


def test_failed_healed_click_is_not_cached(browser, tmp_path, monkeypatch):
    """Do not save a selector until the action using it has actually completed."""
    page_file = tmp_path / "disabled.html"
    page_file.write_text(
        "<button id='continue' disabled>Go</button><p id='result'></p>",
        encoding="utf-8",
    )
    config = _config(page_file.as_uri())
    config["execution"]["timeout_seconds"] = 0.2
    cache = LocatorCache(tmp_path / "failed-click-cache.db")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")

    executor = BrowserExecutor(config, "prod", page_file)
    executor.healer.cache = cache
    executor.healer.client = SuggestedLocatorClient("#continue")

    result = _run_case(browser, executor)
    page_key = urlparse(page_file.as_uri()).path

    assert result["status"] == "failed", result
    assert cache.get("healing-fixture", "prod", page_key, "click:continue") is None


def test_locator_prompt_requires_playwright_compatible_css():
    prompt = OpenAILLMClient._build_prompt("[]", "generic element: Login link", 0.85)

    assert "page.locator()" in prompt
    assert "[text='...']" in prompt


def test_generic_ai_first_discovers_reuses_and_repairs(browser, tmp_path, monkeypatch):
    page_file = tmp_path / "generic.html"
    cache = LocatorCache(tmp_path / "generic-cache.db")
    events = HealingEventStore(tmp_path / "events.db")
    config = _config(page_file.as_uri())
    config["resolver"]["discovery_mode"] = "ai_first"
    config["locators"] = {"Continue": "css=#continue-v1", "result": "css=#result"}
    case = {"step": "CLICK | Continue\nASSERT_TEXT | result | Done", "expected": ""}
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")

    def run(client):
        executor = BrowserExecutor(config, "prod", page_file)
        executor.healer.cache = cache
        executor.healer.event_store = events
        executor.healer.client = client
        context = browser.new_context()
        try:
            result = executor.run_case(context.new_page(), case)
        finally:
            context.close()
        return result, executor.healer.stats()

    _write_page(page_file, "continue-v1")
    first_client = SuggestedLocatorClient("#continue-v1")
    first, first_stats = run(first_client)
    assert first["status"] == "passed", first
    assert "(ai)" in first["steps"][0]["detail"]
    assert first_stats["ai_calls"] == 1
    assert len(first_client.calls) == 1
    page_key = urlparse(page_file.as_uri()).path
    cache_key = "generic-click:continue"
    assert cache.get("healing-fixture", "prod", page_key, cache_key) == "#continue-v1"

    monkeypatch.delenv("OPENAI_API_KEY")
    second, second_stats = run(FailIfCalledClient())
    assert second["status"] == "passed", second
    assert "(cache)" in second["steps"][0]["detail"]
    assert second_stats == {"ai_calls": 0, "cache_hits": 1, "resolution_attempts": 1}

    _write_page(page_file, "continue-v2")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    repair_client = SuggestedLocatorClient("#continue-v2")
    third, third_stats = run(repair_client)
    assert third["status"] == "passed", third
    assert "(healed)" in third["steps"][0]["detail"]
    assert third_stats["ai_calls"] == 1
    assert len(repair_client.calls) == 1
    assert cache.get("healing-fixture", "prod", page_key, cache_key) == "#continue-v2"

    import sqlite3

    with sqlite3.connect(tmp_path / "events.db") as connection:
        event_types = [row[0] for row in connection.execute("SELECT event_type FROM healing_events")]
    assert event_types == ["ai_healed", "cache_hit", "cache_stale", "ai_healed"]


def test_generic_ai_first_does_not_heal_a_failed_assertion(browser, tmp_path, monkeypatch):
    page_file = tmp_path / "assertion.html"
    page_file.write_text("<p id='result'>Wrong result</p>", encoding="utf-8")
    config = _config(page_file.as_uri())
    config["resolver"]["discovery_mode"] = "ai_first"
    config["locators"] = {"result": "css=#result"}
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    executor = BrowserExecutor(config, "prod", page_file)
    executor.healer.cache = LocatorCache(tmp_path / "assertion-cache.db")
    executor.healer.client = FailIfCalledClient()
    case = {"step": "ASSERT_TEXT | result | Done", "expected": ""}
    context = browser.new_context()
    try:
        result = executor.run_case(context.new_page(), case)
    finally:
        context.close()
    assert result["status"] == "failed", result
    assert executor.healer.ai_calls == 0
