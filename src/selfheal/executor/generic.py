"""Configuration-driven Playwright actions for arbitrary browser applications."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from selfheal.errors import ActionFailedError, UnsupportedStepError

COMMANDS = frozenset(
    {
        "OPEN", "CLICK", "FILL", "CLEAR", "SELECT", "CHECK", "UNCHECK", "PRESS", "HOVER",
        "UPLOAD", "WAIT_VISIBLE", "WAIT_HIDDEN", "ASSERT_VISIBLE", "ASSERT_HIDDEN", "ASSERT_TEXT",
        "ASSERT_VALUE", "ASSERT_URL", "ASSERT_URL_CONTAINS", "ASSERT_TITLE",
    }
)
ACTION_COMMANDS = frozenset(
    {"CLICK", "FILL", "CLEAR", "SELECT", "CHECK", "UNCHECK", "PRESS", "HOVER", "UPLOAD"}
)
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

    def reset(self) -> None:
        self.assertion_count = 0

    @staticmethod
    def is_generic_step(step: str) -> bool:
        command, separator, _ = step.partition("|")
        return bool(separator) and command.strip().upper() in COMMANDS

    def execute(self, page, step: str, data: dict[str, str]) -> str:
        command, arguments = self._parse_step(step)
        values = [self._interpolate(value, data) for value in arguments]
        if command == "OPEN":
            self._require_arguments(command, values, 1)
            destination = self._url(values[0])
            response = page.goto(destination, wait_until="load", timeout=self.timeout_ms)
            if response is None or response.status >= 400:
                raise ActionFailedError(f"Navigation did not load successfully: {destination}")
            return f"Opened {destination}"
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
        if command == "ASSERT_TITLE":
            self._require_arguments(command, values, 1)
            self.assertion_count += 1
            if values[0] not in page.title():
                raise ActionFailedError(f"Page title does not contain: {values[0]}")
            return f"Verified page title contains {values[0]}"

        self._require_arguments(command, values, 1)
        reference = values[0]
        if command == "ASSERT_HIDDEN":
            self.assertion_count += 1
            if any(item.is_visible() for item in self._locator(page, reference).all()):
                raise ActionFailedError(f"Locator is visible: {reference}")
            return f"Verified {reference} is hidden"
        if command == "WAIT_HIDDEN":
            locator = self._locator(page, reference)
            locator.first.wait_for(state="hidden", timeout=self.timeout_ms)
            return f"Waited for {reference} to become hidden"
        if command == "WAIT_VISIBLE":
            locator = self._locator(page, reference)
            locator.first.wait_for(state="visible", timeout=self.timeout_ms)
            return f"Waited for {reference} to become visible"

        target, healing_match = self._visible_target(page, reference, command)
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
            return f"Verified {reference} is visible"
        if command == "ASSERT_TEXT":
            self._require_arguments(command, values, 2)
            self.assertion_count += 1
            actual = target.inner_text(timeout=self.timeout_ms)
            if values[1] not in actual:
                raise ActionFailedError(f"Text not found in {reference}: {values[1]}")
            return f"Verified text in {reference}"
        if command == "ASSERT_VALUE":
            self._require_arguments(command, values, 2)
            self.assertion_count += 1
            actual = target.input_value(timeout=self.timeout_ms)
            if actual != values[1]:
                raise ActionFailedError(f"Value mismatch in {reference}: expected {values[1]!r}, got {actual!r}")
            return f"Verified value in {reference}"
        raise UnsupportedStepError(f"Unsupported generic command: {command}")

    def _parse_step(self, step: str) -> tuple[str, list[str]]:
        values = [value.strip() for value in step.split("|")]
        command = values[0].upper()
        if command not in COMMANDS:
            raise UnsupportedStepError(f"Unsupported generic command: {values[0]}")
        return command, values[1:]

    @staticmethod
    def _require_arguments(command: str, values: list[str], count: int) -> None:
        if len(values) < count or any(not value for value in values[:count]):
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
            return str(value)

        return VARIABLE.sub(replace, value)

    def _url(self, value: str) -> str:
        return value if urlparse(value).scheme else urljoin(self.base_url.rstrip("/") + "/", value.lstrip("/"))

    def _visible_target(self, page, reference: str, command: str):
        actionable = command in ACTION_COMMANDS
        kind = f"generic-{command.casefold()}"
        ai_first = actionable and self.config.get("resolver", {}).get("discovery_mode") == "ai_first"
        if ai_first and self.healer is not None:
            match = self.healer.resolve(page, reference, kind=kind)
            if match is not None:
                return match.element, match
        visible = [item for item in self._locator(page, reference).all() if item.is_visible()]
        if len(visible) == 1:
            return visible[0], None
        if actionable and not ai_first and self.healer is not None:
            match = self.healer.resolve(page, reference, kind=kind)
            if match is not None:
                return match.element, match
        if not visible:
            raise ActionFailedError(f"Visible locator not found: {reference}")
        raise UnsupportedStepError(f"Locator is ambiguous: {reference} matched {len(visible)} visible elements")

    def _remember_success(self, match: Any | None) -> None:
        if match is not None and self.healer is not None:
            self.healer.remember_success(match)

    def _locator(self, page, reference: str):
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
        if not separator:
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
