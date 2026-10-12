"""Discover and repair browser locators through verified, cache-backed AI."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from selfheal.errors import HealingApprovalRequiredError
from selfheal.llm.openai_client import OpenAIQuotaError
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
    source: Literal["cache", "ai", "healed", "text"]
    cache_record: tuple[str, str, str, str, str] | None = None
    step: str | None = None


def _cached_locator_failure_reason(page: Any, selector: str) -> str:
    try:
        elements = page.locator(selector).all()
        if not elements:
            return "matched no element on the current page"
        visible = sum(element.is_visible() for element in elements)
    except Exception:
        return "could not be evaluated on the current page"
    if visible == 0:
        return "matched no visible element"
    if visible > 1:
        return f"matched {visible} visible elements, not a unique target"
    return "did not match the requested control or expected text"


def compatible_element(element: Any, command: str) -> bool:
    """Reject selectors that point to the wrong kind of browser control."""
    command = command.removeprefix("generic-").upper()
    tag, input_type, role, editable, disabled, onclick = element.evaluate(
        """el => [el.tagName.toLowerCase(), (el.type || '').toLowerCase(),
            el.getAttribute('role'), el.isContentEditable, !!el.disabled,
            !!el.getAttribute('onclick')]"""
    )
    if disabled:
        return False
    if command in {"FILL", "CLEAR", "PRESS"}:
        return (tag == "input" and input_type not in {
            "button", "checkbox", "file", "hidden", "image", "radio", "reset", "submit",
        }) or tag == "textarea" or editable
    if command == "SELECT":
        return tag == "select"
    if command in {"CHECK", "UNCHECK"}:
        return tag == "input" and input_type in {"radio", "checkbox"}
    if command == "UPLOAD":
        return tag == "input" and input_type == "file"
    if command == "CLICK":
        return (
            tag in {"a", "button"}
            or (tag == "input" and input_type in {"button", "submit", "reset", "image"})
            or role == "button" or onclick
        )
    if command in {"ASSERT_VALUE", "ASSERT_VALIDATION"}:
        return tag in {"input", "textarea", "select"}
    return True


def _supports_visible_target(element: Any, command: str, target: str) -> bool:
    if command.removeprefix("generic-").upper() not in {"ASSERT_VISIBLE", "WAIT_VISIBLE"}:
        return True
    words = [
        word for word in re.findall(r"[a-z0-9]+", target.casefold())
        if len(word) >= 3 and word not in {
            "the", "and", "your", "first", "second", "field", "button", "link",
            "heading", "section", "page", "list", "message", "modal", "element",
        }
    ]
    if not words:
        return False
    evidence = element.evaluate(
        """el => [el.tagName, el.id, el.className, el.textContent,
            el.getAttribute('aria-label'), el.getAttribute('title'),
            el.getAttribute('alt'), el.getAttribute('placeholder'),
            el.getAttribute('href')].filter(Boolean).join(' ').toLowerCase()"""
    )
    return all(word in evidence for word in words)


def _normalized_label(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _form_headings(form: Any) -> list[str]:
    return [
        _normalized_label(text)
        for text in form.locator("xpath=parent::*").locator("h1,h2,h3").all_inner_texts()
    ]


def _requested_form_heading(page: Any, target: str) -> str | None:
    normalized = _normalized_label(target)
    matching = {
        heading
        for form in page.locator("form").all()
        for heading in _form_headings(form)
        if len(heading) >= 5 and heading in normalized
    }
    if not matching:
        return None
    longest = max(map(len, matching))
    candidates = [heading for heading in matching if len(heading) == longest]
    return candidates[0] if len(candidates) == 1 else None


def _click_label(element: Any) -> str:
    return _normalized_label(element.evaluate(
        "el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()"
    ))


def verified_locator(
    page: Any, selector: str, command: str, target: str,
    *, expected_text: str | None = None, allow_scope: bool = False,
) -> tuple[Any, str] | None:
    """Accept only a unique, visible, command-compatible element."""
    try:
        form_heading = _requested_form_heading(page, target)
        command_name = command.removeprefix("generic-").upper()
        exact_click_exists = False
        if command_name == "CLICK":
            label = _normalized_label(target)
            exact_click_exists = bool(label) and any(
                item.is_visible() and compatible_element(item, command)
                and _click_label(item) == label
                for item in page.locator("a,button,input,[role='button']").all()
            )
        matches = [
            item for item in page.locator(selector).all()
            if item.is_visible() and compatible_element(item, command)
            and _supports_visible_target(item, command, target)
            and (expected_text is None or expected_text in item.inner_text())
            and (not form_heading or (
                item.locator("xpath=ancestor::form[1]").count() == 1
                and form_heading in _form_headings(item.locator("xpath=ancestor::form[1]"))
            ))
            and (not exact_click_exists or _click_label(item) == _normalized_label(target))
        ]
        all_visible = [item for item in page.locator(selector).all() if item.is_visible()]
    except Exception:
        return None
    if len(matches) == len(all_visible) == 1:
        return matches[0], selector
    if not allow_scope or selector.startswith(("//", "xpath=")):
        return None

    if form_heading and len(matches) == 1 and len(all_visible) > 1:
        item = matches[0]
        form = item.locator("xpath=ancestor::form[1]")
        if form.count() == 1:
            form_id = form.get_attribute("id")
            action = form.get_attribute("action")
            prefix = (
                f"form[id={json.dumps(form_id)}]" if form_id else
                f"form[action={json.dumps(action)}]" if action else None
            )
            if prefix:
                candidate = f"{prefix} {selector}"
                try:
                    visible = [x for x in page.locator(candidate).all() if x.is_visible()]
                    if len(visible) == 1:
                        return item, candidate
                except Exception:
                    return None
    if command_name == "CLICK" and len(all_visible) > 1:
        candidate = f"{selector}:has-text({json.dumps(target)})"
        try:
            visible = [x for x in page.locator(candidate).all() if x.is_visible()]
            if len(visible) == 1 and compatible_element(visible[0], command):
                return visible[0], candidate
        except Exception:
            return None
    if expected_text and len(matches) == 1:
        candidate = f"{selector}:has-text({json.dumps(expected_text)})"
        try:
            visible = [x for x in page.locator(candidate).all() if x.is_visible()]
            if len(visible) == 1 and expected_text in visible[0].inner_text():
                return visible[0], candidate
        except Exception:
            return None
    return None


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
        self.cache_uses: list[dict[str, str]] = []
        self._quota_error: OpenAIQuotaError | None = None
        self._run_id: str | None = None
        self._test_case_id: str | None = None
        self.recovery_events: list[dict[str, str]] = []
        self.progress_callback: Callable[[dict[str, str]], None] | None = None

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

    def resolve(
        self, page: Any, target: str, *, kind: str, allow_ai: bool = True,
        expected_text: str | None = None,
    ) -> HealingMatch | None:
        """Use a verified cache entry, or ask AI to identify one visible control."""
        resolver_settings = self.config.get("resolver", {})
        healing_settings = self.config.get("healing", {})
        if not resolver_settings.get("ai_enabled") or not healing_settings.get("enabled"):
            return None

        app = self.config.get("app_name", "app")
        page_key = urlparse(page.url).path or "/"
        cache_target = f"{kind}:{target.casefold()}"
        step = f"{kind.removeprefix('generic-').upper()} | {target}"
        cached_locator = self.cache.get(app, self.environment, page_key, cache_target)
        stale_cache = False
        if cached_locator:
            cached = verified_locator(
                page, cached_locator, kind, target, expected_text=expected_text,
            )
            if cached is not None:
                self.cache_hits += 1
                self.cache_uses.append({
                    "page": page_key,
                    "command": kind.removeprefix("generic-").upper(),
                    "target": target,
                    "selector": cached_locator,
                })
                self._record_event(app, page_key, target, "cache_hit", cached_locator)
                return HealingMatch(cached[0], "cache")
            stale_cache = True
            if allow_ai:
                self._report_progress(
                    "locator_failed", step, cached_locator,
                    _cached_locator_failure_reason(page, cached_locator),
                )
                self._record_event(app, page_key, target, "cache_stale", cached_locator)

        text_target = expected_text
        if text_target is None and kind.removeprefix("generic-").upper() == "WAIT_VISIBLE":
            text_target = target
        if text_target:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            if allow_ai:
                try:
                    page.get_by_text(re.compile(re.escape(text_target), re.I)).first.wait_for(
                        state="visible", timeout=5000,
                    )
                except PlaywrightTimeoutError:
                    pass
            pattern = re.escape(text_target).replace("/", r"\/")
            text_selector = f"text=/^{pattern}$/i"
            exact = verified_locator(
                page, text_selector, kind, target, expected_text=expected_text,
            )
            if exact is not None:
                self._record_event(app, page_key, target, "text_match", None)
                return HealingMatch(exact[0], "text")

        if not allow_ai:
            return None

        if self._quota_error is not None:
            raise self._quota_error

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
        if stale_cache:
            self._report_progress("healing_requested", step)
        try:
            suggestion = self._get_client(api_key).identify_element(
                self._page_tree(page),
                f"{kind} element: {target}" + (
                    f"; visible text must contain {expected_text!r}" if expected_text else ""
                ),
                confidence_threshold=threshold,
            )
        except OpenAIQuotaError as exc:
            self._quota_error = exc
            self._record_event(app, page_key, target, "ai_quota_exhausted", None)
            raise
        if not suggestion.get("found") or suggestion.get("confidence", 0) < threshold:
            return None

        selector = suggestion.get("locator")
        if not isinstance(selector, str) or not selector.strip():
            return None
        verified = verified_locator(
            page, selector, kind, target, expected_text=expected_text, allow_scope=True,
        )
        if verified is None:
            self._record_event(app, page_key, target, "ai_invalid_selector", selector)
            return None
        element, selector = verified

        if not healing_settings.get("auto_approve", False):
            self._record_event(app, page_key, target, "ai_review_required", selector)
            raise HealingApprovalRequiredError(
                f"AI suggested {selector!r} for {target}; healing.auto_approve is false"
            )

        return HealingMatch(
            element,
            "healed" if stale_cache else "ai",
            (app, self.environment, page_key, cache_target, selector),
            step,
        )

    def remember_success(self, match: HealingMatch) -> None:
        """Persist a new selector only after its browser action succeeds."""
        if match.cache_record is not None:
            self.cache.put(*match.cache_record)
            app, _, page, target, selector = match.cache_record
            self._record_event(app, page, target.split(":", 1)[-1], "ai_healed", selector)
            if match.source == "healed" and match.step:
                self._report_progress("healed", match.step, selector)

    def _report_progress(
        self, event_type: str, step: str, selector: str | None = None,
        reason: str | None = None,
    ) -> None:
        event = {"event": event_type, "step": step}
        if selector is not None:
            event["selector"] = selector
        if reason is not None:
            event["reason"] = reason
        self.recovery_events.append(event)
        if self.progress_callback is not None:
            self.progress_callback(event)

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
            from selfheal.llm.openai_client import DEFAULT_OPENAI_MODEL, OpenAILLMClient

            self.client = OpenAILLMClient(
                api_key,
                os.getenv("OPENAI_MODEL") or self.config.get("llm", {}).get("model", DEFAULT_OPENAI_MODEL),
                langsmith=self.langsmith,
            )
        return self.client

    @staticmethod
    def _page_tree(page: Any) -> str:
        elements = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                'a, button, input, select, textarea, h1, h2, h3, [role="button"], [role="alert"], [role="status"]'
            )).filter(el => el.getClientRects().length).slice(0, 300).map(el => {
                const form = el.closest('form');
                const heading = form?.parentElement?.querySelector('h1,h2,h3');
                return {
                    tag: el.tagName.toLowerCase(),
                    text: (el.textContent || '').trim().slice(0, 100),
                    id: el.id, className: el.className, href: el.getAttribute('href'),
                    name: el.getAttribute('name'),
                    role: el.getAttribute('role'), type: el.getAttribute('type'),
                    aria: el.getAttribute('aria-label'),
                    placeholder: el.getAttribute('placeholder'),
                    label: Array.from(el.labels || []).map(label => label.textContent.trim()).join(' '),
                    formId: form?.id, formClass: form?.className,
                    formAction: form?.getAttribute('action'),
                    formHeading: heading?.textContent.trim().slice(0, 100)
                };
            })"""
        )
        tree = json.dumps(elements, ensure_ascii=True)
        tree = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "[email]", tree)
        return re.sub(r"\b\d{12,19}\b", "[number]", tree)
