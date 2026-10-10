"""Discover and repair browser locators through verified, cache-backed AI."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from selfheal.errors import HealingApprovalRequiredError
from selfheal.telemetry.healing_log import HealingEventStore
from selfheal.telemetry.langsmith import LangSmithTracker

from .cache import DEFAULT_CACHE_PATH, LocatorCache


class LocatorClient(Protocol):
    """The narrow LLM interface required by the healing service."""

    def identify_element(
        self,
        page_tree: str,
        target: str,
        confidence_threshold: float = 0.85,
    ) -> dict[str, Any]:
        """Return a candidate CSS or XPath locator with a confidence score."""


@dataclass(frozen=True)
class HealingMatch:
    """A visible element selected by cache or a verified AI suggestion."""

    element: Any
    source: Literal["cache", "ai", "healed"]
    cache_record: tuple[str, str, str, str, str] | None = None


class LocatorHealer:
    """Own the cache-to-AI selector recovery lifecycle for a test run."""

    def __init__(
        self,
        config: dict[str, Any],
        environment: str,
        *,
        langsmith: LangSmithTracker,
        cache: LocatorCache | None = None,
        client: LocatorClient | None = None,
        event_store: HealingEventStore | None = None,
    ) -> None:
        self.config = config
        self.environment = environment
        healing_settings = config.get("healing", {})
        self.cache_path = healing_settings.get("cache_path") or DEFAULT_CACHE_PATH
        self._cache = cache
        self.client = client
        self.event_store = event_store
        self.langsmith = langsmith
        self.ai_calls = 0
        self.cache_hits = 0
        self._run_id: str | None = None
        self._test_case_id: str | None = None

    def set_context(self, run_id: str, test_case_id: str) -> None:
        """Attach subsequent recovery events to the active framework test case."""
        self._run_id = run_id
        self._test_case_id = test_case_id

    def stats(self) -> dict[str, int]:
        """Return aggregate activity for the current suite run."""
        attempts = self.ai_calls + self.cache_hits
        return {
            "ai_calls": self.ai_calls,
            "cache_hits": self.cache_hits,
            "resolution_attempts": attempts,
        }

    @property
    def cache(self) -> LocatorCache:
        """Create the SQLite cache only when healing is actually needed."""
        if self._cache is None:
            self._cache = LocatorCache(self.cache_path)
        return self._cache

    @cache.setter
    def cache(self, value: LocatorCache) -> None:
        self._cache = value

    def resolve(self, page: Any, target: str, *, kind: str) -> HealingMatch | None:
        """Use a verified cache entry, or ask AI to identify one visible control."""
        resolver_settings = self.config.get("resolver", {})
        healing_settings = self.config.get("healing", {})
        if not resolver_settings.get("ai_enabled") or not healing_settings.get("enabled"):
            return None

        app = self.config.get("app_name", "app")
        page_key = urlparse(page.url).path or "/"
        cache_target = f"{kind}:{target.casefold()}"
        cached_locator = self.cache.get(app, self.environment, page_key, cache_target)
        stale_cache = False
        if cached_locator:
            cached_element = self._visible_element(page, cached_locator)
            if cached_element is not None:
                self.cache_hits += 1
                self._record_event(app, page_key, target, "cache_hit", cached_locator)
                return HealingMatch(cached_element, "cache")
            stale_cache = True
            self._record_event(app, page_key, target, "cache_stale", cached_locator)

        api_key = os.getenv("OPENAI_API_KEY")
        max_calls = int(healing_settings.get("max_heals_per_run", 50))
        if not api_key or self.ai_calls >= max_calls:
            return None

        threshold = float(
            healing_settings.get(
                "confidence_threshold", resolver_settings.get("confidence_threshold", 0.85)
            )
        )
        self.ai_calls += 1
        suggestion = self._get_client(api_key).identify_element(
            self._page_tree(page),
            f"{kind} element: {target}",
            confidence_threshold=threshold,
        )
        if not suggestion.get("found") or suggestion.get("confidence", 0) < threshold:
            return None

        selector = suggestion.get("locator")
        if not isinstance(selector, str) or not selector.strip():
            return None
        element = self._visible_element(page, selector)
        if element is None:
            self._record_event(app, page_key, target, "ai_invalid_selector", selector)
            return None

        if not healing_settings.get("auto_approve", False):
            self._record_event(app, page_key, target, "ai_review_required", selector)
            raise HealingApprovalRequiredError(
                f"AI suggested {selector!r} for {target}; healing.auto_approve is false"
            )

        return HealingMatch(
            element,
            "healed" if stale_cache else "ai",
            (app, self.environment, page_key, cache_target, selector),
        )

    def remember_success(self, match: HealingMatch) -> None:
        """Persist a new selector only after its browser action succeeds."""
        if match.cache_record is not None:
            self.cache.put(*match.cache_record)
            app, _, page, target, selector = match.cache_record
            self._record_event(app, page, target.split(":", 1)[-1], "ai_healed", selector)

    def _record_event(
        self,
        app: str,
        page: str,
        target: str,
        event_type: str,
        selector: str | None,
    ) -> None:
        if self.event_store is None:
            self.event_store = HealingEventStore()
        self.event_store.record(
            run_id=self._run_id,
            test_case_id=self._test_case_id,
            app=app,
            environment=self.environment,
            page=page,
            target=target,
            event_type=event_type,
            selector=selector,
        )

    def _get_client(self, api_key: str) -> LocatorClient:
        if self.client is None:
            from selfheal.llm.openai_client import OpenAILLMClient

            self.client = OpenAILLMClient(
                api_key,
                self.config.get("llm", {}).get("model", "gpt-3.5-turbo"),
                langsmith=self.langsmith,
            )
        return self.client

    @staticmethod
    def _visible_element(page: Any, selector: str) -> Any | None:
        try:
            matches = [item for item in page.locator(selector).all() if item.is_visible()]
        except Exception:
            return None
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _page_tree(page: Any) -> str:
        elements = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                'a, button, input, select, textarea, [role="button"]'
            )).filter(el => el.getClientRects().length).slice(0, 250).map(el => ({
                tag: el.tagName.toLowerCase(),
                text: (el.textContent || '').trim().slice(0, 100),
                id: el.id, className: el.className, href: el.getAttribute('href'),
                name: el.getAttribute('name'),
                role: el.getAttribute('role'), type: el.getAttribute('type'),
                aria: el.getAttribute('aria-label'),
                placeholder: el.getAttribute('placeholder')
            }))"""
        )
        tree = json.dumps(elements, ensure_ascii=True)
        return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "[email]", tree)
