"""Configuration-driven Playwright actions for arbitrary browser applications."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from selfheal.errors import ActionFailedError, UnsupportedStepError

COMMANDS = frozenset(
    {
        "OPEN", "CLICK", "FILL", "CLEAR", "SELECT", "CHECK", "UNCHECK", "PRESS", "HOVER",
        "UPLOAD", "WAIT_VISIBLE", "WAIT_HIDDEN", "ASSERT_VISIBLE", "ASSERT_HIDDEN", "ASSERT_TEXT",
        "ASSERT_VALUE", "ASSERT_VALIDATION", "ASSERT_URL", "ASSERT_URL_CONTAINS",
        "ASSERT_URL_NOT_CONTAINS", "ASSERT_TITLE",
        "ASSERT_COUNT", "ASSERT_EACH_TEXT_CONTAINS", "ASSERT_COUNT_MATCHES", "ASSERT_NUMBER_EQUALS",
        "ASSERT_MULTIPLY", "ASSERT_DATA_FIELDS", "ASSERT_LOAD_TIME",
        "WATCH_DIALOG", "ASSERT_DIALOG",
    }
)
ACTION_COMMANDS = frozenset(
    {"CLICK", "FILL", "CLEAR", "SELECT", "CHECK", "UNCHECK", "PRESS", "HOVER", "UPLOAD"}
)
CACHEABLE_COMMANDS = ACTION_COMMANDS | {
    "ASSERT_VISIBLE", "ASSERT_TEXT", "ASSERT_VALUE", "ASSERT_VALIDATION", "WAIT_VISIBLE",
}
DIRECT_CACHEABLE_COMMANDS = ACTION_COMMANDS | {
    "ASSERT_TEXT", "ASSERT_VALUE", "ASSERT_VALIDATION",
}
VARIABLE = re.compile(r"\$\{([^}]+)}")


class GenericStepExecutor:
    """Execute explicit spreadsheet commands using locators supplied by YAML."""

    def __init__(
        self,
        config: dict[str, Any],
        env: str,
        suite_path: Path,
        timeout_ms: int,
        healer: Any | None = None,
    ) -> None:
        self.config = config
        self.env = env
        self.suite_path = suite_path
        self.timeout_ms = timeout_ms
        self.base_url = str(config["environments"][env]["base_url"])
        self.healer = healer
        self.assertion_count = 0
        self.last_navigation_ms: float | None = None
        self.last_dialog: str | None = None
        self._pending_direct_locators: dict[tuple[str, str], str] = {}
        self._current_direct_locator: tuple[str, str, str] | None = None

    def reset(self) -> None:
        self.assertion_count = 0
        self.last_navigation_ms = None
        self.last_dialog = None
        self.discard_pending_locators()

    def commit_passed_locators(self) -> None:
        """Persist direct selectors only after every step and assertion passes."""
        if (
            self.healer is not None
            and self.config.get("resolver", {}).get("ai_enabled")
            and self.config.get("healing", {}).get("enabled")
            and self._pending_direct_locators
        ):
            self.healer.cache.put_missing(
                self.config.get("app_name", "app"), self.env,
                [
                    (page, target, selector)
                    for (page, target), selector in self._pending_direct_locators.items()
                ],
            )
        self.discard_pending_locators()

    def discard_pending_locators(self) -> None:
        self._pending_direct_locators.clear()
        self._current_direct_locator = None

    @staticmethod
    def is_generic_step(step: str) -> bool:
        command, separator, _ = step.partition("|")
        return bool(separator) and command.strip().upper() in COMMANDS

    def execute(self, page, step: str, data: dict[str, str]) -> str:
        self._current_direct_locator = None
        command, arguments = self._parse_step(step)
        values = [self._interpolate(value, data) for value in arguments]
        if command == "OPEN":
            self._require_arguments(command, values, 1)
            destination = self._url(values[0])
            started = time.monotonic()
            response = page.goto(destination, wait_until="load", timeout=self.timeout_ms)
            self.last_navigation_ms = (time.monotonic() - started) * 1000
            if response is None or response.status >= 400:
                raise ActionFailedError(f"Navigation did not load successfully: {destination}")
            return f"Opened {destination}"
        if command == "ASSERT_LOAD_TIME":
            self._require_arguments(command, values, 1)
            try:
                limit = int(values[0])
            except ValueError as exc:
                raise UnsupportedStepError("ASSERT_LOAD_TIME needs a millisecond limit") from exc
            if limit <= 0 or self.last_navigation_ms is None:
                raise UnsupportedStepError("ASSERT_LOAD_TIME needs a positive limit after OPEN")
            if self.last_navigation_ms > limit:
                raise ActionFailedError(f"Page loaded in {self.last_navigation_ms:.0f} ms, above {limit} ms")
            self.assertion_count += 1
            return f"Verified load time at most {limit} ms"
        if command == "WATCH_DIALOG":
            self._require_arguments(command, values, 1)
            mode = values[0].casefold()
            if mode not in {"accept", "dismiss"}:
                raise UnsupportedStepError("WATCH_DIALOG needs accept or dismiss")
            self.last_dialog = None

            def handle_dialog(dialog):
                self.last_dialog = dialog.message
                getattr(dialog, mode)()

            page.once("dialog", handle_dialog)
            return "Watching the next browser dialog"
        if command == "ASSERT_DIALOG":
            self._require_arguments(command, values, 1)
            if self.last_dialog is None or (
                values[0].casefold() != "any" and values[0] not in self.last_dialog
            ):
                raise ActionFailedError(f"Expected browser dialog did not appear: {values[0]}")
            self.assertion_count += 1
            return "Verified browser dialog"
        if command == "ASSERT_URL":
            self._require_arguments(command, values, 1)
            self.assertion_count += 1
            expected = values[0]
            actual = page.url
            matches = urlparse(actual).path == expected if expected.startswith("/") else actual == expected
            if not matches:
                raise ActionFailedError(f"URL did not match: expected {expected}, got {actual}")
            return f"Verified URL {expected}"
        if command == "ASSERT_URL_CONTAINS":
            self._require_arguments(command, values, 1)
            self.assertion_count += 1
            if values[0] not in page.url:
                raise ActionFailedError(f"URL does not contain: {values[0]}")
            return f"Verified URL contains {values[0]}"
        if command == "ASSERT_URL_NOT_CONTAINS":
            self._require_arguments(command, values, 1)
            if values[0] in page.url:
                raise ActionFailedError(f"URL unexpectedly contains: {values[0]}")
            self.assertion_count += 1
            return f"Verified URL excludes {values[0]}"
        if command == "ASSERT_TITLE":
            self._require_arguments(command, values, 1)
            self.assertion_count += 1
            if values[0] not in page.title():
                raise ActionFailedError(f"Page title does not contain: {values[0]}")
            return f"Verified page title contains {values[0]}"
        if command in {
            "ASSERT_COUNT", "ASSERT_EACH_TEXT_CONTAINS", "ASSERT_COUNT_MATCHES", "ASSERT_NUMBER_EQUALS",
            "ASSERT_MULTIPLY", "ASSERT_DATA_FIELDS",
        }:
            counts = {"ASSERT_MULTIPLY": 3}
            self._require_arguments(command, values, counts.get(command, 2))
            if command == "ASSERT_COUNT":
                try:
                    expected = int(values[1])
                except ValueError as exc:
                    raise UnsupportedStepError("ASSERT_COUNT needs an integer") from exc
                if expected < 0:
                    raise UnsupportedStepError("ASSERT_COUNT needs a nonnegative integer")
                actual = len([item for item in self._locator(page, values[0], command).all() if item.is_visible()])
                if actual != expected:
                    raise ActionFailedError(f"Count mismatch for {values[0]}: expected {expected}, got {actual}")
            elif command == "ASSERT_NUMBER_EQUALS":
                expected = self._number(values[1])
                actual = self._number(self._element_text(self._locator(page, values[0], command)))
                if actual != expected:
                    raise ActionFailedError(f"Number mismatch for {values[0]}: expected {expected}, got {actual}")
            elif command == "ASSERT_EACH_TEXT_CONTAINS":
                items = [item for item in self._locator(page, values[0], command).all() if item.is_visible()]
                if not items or any(values[1].casefold() not in item.inner_text().casefold() for item in items):
                    raise ActionFailedError(f"Not every visible {values[0]} contains {values[1]}")
            elif command == "ASSERT_COUNT_MATCHES":
                actual = len([item for item in self._locator(page, values[0], command).all() if item.is_visible()])
                expected = self._number(self._element_text(self._locator(page, values[1], command)))
                if Decimal(actual) != expected:
                    raise ActionFailedError(f"Count mismatch: {actual} visible items, expected {expected}")
            elif command == "ASSERT_MULTIPLY":
                price, quantity, total = (
                    self._number(self._element_text(self._locator(page, reference, command)))
                    for reference in values
                )
                if price * quantity != total:
                    raise ActionFailedError(f"Cart arithmetic mismatch: {price} x {quantity} != {total}")
            elif command == "ASSERT_DATA_FIELDS":
                keys = [key.strip().casefold() for key in values[1].split(",")]
                if not keys or any(not key or not data.get(key) for key in keys):
                    raise UnsupportedStepError("ASSERT_DATA_FIELDS needs populated Test Data keys")
                actual = " ".join(self._locator(page, values[0], command).inner_text().casefold().split())
                missing = [key for key in keys if " ".join(data[key].casefold().split()) not in actual]
                if missing:
                    raise ActionFailedError("Address fields not present on page: " + ", ".join(missing))
            self.assertion_count += 1
            return f"Verified {command.removeprefix('ASSERT_').lower().replace('_', ' ')}"

        self._require_arguments(
            command, values,
            2 if command in {
                "FILL", "SELECT", "PRESS", "UPLOAD", "ASSERT_TEXT", "ASSERT_VALUE",
                "ASSERT_VALIDATION",
            } else 1,
        )
        reference = values[0]
        if command == "ASSERT_HIDDEN":
            self.assertion_count += 1
            if any(item.is_visible() for item in self._locator(page, reference, command).all()):
                raise ActionFailedError(f"Locator is visible: {reference}")
            return f"Verified {reference} is hidden"
        if command == "WAIT_HIDDEN":
            locator = self._locator(page, reference, command)
            locator.first.wait_for(state="hidden", timeout=self.timeout_ms)
            return f"Waited for {reference} to become hidden"
        if command == "WAIT_VISIBLE":
            if self._is_direct_reference(reference) or reference in (self.config.get("locators") or {}):
                try:
                    self._locator(page, reference, command).first.wait_for(
                        state="visible", timeout=self.timeout_ms,
                    )
                except PlaywrightTimeoutError as exc:
                    raise ActionFailedError(f"Visible locator not found: {reference}") from exc
            target, healing_match = self._visible_target(page, reference, command)
            target.wait_for(state="visible", timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Waited for {reference} to become visible"

        target, healing_match = self._visible_target(
            page, reference, command,
            expected_text=values[1] if command == "ASSERT_TEXT" else None,
        )
        healing_source = f" ({healing_match.source})" if healing_match is not None else ""
        if command == "CLICK":
            target.click(timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Clicked {reference}{healing_source}"
        if command == "FILL":
            self._require_arguments(command, values, 2)
            target.fill(values[1], timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Filled {reference}{healing_source}"
        if command == "CLEAR":
            target.fill("", timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Cleared {reference}{healing_source}"
        if command == "SELECT":
            self._require_arguments(command, values, 2)
            option = values[1]
            if option.startswith("value="):
                target.select_option(value=option.removeprefix("value="), timeout=self.timeout_ms)
            else:
                target.select_option(label=option.removeprefix("label="), timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Selected option in {reference}{healing_source}"
        if command == "CHECK":
            target.check(timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Checked {reference}{healing_source}"
        if command == "UNCHECK":
            target.uncheck(timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Unchecked {reference}{healing_source}"
        if command == "PRESS":
            self._require_arguments(command, values, 2)
            target.press(values[1], timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Pressed {values[1]} on {reference}{healing_source}"
        if command == "HOVER":
            target.hover(timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Hovered over {reference}{healing_source}"
        if command == "UPLOAD":
            self._require_arguments(command, values, 2)
            path = Path(values[1])
            if not path.is_absolute():
                path = self.suite_path.parent / path
            if not path.is_file():
                raise ActionFailedError(f"Upload file not found: {path}")
            target.set_input_files(str(path.resolve()), timeout=self.timeout_ms)
            self._remember_success(healing_match)
            return f"Uploaded {path.name} to {reference}{healing_source}"
        if command == "ASSERT_VISIBLE":
            self.assertion_count += 1
            self._remember_success(healing_match)
            return f"Verified {reference} is visible"
        if command == "ASSERT_TEXT":
            self._require_arguments(command, values, 2)
            self.assertion_count += 1
            actual = target.inner_text(timeout=self.timeout_ms)
            if values[1] not in actual:
                raise ActionFailedError(f"Text not found in {reference}: {values[1]}")
            self._remember_success(healing_match)
            return f"Verified text in {reference}"
        if command == "ASSERT_VALUE":
            self._require_arguments(command, values, 2)
            self.assertion_count += 1
            actual = target.input_value(timeout=self.timeout_ms)
            if actual != values[1]:
                raise ActionFailedError(f"Value mismatch in {reference}: expected {values[1]!r}, got {actual!r}")
            self._remember_success(healing_match)
            return f"Verified value in {reference}"
        if command == "ASSERT_VALIDATION":
            self._require_arguments(command, values, 2)
            self.assertion_count += 1
            message = target.evaluate("el => !el.checkValidity() ? el.validationMessage : ''")
            if not message or (values[1].casefold() != "any" and values[1] not in message):
                raise ActionFailedError(f"Validation message did not match for {reference}")
            self._remember_success(healing_match)
            return f"Verified browser validation on {reference}"
        raise UnsupportedStepError(f"Unsupported generic command: {command}")

    @staticmethod
    def _number(value: str) -> Decimal:
        numbers = re.findall(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?", value)
        if len(numbers) != 1:
            raise ActionFailedError(f"Expected one numeric value, found {len(numbers)} in {value!r}")
        try:
            return Decimal(numbers[0].replace(",", ""))
        except InvalidOperation as exc:
            raise ActionFailedError(f"Invalid numeric value: {value!r}") from exc

    @staticmethod
    def _element_text(locator) -> str:
        if locator.count() != 1:
            raise ActionFailedError("Numeric assertion needs exactly one element")
        element = locator.first
        if element.evaluate("el => 'value' in el"):
            return element.input_value()
        return element.inner_text()

    def _parse_step(self, step: str) -> tuple[str, list[str]]:
        values = [value.strip() for value in step.split("|")]
        command = values[0].upper()
        if command not in COMMANDS:
            raise UnsupportedStepError(f"Unsupported generic command: {values[0]}")
        return command, values[1:]

    @staticmethod
    def _require_arguments(command: str, values: list[str], count: int) -> None:
        if len(values) != count or any(not value for value in values):
            raise UnsupportedStepError(f"{command} needs {count} pipe-separated argument(s)")

    def _interpolate(self, value: str, data: dict[str, str]) -> str:
        variables = self.config.get("variables", {})
        environment = self.config["environments"][self.env]
        environment_variables = environment.get("variables", {}) if isinstance(environment, Mapping) else {}

        def replace(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            if key == "BASE_URL":
                return self.base_url
            if key.upper().startswith("ENV:"):
                name = key[4:].strip()
                value = os.getenv(name)
                if value is None:
                    raise UnsupportedStepError(f"Environment variable is not set: {name}")
                return value
            if key.upper().startswith("DATA:"):
                value = data.get(key[5:].strip().casefold())
            else:
                value = data.get(key.casefold())
                if value is None and isinstance(environment_variables, Mapping):
                    value = environment_variables.get(key)
                if value is None and isinstance(variables, Mapping):
                    value = variables.get(key)
            if value is None:
                raise UnsupportedStepError(f"Variable is not defined: {match.group(1)}")
            if key.upper().startswith("DATA:") and re.search(r"<[^>]+>", str(value)):
                raise UnsupportedStepError(f"Test Data for {key[5:].strip()} needs a real value")
            return str(value)

        return VARIABLE.sub(replace, value)

    def _url(self, value: str) -> str:
        return value if urlparse(value).scheme else urljoin(self.base_url.rstrip("/") + "/", value.lstrip("/"))

    def _visible_target(
        self, page, reference: str, command: str, *, expected_text: str | None = None,
    ):
        actionable = command in ACTION_COMMANDS
        kind = f"generic-{command.casefold()}"
        mode = self.config.get("resolver", {}).get("discovery_mode")
        ai_first = actionable and mode == "ai_first"
        batch_cached = mode == "batch_cached"
        if batch_cached and command in CACHEABLE_COMMANDS and self.healer is not None:
            cache_expected_text = (
                None if self._direct_selector(reference) is not None else expected_text
            )
            match = self.healer.resolve(
                page, reference, kind=kind, allow_ai=False, expected_text=cache_expected_text,
            )
            if match is not None:
                return match.element, match
            if not self.config.get("_discovery_run") and not self._is_direct_reference(reference):
                match = self.healer.resolve(
                    page, reference, kind=kind, expected_text=expected_text,
                )
                if match is not None:
                    return match.element, match
                raise ActionFailedError(f"Could not resolve cached locator: {reference}")
        if ai_first and self.healer is not None:
            match = self.healer.resolve(
                page, reference, kind=kind, expected_text=expected_text,
            )
            if match is not None:
                return match.element, match
        visible = [item for item in self._locator(page, reference, command).all() if item.is_visible()]
        if len(visible) == 1:
            if command in DIRECT_CACHEABLE_COMMANDS:
                selector = self._direct_selector(reference)
                page_path = urlparse(page.url).path or "/"
                page_locators = self.config.get("locators_by_page")
                scoped_page = page_locators.get(page_path, {}) if isinstance(page_locators, Mapping) else {}
                scoped = scoped_page.get(command, {}) if isinstance(scoped_page, Mapping) else {}
                configured = self.config.get("locators")
                if (
                    selector is not None
                    and (not isinstance(scoped, Mapping) or reference not in scoped)
                    and (not isinstance(configured, Mapping) or reference not in configured)
                ):
                    self._current_direct_locator = (
                        page_path,
                        f"generic-{command.casefold()}:{reference.casefold()}",
                        selector,
                    )
            return visible[0], None
        if (
            not visible and batch_cached and expected_text
            and self.healer is not None and not self.config.get("_discovery_run")
        ):
            match = self.healer.resolve(
                page, reference, kind=kind, expected_text=expected_text,
            )
            if match is not None:
                return match.element, match
        if batch_cached and actionable and not self.config.get("_discovery_run") and self.healer is not None:
            match = self.healer.resolve(
                page, reference, kind=kind, expected_text=expected_text,
            )
            if match is not None:
                return match.element, match
        if actionable and not ai_first and self.healer is not None:
            if not batch_cached:
                match = self.healer.resolve(
                    page, reference, kind=kind, expected_text=expected_text,
                )
                if match is not None:
                    return match.element, match
        if not visible:
            raise ActionFailedError(f"Visible locator not found: {reference}")
        raise UnsupportedStepError(f"Locator is ambiguous: {reference} matched {len(visible)} visible elements")

    @staticmethod
    def _is_direct_reference(reference: str) -> bool:
        strategy, separator, _ = reference.partition("=")
        return reference.startswith(("#", ".", "[", "//")) or (
            bool(separator) and strategy.casefold() in {
                "css", "xpath", "text", "label", "placeholder", "testid", "role",
            }
        ) or bool(re.match(
            r"^(?:a|button|input|form|h[1-6]|section|div|span|p|label|select|textarea|ul|li|table|tr|td|th)"
            r"(?:$|[.#:[> ])", reference,
        ))

    @classmethod
    def _direct_selector(cls, reference: str) -> str | None:
        strategy, separator, value = reference.partition("=")
        if separator and strategy.casefold() == "css":
            return value or None
        if separator and strategy.casefold() == "xpath":
            return f"xpath={value}" if value else None
        if separator and strategy.casefold() in {
            "text", "label", "placeholder", "testid", "role",
        }:
            return None
        if reference.startswith("//"):
            return f"xpath={reference}"
        return reference if cls._is_direct_reference(reference) else None

    def _remember_success(self, match: Any | None) -> None:
        if match is not None and self.healer is not None:
            self.healer.remember_success(match)
        elif self._current_direct_locator is not None:
            page, target, selector = self._current_direct_locator
            self._pending_direct_locators[(page, target)] = selector
        self._current_direct_locator = None

    def _locator(self, page, reference: str, command: str = ""):
        page_locators = self.config.get("locators_by_page", {})
        page_path = urlparse(page.url).path or "/"
        scoped = page_locators.get(page_path, {}) if isinstance(page_locators, Mapping) else {}
        command_locators = scoped.get(command, {}) if isinstance(scoped, Mapping) else {}
        if isinstance(command_locators, Mapping) and reference in command_locators:
            return page.locator(str(command_locators[reference]))
        configured = self.config.get("locators", {})
        spec = configured.get(reference, reference) if isinstance(configured, Mapping) else reference
        if isinstance(spec, str):
            return self._string_locator(page, spec)
        if not isinstance(spec, Mapping):
            raise UnsupportedStepError(f"Locator {reference!r} must be a string or YAML mapping")
        by = str(spec.get("by", "")).casefold()
        value = str(spec.get("value", ""))
        if by == "role":
            role = str(spec.get("role") or value)
            return page.get_by_role(role, name=spec.get("name"), exact=bool(spec.get("exact", True)))
        if by == "css":
            return page.locator(value)
        if by == "xpath":
            return page.locator(f"xpath={value}")
        if by == "text":
            return page.get_by_text(value, exact=bool(spec.get("exact", True)))
        if by == "label":
            return page.get_by_label(value, exact=bool(spec.get("exact", True)))
        if by == "placeholder":
            return page.get_by_placeholder(value, exact=bool(spec.get("exact", True)))
        if by == "testid":
            return page.get_by_test_id(value)
        raise UnsupportedStepError(f"Unsupported locator strategy for {reference}: {by or '(missing by)'}")

    @staticmethod
    def _string_locator(page, spec: str):
        strategy, separator, value = spec.partition("=")
        if not separator or strategy.casefold() not in {
            "css", "xpath", "text", "label", "placeholder", "testid", "role",
        }:
            return page.locator(spec)
        strategy = strategy.casefold()
        if strategy == "css":
            return page.locator(value)
        if strategy == "xpath":
            return page.locator(f"xpath={value}")
        if strategy == "text":
            return page.get_by_text(value, exact=True)
        if strategy == "label":
            return page.get_by_label(value, exact=True)
        if strategy == "placeholder":
            return page.get_by_placeholder(value, exact=True)
        if strategy == "testid":
            return page.get_by_test_id(value)
        if strategy == "role":
            role, separator, name = value.partition("|")
            if not separator:
                raise UnsupportedStepError("Role locator needs role=name, for example role=button|Save")
            return page.get_by_role(role, name=name, exact=True)
        raise UnsupportedStepError(f"Unsupported locator strategy: {strategy}")
