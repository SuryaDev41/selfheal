"""Execute spreadsheet steps against a real Playwright page."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from selfheal.errors import ActionFailedError, UnsupportedStepError
from selfheal.healing import LocatorHealer
from selfheal.telemetry.langsmith import LangSmithTracker
from selfheal.time_utils import now_ist

from .generic import GenericStepExecutor


def parse_test_data(raw: object) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip().lower()] = value.strip()
    return values


def split_steps(raw: object) -> list[str]:
    return [
        re.sub(r"^\s*\d+[.)]\s*", "", line).strip()
        for line in str(raw or "").splitlines()
        if line.strip()
    ]


def quoted_values(raw: str) -> list[str]:
    return re.findall(r"'([^']+)'", raw)


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _action_label(value: object) -> str:
    without_icons = re.sub(r"[\ue000-\uf8ff]", "", str(value or ""))
    without_count = re.sub(r"^\s*\(\d+\)\s*", "", without_icons)
    return " ".join(without_count.split()).casefold()


FIELD_ALIASES = {
    "first name": ("first name", "firstname", "first_name"),
    "last name": ("last name", "lastname", "last_name"),
    "name": ("name", "username", "your name"),
    "email": ("email", "email address", "e-mail"),
    "password": ("password",),
    "dob day": ("day", "days"),
    "dob month": ("month", "months"),
    "dob year": ("year", "years"),
    "address": ("address", "address1", "street address"),
    "country": ("country",),
    "state": ("state", "province"),
    "city": ("city", "town"),
    "zipcode": ("zip", "zipcode", "postal code", "postcode"),
    "mobile": ("mobile", "mobile number", "phone", "telephone"),
    "subject": ("subject",),
    "message": ("message",),
    "review": ("review",),
    "search": ("search", "keyword"),
    "quantity": ("quantity", "qty"),
    "comment": ("comment", "message"),
    "name on card": ("name on card", "name_on_card", "cardname"),
    "card no": ("card no", "card number", "card_number"),
    "cvc": ("cvc", "cvv"),
    "expiry month": ("expiry month", "expiry_month"),
    "expiry year": ("expiry year", "expiry_year"),
}

FIELD_WORDS = re.compile(
    r"first/last name|first name|last name|zipcode|postal code|"
    r"name|email|password|dob|address|country|state|city|zip|mobile|"
    r"subject|message|review|keyword|search|quantity|comment|title",
    re.I,
)


class BrowserExecutor:
    def __init__(self, config: dict, env: str, suite_path: Path, langsmith: LangSmithTracker | None = None):
        self.config = config
        self.env = env
        self.suite_path = suite_path
        self.base_url = config["environments"][env]["base_url"]
        self.timeout_ms = int(config.get("execution", {}).get("timeout_seconds", 30) * 1000)
        self.configured_account = dict(config["environments"][env].get("account", {}))
        if os.getenv("TEST_EMAIL"):
            self.configured_account["email"] = os.environ["TEST_EMAIL"]
        if os.getenv("TEST_PASSWORD"):
            self.configured_account["password"] = os.environ["TEST_PASSWORD"]
        self.registered_account: dict[str, str] | None = None
        self.last_form = None
        self.last_field = None
        self.last_submit_url = None
        self.review_notes: list[str] = []
        self.cart_products: list[str] = []
        self.removed_cart_products: list[str] = []
        self.add_feedback: list[str] = []
        self.listings: dict[str, list[dict[str, str]]] = {}
        self.product_list_verified = False
        self.home_load_seconds: float | None = None
        self.brand_sidebar_count: int | None = None
        self.invalid_subscription: dict | None = None
        self.cart_snapshot: dict[str, dict[str, str]] = {}
        self.checkout_address_verified = False
        self.checkout_order_verified = False
        self._detail_cache: dict[str, dict[str, str]] = {}
        self.langsmith = langsmith if langsmith is not None else LangSmithTracker()
        self.healer = LocatorHealer(config, env, langsmith=self.langsmith)
        self.generic = GenericStepExecutor(config, env, suite_path, self.timeout_ms, healer=self.healer)

    def _prepare_data(self, raw: object) -> dict[str, str]:
        data = parse_test_data(raw)
        timestamp = now_ist().strftime("%Y%m%d%H%M%S%f")
        for key, value in data.items():
            value = re.sub(r"<timestamp>", timestamp, value, flags=re.I)
            if value.lower() in {"(blank)", "blank"}:
                value = ""
            elif value.casefold() in {"<previously registered email>", "<test account email>"} and "email" in key:
                value = (self.registered_account or {}).get("email", value)
            elif value.casefold() == "<registered email>" and "email" in key:
                value = (self.registered_account or self.configured_account).get("email", value)
            elif value.casefold() == "<registered password>" and "password" in key:
                value = (self.registered_account or self.configured_account).get("password", value)
            elif value.casefold() == "<test email>" and "email" in key:
                value = self.configured_account.get("email", value)
            elif value.casefold() == "<test password>" and "password" in key:
                value = self.configured_account.get("password", value)
            data[key] = value
        return data

    @staticmethod
    def _visible(locator, allow_first: bool = False):
        matches = [item for item in locator.all() if item.is_visible()]
        if not matches:
            return None
        if len(matches) > 1 and not allow_first:
            raise UnsupportedStepError(f"Ambiguous target: {len(matches)} visible elements match")
        return matches[0]

    @staticmethod
    def _text_pattern(text: str):
        escaped = re.escape(text).replace(re.escape("<username>"), r"\S(?:.*\S)?")
        return re.compile(escaped, re.I)

    def _text_visible(self, page, text: str, *, exact: bool = False) -> bool:
        locator = (
            page.get_by_text(self._text_pattern(text))
            if "<username>" in text else page.get_by_text(text, exact=exact)
        )
        return any(
            element.is_visible()
            for element in locator.all()[:20]
        )

    def _wait_text_visible(self, page, text: str, seconds: float = 3, *, exact: bool = False) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._text_visible(page, text, exact=exact):
                return True
            page.wait_for_timeout(100)
        return self._text_visible(page, text, exact=exact)

    def _section(self, page, heading: str):
        headings = page.get_by_text(heading, exact=True)
        item = self._visible(headings)
        if item is None:
            raise ActionFailedError(f"Section heading not found: {heading}")
        for _ in range(5):
            if item.locator("input, textarea, select").count():
                return item
            item = item.locator("xpath=..")
        raise ActionFailedError(f"No form fields near section: {heading}")

    def _field_score(self, element, field: str) -> int:
        meta = element.evaluate(
            """el => ({
                label: Array.from(el.labels || []).map(x => x.textContent).join(' '),
                name: el.getAttribute('name'), id: el.id,
                placeholder: el.getAttribute('placeholder'),
                aria: el.getAttribute('aria-label'), type: el.getAttribute('type')
            })"""
        )
        aliases = {_normalized(item) for item in FIELD_ALIASES.get(field, (field,))}
        score = 0
        for attribute in ("label", "name", "id", "placeholder", "aria"):
            value = _normalized(meta.get(attribute))
            if not value:
                continue
            if value in aliases:
                score = max(score, 100)
            elif any(alias in value for alias in aliases):
                score = max(score, 60)
        if meta.get("type") == field and field in {"email", "password", "search"}:
            score = max(score, 40)
        return score

    def _field(self, scope, field: str):
        candidates = []
        for item in scope.locator("input, textarea, select").all():
            if not item.is_visible() or item.get_attribute("disabled") is not None:
                continue
            if item.get_attribute("type") in {"hidden", "radio", "checkbox", "submit"}:
                continue
            score = self._field_score(item, field)
            if score:
                candidates.append((score, item))
        if not candidates:
            raise ActionFailedError(f"Field not found: {field}")
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise UnsupportedStepError(f"Ambiguous field: {field}; add a section with 'under ...'")
        winner = candidates[0][1]
        for attribute in ("id", "name"):
            value = winner.get_attribute(attribute)
            if value:
                stable = scope.locator(f"input[{attribute}={json.dumps(value)}], "
                                       f"textarea[{attribute}={json.dumps(value)}], "
                                       f"select[{attribute}={json.dumps(value)}]")
                if stable.count() == 1 and stable.is_visible():
                    return stable
        return winner

    def _remember_form(self, field) -> None:
        form = field.locator("xpath=ancestor::form[1]")
        if form.count():
            self.last_form = form

    def _password_form(self, page):
        forms = page.locator("form:has(input[type=password])")
        visible = [
            form for form in forms.all()
            if any(field.is_visible() for field in form.locator("input[type=password]").all())
        ]
        if len(visible) > 1:
            raise UnsupportedStepError("Several password forms are visible")
        return visible[0] if visible else None

    def _fill(self, scope, field: str, value: str) -> str:
        element = self._field(scope, field)
        tag = element.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            options = element.locator("option").all()
            matching = [
                option for option in options
                if option.inner_text().strip().casefold() == value.casefold()
                or (option.get_attribute("value") or "").casefold() == value.casefold()
            ]
            if not matching:
                raise ActionFailedError(f"Option not found for {field}: {value}")
            option_value = matching[0].get_attribute("value")
            element.select_option(value=option_value, timeout=self.timeout_ms)
        else:
            element.fill(value, timeout=self.timeout_ms)
            if element.input_value() != value:
                raise ActionFailedError(f"Value was not entered in {field}")
        self._remember_form(element)
        self.last_field = element
        return field

    def _fill_title(self, scope, data: dict[str, str]) -> None:
        options = [item for item in scope.locator("input[type=radio]").all() if item.is_visible()]
        if not options:
            raise ActionFailedError("Title choice not found")
        value = data.get("title") or self.config.get("registration", {}).get("default_title")
        if value:
            options = [
                item for item in options
                if _normalized(item.get_attribute("value")) == _normalized(value)
                or _normalized(item.evaluate(
                    "el => Array.from(el.labels || []).map(x => x.textContent).join(' ')"
                )) == _normalized(value)
            ]
            if len(options) != 1:
                raise ActionFailedError(f"Title option not found or ambiguous: {value}")
        else:
            self.review_notes.append("Title was not in Test Data; selected the first visible option")
        options[0].check(timeout=self.timeout_ms)

    def _fill_dob(self, scope, raw: str) -> None:
        date = None
        for date_format in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                date = datetime.strptime(raw, date_format)
                break
            except ValueError:
                pass
        if date is None:
            raise UnsupportedStepError(f"DOB format not understood: {raw}")
        self._fill(scope, "dob day", str(date.day))
        self._fill(scope, "dob month", date.strftime("%B"))
        self._fill(scope, "dob year", str(date.year))

    def _data_value(self, data: dict[str, str], field: str, step: str) -> str:
        key = "search" if field == "search" else "zip" if field == "zipcode" else field
        if field == "email" and "subscription" in step.lower():
            key = "valid"
        if field in {"first name", "last name"} and key not in data:
            name = data.get("name", "").split()
            if field == "first name" and name:
                return name[0]
            if field == "last name" and len(name) > 1:
                return " ".join(name[1:])
        if key not in data:
            raise UnsupportedStepError(f"Test Data is missing {field}")
        value = data[key]
        if re.fullmatch(r"<[^>]+>", value) or (not value and "empty" not in step.lower()):
            raise UnsupportedStepError(f"Test Data for {field} needs a real value")
        return value

    def _fill_from_step(self, page, step: str, data: dict[str, str]) -> str:
        heading = re.search(r"\bunder\s+'([^']+)'", step, re.I)
        scope = self._section(page, heading.group(1)) if heading else page
        if heading and "signup" in heading.group(1).casefold():
            self.signup_heading = heading.group(1)
        fields: list[str] = []
        field_text = re.split(r"\s+under\s+", step, maxsplit=1, flags=re.I)[0]
        for match in FIELD_WORDS.finditer(field_text):
            token = match.group().lower()
            expanded = ["first name", "last name"] if token == "first/last name" else [token]
            for item in expanded:
                field = "search" if item == "keyword" else "zipcode" if item == "zip" else item
                if field not in fields:
                    fields.append(field)
        if not fields:
            raise UnsupportedStepError(f"No fields identified in step: {step}")
        if not heading and "email" in fields and "password" in fields:
            scope = self._password_form(page) or page
        for field in fields:
            if field == "title":
                self._fill_title(scope, data)
            elif field == "dob":
                self._fill_dob(scope, self._data_value(data, "dob", step))
            else:
                value = "" if step.lower().startswith("leave ") and "empty" in step.lower() else self._data_value(data, field, step)
                self._fill(scope, field, value)
            if not heading and self.last_form is not None and self.last_form.is_visible():
                scope = self.last_form
        return "Filled " + ", ".join(fields)

    def _interactive_matches(self, scope, target: str):
        wanted = _action_label(target)
        matches = []
        for element in scope.locator("a, button, [role=button], input[type=submit]").all():
            if not element.is_visible():
                continue
            labels = (
                element.inner_text(),
                element.get_attribute("aria-label"),
                element.get_attribute("title"),
                element.get_attribute("value"),
            )
            if any(_action_label(label) == wanted for label in labels):
                matches.append(element)
        return matches

    def _click_and_wait(self, page, element) -> None:
        element.click(timeout=self.timeout_ms)
        page.wait_for_load_state("load", timeout=self.timeout_ms)

    def _click_target(self, page, target: str, *, scope=None, first: bool = False) -> str:
        scope = scope or (
            self.last_form if self.last_form is not None and target.casefold() in {"login", "signup", "submit"} else page
        )
        if first and target.casefold() == "view product" and scope is page:
            self._capture_expected(page)
            self._click_and_wait(page, self._first_product_link(page))
            return "product link"
        if target.casefold() == "cart" and scope is page:
            modal_matches = [
                element
                for modal in page.locator("dialog:visible, [role=dialog]:visible, .modal:visible").all()
                for element in modal.locator("a, button, [role=button]").all()
                if element.is_visible() and any(
                    target.casefold() in _action_label(label).split()
                    for label in (element.inner_text(), element.get_attribute("aria-label"))
                )
            ]
            if len(modal_matches) > 1:
                raise UnsupportedStepError("Several cart controls are visible in the active dialog")
            if modal_matches:
                self._capture_expected(page)
                self._click_and_wait(page, modal_matches[0])
                return "dialog"
        ai_first = (
            scope is page
            and not first
            and self.config.get("resolver", {}).get("discovery_mode") == "ai_first"
        )
        if ai_first:
            match = self.healer.resolve(page, target, kind="click")
            if match is not None:
                self._capture_expected(page)
                self._click_and_wait(page, match.element)
                self.healer.remember_success(match)
                return match.source
        for attempt in range(2):
            candidates = [
                scope.get_by_role("button", name=target, exact=True),
                scope.get_by_role("link", name=target, exact=True),
                scope.get_by_text(target, exact=True).locator(
                    "xpath=ancestor-or-self::*[self::a or self::button or @role='button'][1]"
                ),
            ]
            ambiguity = None
            for candidate in candidates:
                try:
                    item = self._visible(candidate, allow_first=first)
                except UnsupportedStepError as exc:
                    ambiguity = exc
                    continue
                if item is not None:
                    self._capture_expected(page)
                    self._click_and_wait(page, item)
                    return "semantic"
            text_matches = self._interactive_matches(scope, target)
            if text_matches:
                if len(text_matches) > 1 and not first:
                    button_expected = f"'{target.casefold()}' button" in getattr(self, "_active_expected", "").casefold()
                    button_like = [
                        element for element in text_matches
                        if element.evaluate("el => el.matches('button, [role=button], .btn')")
                    ] if button_expected else []
                    if len(button_like) != 1:
                        raise UnsupportedStepError(f"Ambiguous target: {len(text_matches)} visible elements match {target}")
                    text_matches = button_like
                self._capture_expected(page)
                self._click_and_wait(page, text_matches[0])
                return "semantic"
            if ambiguity is not None:
                raise ambiguity
            if attempt == 0:
                self._wait_text_visible(page, target, seconds=min(self.timeout_ms / 1000, 5))
        if scope is not page:
            raise ActionFailedError(f"Clickable element not found in the intended form or product: {target}")
        match = None if ai_first else self.healer.resolve(page, target, kind="click")
        if match is None:
            raise ActionFailedError(f"Clickable element not found: {target}")
        self._capture_expected(page)
        self._click_and_wait(page, match.element)
        self.healer.remember_success(match)
        return match.source

    def _product_scope(self, page, product: str, action: str):
        product = re.sub(r"\s*\([^)]*\)\s*$", "", product).strip()
        self._wait_text_visible(page, product, seconds=min(self.timeout_ms / 1000, 5))
        for name in page.get_by_text(product, exact=True).all():
            if not name.is_visible():
                continue
            ancestor = name
            for _ in range(6):
                if self._interactive_matches(ancestor, action):
                    return ancestor
                ancestor = ancestor.locator("xpath=..")
        raise ActionFailedError(f"Product/action pair not found: {product} / {action}")

    def _product_action(self, page, data: dict[str, str], action: str, which: str = "product") -> str:
        product = data.get(which)
        if not product:
            raise UnsupportedStepError(f"Test Data is missing {which}")
        scope = self._product_scope(page, product, action)
        scope.hover(timeout=self.timeout_ms)
        source = self._click_target(page, action, scope=scope, first=True)
        product_name = re.sub(r"\s*\([^)]*\)\s*$", "", product)
        if action.casefold() == "add to cart":
            self._after_add_to_cart(page, product_name)
        return f"Clicked {action} for {product_name} ({source})"

    def _after_add_to_cart(self, page, product: str) -> None:
        modal = page.locator("dialog:visible, [role=dialog]:visible, .modal:visible").first
        try:
            modal.wait_for(state="visible", timeout=min(self.timeout_ms, 5000))
            self.add_feedback.append(modal.inner_text())
        except PlaywrightTimeoutError:
            self.add_feedback.append("")
            try:
                page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 3000))
            except PlaywrightTimeoutError:
                pass
        self.cart_products.append(re.sub(r"\s*\([^)]*\)\s*$", "", product).strip())

    def _product_cards(self, page) -> list[dict[str, str]]:
        cards: list[dict[str, str]] = []
        seen: set[str] = set()
        for link in page.get_by_role("link", name=re.compile("View Product", re.I)).all():
            if not link.is_visible():
                continue
            href = link.get_attribute("href")
            wrapper = link.locator(
                "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), "
                "' product-image-wrapper ')][1]"
            )
            if not href or href in seen or wrapper.count() != 1:
                continue
            name = wrapper.locator(".productinfo p").first
            if name.count() != 1 or not name.is_visible():
                continue
            cards.append({"name": name.inner_text().strip(), "href": href})
            seen.add(href)
        return cards

    def _first_product_link(self, page):
        cards = self._product_cards(page)
        if not cards:
            raise ActionFailedError("No visible product detail link is available")
        links = [
            link for link in page.locator(f"a[href={json.dumps(cards[0]['href'])}]").all()
            if link.is_visible()
        ]
        if len(links) != 1:
            raise UnsupportedStepError("The first product detail link is missing or ambiguous")
        return links[0]

    def _detail_info(self, page) -> dict[str, str]:
        area = page.locator(".product-information:visible").first
        if area.count() != 1:
            raise ActionFailedError("Product detail information is not visible")
        heading = area.locator("h2").first
        if heading.count() != 1 or not heading.inner_text().strip():
            raise ActionFailedError("Product detail name is not visible")
        text = area.inner_text()
        info = {"name": heading.inner_text().strip()}
        for field in ("Category", "Availability", "Condition", "Brand"):
            match = re.search(rf"(?im)^\s*{field}:\s*(.+?)\s*$", text)
            if match:
                info[field.lower()] = match.group(1).strip()
        price = re.search(r"\bRs\.?\s*([\d,]+(?:\.\d+)?)\b", text, re.I)
        if price:
            info["price"] = price.group(1).replace(",", "")
        return info

    def _detail_for_card(self, page, card: dict[str, str]) -> dict[str, str]:
        destination = urljoin(self.base_url, card["href"])
        if urlparse(destination).hostname != urlparse(self.base_url).hostname:
            raise UnsupportedStepError("Product detail link leaves the configured test site")
        if destination not in self._detail_cache:
            detail_page = page.context.new_page()
            try:
                response = detail_page.goto(
                    destination, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
                if response is None or response.status >= 400:
                    raise ActionFailedError(f"Product detail did not load: {card['name']}")
                self._detail_cache[destination] = self._detail_info(detail_page)
            finally:
                detail_page.close()
        info = self._detail_cache[destination]
        if info["name"].casefold() != card["name"].casefold():
            raise ActionFailedError(f"Product detail name differs from listing: {card['name']}")
        return info

    def _verify_home_load(self, page) -> None:
        limit = self.config.get("verification", {}).get("max_home_load_seconds")
        if limit is None or self.home_load_seconds is None:
            raise UnsupportedStepError("Set verification.max_home_load_seconds to verify home load time")
        if self.home_load_seconds > float(limit):
            raise ActionFailedError(
                f"Home loaded in {self.home_load_seconds:.2f}s, over the {limit}s limit"
            )
        if urlparse(page.url).path.rstrip("/") != urlparse(self.base_url).path.rstrip("/"):
            raise ActionFailedError("Navigation did not land on the home page")

    def _verify_home_elements(self, page, expected: str) -> None:
        header = page.locator("header").first
        if header.count() != 1 or not header.is_visible():
            raise ActionFailedError("Home page header is not visible")
        logos = [
            image for image in header.locator("img").all()
            if image.is_visible() and image.evaluate("el => el.complete && el.naturalWidth > 0")
        ]
        if not logos:
            raise ActionFailedError("Home page logo image did not load")
        menu = re.search(r"nav menu\s*\(([^)]+)\)", expected, re.I)
        if menu is None:
            raise UnsupportedStepError("Navigation items were not named in the expectation")
        for label in (item.strip() for item in menu.group(1).split(",")):
            if not self._interactive_matches(header, label):
                raise ActionFailedError(f"Home navigation item is missing: {label}")
        slider = page.locator("#slider, [aria-roledescription=carousel]")
        if not any(item.is_visible() for item in slider.all()):
            raise ActionFailedError("Home page slider is not visible")
        sections = re.search(r"slider,\s*(.+?)\s+sections are visible", expected, re.I)
        if sections is None:
            raise UnsupportedStepError("Home sections were not named in the expectation")
        for label in re.split(r",\s*|\s+and\s+", sections.group(1)):
            heading = page.get_by_role("heading", name=re.compile(rf"^{re.escape(label)}$", re.I))
            if not any(item.is_visible() for item in heading.all()):
                raise ActionFailedError(f"Home section is missing: {label}")

    def _listing(self, title: str) -> list[dict[str, str]]:
        if title not in self.listings:
            raise ActionFailedError(f"Product listing was not observed: {title}")
        return self.listings[title]

    def _verify_product_listing(self, title: str) -> None:
        if not self.product_list_verified:
            raise ActionFailedError("The product list verification step did not run")
        if not self._listing(title):
            raise ActionFailedError(f"No products are listed under {title}")

    def _verify_product_detail(self, page, expected: str, data: dict[str, str]) -> None:
        cards = self._listing("ALL PRODUCTS")
        if not cards:
            raise ActionFailedError("The All Products listing was empty")
        info = self._detail_info(page)
        expected_name = re.sub(r"\s*\([^)]*\)\s*$", "", data.get("product", "")).strip()
        if info["name"].casefold() != cards[0]["name"].casefold() or (
            expected_name and info["name"].casefold() != expected_name.casefold()
        ):
            raise ActionFailedError("The opened detail is not the first expected product")
        for field in ("category", "availability", "condition", "brand", "price"):
            if not info.get(field):
                raise ActionFailedError(f"Product detail is missing {field}")
        expected_price = re.search(r"Price\s*\(\s*Rs\.?\s*([\d,]+(?:\.\d+)?)\s*\)", expected, re.I)
        if expected_price is None:
            raise UnsupportedStepError("Product detail price expectation is missing")
        if Decimal(info["price"]) != Decimal(expected_price.group(1).replace(",", "")):
            raise ActionFailedError("Product detail price does not match the workbook")

    def _verify_search_results(self, page, expected: str, data: dict[str, str]) -> None:
        title = next((value for value in quoted_values(expected) if value.upper().endswith("PRODUCTS")), None)
        keyword = data.get("search", "")
        if not title or not keyword:
            raise UnsupportedStepError("Search check needs a result heading and keyword")
        cards = self._listing(title)
        if not cards:
            raise ActionFailedError("Search returned no visible products")
        if "name or category" in expected.casefold():
            for card in cards:
                if keyword.casefold() in card["name"].casefold():
                    continue
                info = self._detail_for_card(page, card)
                if keyword.casefold() not in info.get("category", "").casefold():
                    raise ActionFailedError(f"Search result does not match name or category: {card['name']}")
        elif "every displayed product name contains" in expected.casefold():
            for card in cards:
                if keyword.casefold() not in card["name"].casefold():
                    raise ActionFailedError(f"Search result name does not contain {keyword}: {card['name']}")
        else:
            raise UnsupportedStepError("Search matching rule is not defined")

    def _verify_empty_search(self, page, expected: str) -> None:
        title = next((value for value in quoted_values(expected) if value.upper().endswith("PRODUCTS")), None)
        if title is None:
            raise UnsupportedStepError("Empty search check needs a result heading")
        if self._listing(title) or self._product_cards(page):
            raise ActionFailedError("The invalid search still lists products")
        if not page.locator("body").is_visible():
            raise ActionFailedError("Search page is not responsive")

    def _verify_added_cart(self, page, expected: str, data: dict[str, str]) -> None:
        names = []
        for key in ("product 1", "product 2"):
            value = data.get(key, "")
            match = re.fullmatch(r"(.+?)\s*\(\s*Rs\.?\s*([\d,]+(?:\.\d+)?)\s*\)", value, re.I)
            if match is None:
                raise UnsupportedStepError(f"Test Data for {key} needs a product name and price")
            names.append((match.group(1).strip(), Decimal(match.group(2).replace(",", ""))))
        if len(self.add_feedback) != len(names) or any(
            "Added!" not in message for message in self.add_feedback
        ):
            raise ActionFailedError("Added! modal did not appear after each product")
        if not all(product in self.cart_products for product, _ in names):
            raise ActionFailedError("The expected products were not both added to the cart")
        if "cart shows" not in expected.casefold():
            return
        for product, price in names:
            row = self._cart_row_values(page, product)
            for column in ("price", "quantity", "total"):
                if column not in row:
                    raise UnsupportedStepError(f"Cart table is missing {column} column")
            if self._single_number(row["price"]) != price:
                raise ActionFailedError(f"Cart price is wrong for {product}")
            if self._single_number(row["quantity"]) != 1:
                raise ActionFailedError(f"Cart quantity is wrong for {product}")
            if self._single_number(row["total"]) != price:
                raise ActionFailedError(f"Cart total is wrong for {product}")

    def _brand_count(self, page, brand: str) -> int | None:
        counts = []
        for link in page.locator(".brands-name a").all():
            if not link.is_visible():
                continue
            href = link.get_attribute("href") or ""
            href_name = unquote(urlparse(urljoin(self.base_url, href)).path.rstrip("/").rsplit("/", 1)[-1])
            if _normalized(href_name) != _normalized(brand) and _action_label(link.inner_text()) != _action_label(brand):
                continue
            count = re.search(r"\((\d+)\)", link.text_content() or "")
            if count:
                counts.append(int(count.group(1)))
        return counts[0] if len(counts) == 1 else None

    def _verify_filtered_listing(self, page, expected: str, data: dict[str, str]) -> None:
        title = next((value for value in quoted_values(expected) if value.upper().endswith("PRODUCTS")), None)
        if title is None:
            raise UnsupportedStepError("Filter check needs a listing title")
        cards = self._listing(title)
        if not cards:
            raise ActionFailedError(f"Filtered listing is empty: {title}")
        if title.upper().startswith("BRAND - "):
            field, value = "brand", data.get("brand", "")
            current_count = self._brand_count(page, value) if page is not None else None
            if current_count is not None and self.brand_sidebar_count is not None and (
                current_count != self.brand_sidebar_count
            ):
                raise ActionFailedError("Brand count changed between category and brand pages")
            sidebar_count = current_count if current_count is not None else self.brand_sidebar_count
            if sidebar_count is None:
                raise UnsupportedStepError("Brand count was not visible in the sidebar")
            if len(cards) != sidebar_count:
                raise ActionFailedError("Brand result count differs from the sidebar")
        else:
            field, value = "category", data.get("category", "")
        if not value:
            raise UnsupportedStepError(f"Test Data is missing {field}")
        for card in cards:
            info = self._detail_for_card(page, card)
            if _normalized(info.get(field)) != _normalized(value):
                raise ActionFailedError(f"Product does not match {field} filter: {card['name']}")

    def _verify_invalid_subscription(self) -> None:
        evidence = self.invalid_subscription
        if evidence is None:
            raise ActionFailedError("Invalid subscription was not attempted")
        if evidence["valid"] or not evidence["validation_message"]:
            raise ActionFailedError("Browser did not reject the invalid email format")
        if evidence["submit_count"] != 0 or not evidence["url_unchanged"]:
            raise ActionFailedError("Invalid subscription submitted despite browser validation")

    def _raise_if_origin_error(self, page) -> None:
        heading = page.locator("h1").first
        if heading.count() and heading.is_visible() and "web server is returning" in heading.inner_text().casefold():
            code = re.search(r"Error code\s+(\d+)", page.locator("body").inner_text(), re.I)
            suffix = f" (HTTP {code.group(1)})" if code else ""
            raise ActionFailedError(f"Demo site origin error{suffix}")

    def _verify_checkout_address_and_order(self, page) -> None:
        for heading in ("Address Details", "Review Your Order"):
            if not self._text_visible(page, heading, exact=True):
                raise ActionFailedError(f"Checkout section is missing: {heading}")
        profile_keys = {
            "name": "TEST_ACCOUNT_NAME",
            "address": "TEST_ACCOUNT_ADDRESS",
            "city": "TEST_ACCOUNT_CITY",
            "state": "TEST_ACCOUNT_STATE",
            "zip": "TEST_ACCOUNT_ZIP",
            "country": "TEST_ACCOUNT_COUNTRY",
            "mobile": "TEST_ACCOUNT_MOBILE",
        }
        profile = {name: os.getenv(key) for name, key in profile_keys.items()}
        if not all(profile.values()):
            raise UnsupportedStepError("Checkout address needs the TEST_ACCOUNT_* registration profile")
        delivery = page.locator("#address_delivery:visible").first
        if delivery.count() != 1:
            raise ActionFailedError("Delivery address is not visible at checkout")
        lines = [_normalized(line) for line in delivery.locator("li").all_inner_texts() if line.strip()]
        title = self.config.get("registration", {}).get("default_title", "")
        expected_lines = (
            f"{title} {profile['name']}",
            profile["address"],
            f"{profile['city']} {profile['state']} {profile['zip']}",
            profile["country"],
            profile["mobile"],
        )
        for line in expected_lines:
            if _normalized(line) not in lines:
                raise ActionFailedError(f"Delivery address does not match registration field: {line}")
        self.checkout_address_verified = True
        if not self.cart_snapshot:
            raise ActionFailedError("Cart contents were not captured before checkout")
        total = Decimal(0)
        for product, original in self.cart_snapshot.items():
            current = self._cart_row_values(page, product)
            for column in ("price", "quantity", "total"):
                if column not in original or column not in current:
                    raise UnsupportedStepError(f"Order table is missing {column}")
                if self._single_number(original[column]) != self._single_number(current[column]):
                    raise ActionFailedError(f"Checkout {column} changed for {product}")
            total += self._single_number(current["total"])
        total_label = self._visible(page.get_by_text("Total Amount", exact=True))
        if total_label is None:
            raise ActionFailedError("Order total is not shown")
        total_row = total_label.locator("xpath=ancestor::tr[1]")
        if total_row.count() != 1 or self._single_number(total_row.inner_text()) != total:
            raise ActionFailedError("Checkout total does not equal the cart line totals")
        self.checkout_order_verified = True

    def _fill_demo_payment(self, page, data: dict[str, str]) -> str:
        demo = self.config.get("safety", {}).get("demo_orders", {})
        host = urlparse(page.url).hostname
        base_host = urlparse(self.base_url).hostname
        card = re.sub(r"[\s-]", "", data.get("card no", ""))
        if (
            not demo.get("enabled") or host != base_host
            or host not in demo.get("hosts", [])
            or card not in demo.get("card_numbers", [])
        ):
            raise UnsupportedStepError("Payment is limited to the configured demo host and test card")
        expiry = re.fullmatch(r"\s*(\d{1,2})/(\d{4})\s*", data.get("expiry", ""))
        if expiry is None or not 1 <= int(expiry.group(1)) <= 12:
            raise UnsupportedStepError("Test Data needs expiry in MM/YYYY format")
        values = {
            "name on card": self._data_value(data, "name on card", "card details"),
            "card no": card,
            "cvc": self._data_value(data, "cvc", "card details"),
            "expiry month": expiry.group(1).zfill(2),
            "expiry year": expiry.group(2),
        }
        forms = page.locator("form:has(input[name=card_number])")
        if forms.count() != 1:
            raise ActionFailedError("Demo payment form is not visible")
        form = forms.first
        if not form.locator("input[name=card_number]").is_visible():
            raise ActionFailedError("Demo payment fields are not visible")
        for field, value in values.items():
            self._fill(form, field, value)
        return "Filled demo payment fields"

    def _verify_cart_products(self, page) -> None:
        for product in dict.fromkeys(self.cart_products):
            if not self._wait_text_visible(
                page, product, seconds=self.timeout_ms / 1000, exact=True
            ):
                raise ActionFailedError(f"Cart does not show added product: {product}")

    def _cart_row(self, page, product: str):
        name = self._visible(page.get_by_text(product, exact=True))
        if name is None:
            raise ActionFailedError(f"Cart does not show product: {product}")
        row = name.locator("xpath=ancestor::tr[1]")
        if row.count() != 1:
            raise UnsupportedStepError(f"No unique cart row contains {product}")
        return row

    def _cart_row_values(self, page, product: str) -> dict[str, str]:
        row = self._cart_row(page, product)
        table = row.locator("xpath=ancestor::table[1]")
        headers = table.locator("thead tr").last.locator("th, td")
        cells = row.locator("th, td")
        header_items = headers.all()
        cell_items = cells.all()
        while len(header_items) > len(cell_items) and not header_items[-1].inner_text().strip():
            header_items.pop()
        if table.count() != 1 or not header_items or len(header_items) != len(cell_items):
            raise UnsupportedStepError("Cart table has no matching column headings")
        return {
            _normalized(header.inner_text()): " ".join(cell.inner_text().split())
            for header, cell in zip(header_items, cell_items, strict=True)
        }

    @staticmethod
    def _single_number(value: str) -> Decimal:
        matches = re.findall(r"\d[\d,]*(?:\.\d+)?", value)
        if len(matches) != 1:
            raise UnsupportedStepError(f"Cannot read a single number from cart cell: {value}")
        return Decimal(matches[0].replace(",", ""))

    def _verify_cart_amounts(self, page, expected: str, data: dict[str, str]) -> None:
        product = re.sub(r"\s*\([^)]*\)\s*$", "", data.get("product", "")).strip()
        quantity = re.search(r"\bquantity\s+(\d+)", expected, re.I)
        total = re.search(r"\btotal\s*=\s*(?:Rs\.?\s*)?([\d,]+(?:\.\d+)?)", expected, re.I)
        unit = re.search(r"\(\s*\d+\s*x\s*(?:Rs\.?\s*)?([\d,]+(?:\.\d+)?)\s*\)", expected, re.I)
        if not product or quantity is None or total is None:
            raise UnsupportedStepError("Cart amount expectation needs product, quantity, and total")
        actual = self._cart_row_values(page, product)
        numbers = {"quantity": quantity.group(1), "total": total.group(1)}
        if unit is not None:
            numbers["price"] = unit.group(1)
        for column, expected_number in numbers.items():
            if column not in actual:
                raise UnsupportedStepError(f"Cart table is missing {column} column")
            if self._single_number(actual[column]) != Decimal(expected_number.replace(",", "")):
                raise ActionFailedError(f"Cart {column} for {product} does not match {expected_number}")

    def _delete_cart_product(self, page, data: dict[str, str]) -> str:
        product = re.sub(r"\s*\([^)]*\)\s*$", "", data.get("product", "")).strip()
        if not product:
            raise UnsupportedStepError("Test Data is missing the product to remove")
        for attempt in range(2):
            row = self._cart_row(page, product)
            name = self._visible(row.get_by_text(product, exact=True))
            matches = []
            for element in row.locator("a, button, [role=button]").all():
                attributes = (
                    element.inner_text(), element.get_attribute("aria-label"),
                    element.get_attribute("title"), element.get_attribute("data-action"),
                    element.get_attribute("href"), element.get_attribute("class"),
                )
                tokens = {
                    token for value in attributes
                    for token in re.findall(r"[a-z]+", str(value or "").lower())
                }
                if tokens.intersection({"delete", "remove", "trash", "close", "clear"}) or any(
                    _action_label(value) in {"x", "×"} for value in attributes[:3]
                ):
                    matches.append(element)
            if len(matches) != 1:
                raise UnsupportedStepError(f"Delete control for {product} is missing or ambiguous")
            control = matches[0]
            box = control.bounding_box()
            if control.is_visible() and box and box["width"] > 0 and box["height"] > 0:
                control.click(timeout=self.timeout_ms)
                try:
                    name.wait_for(state="hidden", timeout=self.timeout_ms)
                except PlaywrightTimeoutError as exc:
                    raise ActionFailedError(f"Product remains in cart after remove: {product}") from exc
                self.cart_products = [item for item in self.cart_products if item != product]
                self.removed_cart_products.append(product)
                suffix = " after reloading the missing icon" if attempt else ""
                return f"Removed {product} from cart{suffix}"
            if attempt == 0:
                response = page.reload(wait_until="load", timeout=self.timeout_ms)
                if response is None or response.status >= 400:
                    raise ActionFailedError("Cart could not reload after the delete icon failed to render")
                page.evaluate("() => document.fonts.ready")
        raise UnsupportedStepError(f"Delete icon for {product} is not rendered")

    def _submit_near_field(self, page, field: str, retries_remaining: int | None = None) -> str:
        if retries_remaining is None:
            execution = self.config.get("execution", {})
            retries_remaining = int(execution.get("max_retries_per_step", 0)) if execution.get(
                "retry_on_failure", False
            ) else 0
        form = self.last_form
        if form is not None and form.count() == 1:
            item = self._visible(form.locator("button, input[type=submit]"))
            if item is not None:
                self._click_and_wait(page, item)
                return f"Submitted {field} form"
        element = self.last_field or self._field(page, field)
        original_url = page.url
        entered_value = element.input_value() if field == "search" else None
        container = element
        for _ in range(5):
            container = container.locator("xpath=..")
            buttons = container.locator("button, input[type=submit]")
            item = self._visible(buttons)
            if item is not None:
                self._click_and_wait(page, item)
                if field == "search":
                    heading = next(
                        (value for value in quoted_values(getattr(self, "_active_expected", ""))
                         if value.upper().endswith("PRODUCTS")), None
                    )
                    if heading and not self._wait_text_visible(page, heading, seconds=5):
                        if retries_remaining < 1:
                            raise ActionFailedError(f"Search did not reach its results page: {page.url}")
                        response = page.goto(original_url, wait_until="load", timeout=self.timeout_ms)
                        if response is None or response.status >= 400:
                            raise ActionFailedError("Could not restore the search page for retry")
                        self._fill(page, "search", entered_value)
                        detail = self._submit_near_field(
                            page, "search", retries_remaining=retries_remaining - 1
                        )
                        return detail + " (retried after unexpected navigation)"
                return f"Clicked {field} control"
        raise ActionFailedError(f"Submit control not found near {field}")

    def _login(self, page, data: dict[str, str], expect_success: bool) -> str:
        email = data["email"] if "email" in data else self.configured_account.get("email")
        password = data["password"] if "password" in data else self.configured_account.get("password")
        if not email or not password or any(
            re.fullmatch(r"<[^>]+>", value) for value in (email, password)
        ):
            raise UnsupportedStepError("Registered email/password are missing from Test Data or environment")
        form = self._password_form(page)
        if form is None:
            links = page.get_by_role("link", name=re.compile("login", re.I))
            link = self._visible(links)
            if link is None:
                raise ActionFailedError("Login page link not found")
            self._click_and_wait(page, link)
            page.locator("form:has(input[type=password]) input[type=password]").first.wait_for(
                state="visible", timeout=self.timeout_ms
            )
            form = self._password_form(page)
        if form is None:
            raise ActionFailedError("Login form not found")
        self._fill(form, "email", email)
        self._fill(form, "password", password)
        self.last_submit_url = page.url
        self._click_target(page, "Login", scope=form)
        if expect_success:
            try:
                page.get_by_text(re.compile(r"Logged\s+in\s+as\s+\S", re.I)).first.wait_for(
                    state="visible", timeout=self.timeout_ms
                )
            except Exception as exc:
                raise ActionFailedError("Login was submitted but no logged-in state appeared") from exc
        return "Submitted login form"

    def _execute_step(self, page, step: str, data: dict[str, str]) -> str:
        if self.generic.is_generic_step(step):
            return self.generic.execute(page, step, data)
        lower = step.casefold()
        if lower.startswith("launch browser"):
            if page.is_closed():
                raise ActionFailedError("Browser page is closed")
            return "Browser is open"
        if lower.startswith("navigate to "):
            url = re.search(r"https?://[^\s]+", step)
            destination = url.group(0) if url else self.base_url if "home" in lower else None
            if destination is None:
                raise UnsupportedStepError(f"Navigation target not understood: {step}")
            started = time.monotonic()
            response = page.goto(destination, wait_until="load", timeout=self.timeout_ms)
            self.home_load_seconds = time.monotonic() - started
            if response is None or response.status >= 400:
                raise ActionFailedError(f"Navigation did not load successfully: {destination}")
            self.last_form = None
            return f"Navigated to {destination} in {self.home_load_seconds:.2f}s"
        if lower.startswith("observe "):
            if not page.locator("body").is_visible():
                raise ActionFailedError("Page body is not visible")
            return f"Observed page at {page.url}"
        if lower.startswith("login with valid credentials") or lower == "login":
            return self._login(page, data, expect_success=True)
        if lower.startswith("try logging in again"):
            return self._login(page, data, expect_success=False)
        if lower.startswith("go to cart"):
            source = self._click_target(page, "Cart")
            self._verify_cart_products(page)
            return f"Clicked Cart ({source})"
        if lower.startswith("open product detail page"):
            return self._product_action(page, data, "View Product")
        if lower.startswith("add second product"):
            return self._product_action(page, data, "Add to cart", "product 2")
        if lower.startswith("add a product to cart"):
            return self._product_action(page, data, "Add to cart")
        if lower.startswith("add product to cart and go to cart"):
            if not data.get("product"):
                cards = self._product_cards(page)
                if not cards:
                    self._click_target(page, "Products")
                    cards = self._product_cards(page)
                if not cards:
                    raise ActionFailedError("No product is available to add to the order")
                data["product"] = cards[0]["name"]
            detail = self._product_action(page, data, "Add to cart")
            source = self._click_target(page, "Cart")
            self._verify_cart_products(page)
            self.cart_snapshot = {
                product: self._cart_row_values(page, product)
                for product in dict.fromkeys(self.cart_products)
            }
            return detail + f"; clicked Cart ({source})"
        if lower.startswith("verify address details") and "review order" in lower:
            self._verify_checkout_address_and_order(page)
            return "Verified delivery address and order totals"
        if lower.startswith("change quantity to "):
            match = re.search(r"\bto\s+(\d+)\b", step, re.I)
            value = match.group(1) if match else self._data_value(data, "quantity", step)
            self._fill(page, "quantity", value)
            return "Changed quantity"
        if lower.startswith("scroll to footer"):
            footer = page.locator("footer")
            if footer.count():
                footer.first.scroll_into_view_if_needed(timeout=self.timeout_ms)
            else:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            return "Scrolled to footer"
        if lower.startswith("repeat with invalid email"):
            value = quoted_values(step)
            invalid = value[0] if value else self._data_value(data, "invalid", step)
            self._fill(page, "email", invalid)
            form = self.last_form
            if form is None or form.count() != 1:
                raise UnsupportedStepError("Cannot observe the invalid subscription form submission")
            form.evaluate(
                "el => { el.dataset.selfhealSubmitCount = '0'; "
                "el.addEventListener('submit', () => { "
                "el.dataset.selfhealSubmitCount = String(Number(el.dataset.selfhealSubmitCount) + 1) "
                "}, {once: true}) }"
            )
            before_url = page.url
            detail = self._submit_near_field(page, "email")
            self.invalid_subscription = {
                "valid": self.last_field.evaluate("el => el.checkValidity()"),
                "validation_message": self.last_field.evaluate("el => el.validationMessage"),
                "submit_count": int(form.evaluate("el => el.dataset.selfhealSubmitCount")),
                "url_unchanged": page.url == before_url,
            }
            return detail
        if lower.startswith("upload a file"):
            raw = data.get("file", "")
            filename = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
            path = Path(filename)
            if not path.is_absolute():
                path = self.suite_path.parent / path
                if not path.exists():
                    path = Path(filename)
            if not filename or not path.is_file():
                raise ActionFailedError(f"Upload file not found: {filename or '(missing File test data)'}")
            file_input = self._visible(page.locator("input[type=file]"))
            if file_input is None and page.locator("input[type=file]").count() == 1:
                file_input = page.locator("input[type=file]")
            if file_input is None:
                raise ActionFailedError("File input not found")
            file_input.set_input_files(str(path.resolve()), timeout=self.timeout_ms)
            return f"Uploaded {path.name}"
        if lower.startswith("verify product list"):
            if not self._product_cards(page):
                raise ActionFailedError("No visible product cards and detail links found")
            self.product_list_verified = True
            return "Product list is present"
        if lower.startswith("locate "):
            terms = quoted_values(step)
            if len(terms) != 1 or not self._text_visible(page, terms[0]):
                raise ActionFailedError(f"Text not found: {terms[0] if terms else step}")
            return f"Located {terms[0]}"
        if lower.startswith("hover ") and "click" in lower:
            target = quoted_values(step)
            if len(target) != 1:
                raise UnsupportedStepError(f"Click target not understood: {step}")
            return self._product_action(page, data, target[0], "product 1")
        if lower.startswith("click ") and "delete" in lower and "icon" in lower and "against the product" in lower:
            return self._delete_cart_product(page, data)
        if lower.startswith("click ") or (
            re.search(r",\s*click\s+", lower)
            and not lower.startswith(("enter ", "fill ", "leave "))
        ):
            if "search icon" in lower:
                return self._submit_near_field(page, "search")
            if "arrow button" in lower:
                return self._submit_near_field(page, "email")
            targets = quoted_values(step)
            if not targets:
                raise UnsupportedStepError(f"Click target not understood: {step}")
            details = []
            for target in targets:
                if "brand" in lower and _action_label(target) == _action_label(data.get("brand")):
                    self.brand_sidebar_count = self._brand_count(page, target)
                if target == "Pay and Confirm Order" and self.config.get("safety", {}).get("no_real_payments", True):
                    raise UnsupportedStepError("Payment action is disabled by safety.no_real_payments")
                first = target == "View Product" and (
                    "first product" in lower or (">" in step and not data.get("product"))
                )
                self.last_submit_url = page.url
                accepted = {"seen": False}
                if "accept" in lower and "alert" in lower:
                    def accept_dialog(dialog, state=accepted):
                        state["seen"] = True
                        dialog.accept()

                    page.once("dialog", accept_dialog)
                source = self._click_target(page, target, first=first)
                if target.casefold() == "add to cart" and data.get("product"):
                    self._after_add_to_cart(page, data["product"])
                if target.casefold() in {"cart", "view cart"}:
                    self._verify_cart_products(page)
                if "accept" in lower and "alert" in lower:
                    deadline = time.monotonic() + min(self.timeout_ms / 1000, 3)
                    while not accepted["seen"] and time.monotonic() < deadline:
                        page.wait_for_timeout(100)
                    if not accepted["seen"]:
                        raise ActionFailedError("The expected browser alert did not appear")
                details.append(f"Clicked {target} ({source})")
            return "; ".join(details)
        if lower.startswith(("enter ", "fill ", "leave ")):
            if "card details" in lower:
                detail = self._fill_demo_payment(page, data)
                targets = quoted_values(step)
                if len(targets) != 1 or targets[0].casefold() != "pay and confirm order":
                    raise UnsupportedStepError("Demo payment step needs a single confirmation action")
                self.last_submit_url = page.url
                return detail + "; clicked " + targets[0] + " (" + self._click_target(page, targets[0]) + ")"
            if ", click " in lower:
                fill_part, click_part = re.split(r",\s*click\s+", step, maxsplit=1, flags=re.I)
                detail = self._fill_from_step(page, fill_part, data)
                targets = quoted_values(click_part)
                if len(targets) != 1:
                    raise UnsupportedStepError(f"Click target not understood: {step}")
                return detail + "; clicked " + targets[0] + " (" + self._click_target(page, targets[0]) + ")"
            return self._fill_from_step(page, step, data)
        raise UnsupportedStepError(f"Unsupported step: {step}")

    def _observe_expected(self, page, expected: str, observed: set[str]) -> None:
        for literal in quoted_values(expected):
            if literal.lower() == "please fill out this field":
                continue
            if self._text_visible(page, literal):
                observed.add(literal)
                if literal.upper().endswith("PRODUCTS") and literal not in self.listings:
                    self.listings[literal] = self._product_cards(page)

    def _capture_expected(self, page) -> None:
        observed = getattr(self, "_active_observed", None)
        if observed is not None:
            self._observe_expected(page, self._active_expected, observed)

    def _verify_expected(self, page, expected: str, observed: set[str], data: dict[str, str]):
        checks = []
        if not expected.strip():
            if self.generic.assertion_count:
                return [{"status": "passed", "expectation": "Inline generic assertions", "reason": None}]
            return [{"status": "needs_review", "expectation": "Expected Result is empty"}]
        for part in (item.strip() for item in expected.split(";") if item.strip()):
            lower = part.casefold()
            status = "passed"
            reason = None
            behavior_verified = False
            literals = quoted_values(part)
            if "required-field validation" in lower or "html5" in lower:
                scope = self.last_form if self.last_form is not None else page
                try:
                    email = self._field(scope, "email")
                    valid = email.evaluate("el => el.checkValidity()")
                    message = email.evaluate("el => el.validationMessage")
                    if valid or not message:
                        status, reason = "failed", "Email did not show required-field validation"
                except ActionFailedError as exc:
                    status, reason = "failed", str(exc)
                literals = [item for item in literals if item.lower() != "please fill out this field"]
            for literal in literals:
                if literal not in observed and not self._wait_text_visible(page, literal):
                    status, reason = "failed", f"Expected text was never visible: {literal}"
                    break
            if status == "passed" and ("not logged in" in lower or "logged out" in lower or "no account is created" in lower):
                if self._text_visible(page, "Logged in as"):
                    status, reason = "failed", "Logged-in state is still visible"
            if status == "passed" and "user is logged in" in lower:
                if not self._text_visible(page, "Logged in as"):
                    status, reason = "failed", "Logged-in state is not visible"
            if status == "passed" and "stays on signup page" in lower:
                if not self.signup_heading or not self._text_visible(page, self.signup_heading):
                    status, reason = "failed", "Signup form is no longer visible"
            if status == "passed" and "redirected to the login page" in lower:
                if not page.locator("form input[type=password]").count():
                    status, reason = "failed", "Login form is not visible"
            if status == "passed" and "form is not submitted" in lower:
                if self.last_submit_url is None or page.url != self.last_submit_url:
                    status, reason = "failed", "Form navigated despite expected validation"
            if status == "passed" and "returns user to home page" in lower:
                if urlparse(page.url).path.rstrip("/") != urlparse(self.base_url).path.rstrip("/"):
                    status, reason = "failed", "Home action did not return to the base URL"
            if status == "passed" and "product is removed from the cart" in lower:
                if not self.removed_cart_products or any(
                    self._text_visible(page, product, exact=True) for product in self.removed_cart_products
                ):
                    status, reason = "failed", "Cart product removal was not verified"
            if status == "passed" and "checkout page is not opened" in lower:
                dialogs = page.locator("dialog:visible, [role=dialog]:visible, .modal:visible")
                if self.last_submit_url is None or page.url != self.last_submit_url or not dialogs.count():
                    status, reason = "failed", "Checkout navigated or no guest prompt is visible"
            cart_amount_claim = "cart shows the product with quantity" in lower and "total" in lower
            if status == "passed" and cart_amount_claim:
                try:
                    self._verify_cart_amounts(page, part, data)
                    behavior_verified = True
                except UnsupportedStepError as exc:
                    status, reason = "needs_review", str(exc)
                except ActionFailedError as exc:
                    status, reason = "failed", str(exc)
            if status == "passed" and "form fields are cleared" in lower:
                try:
                    scope = self.last_form if self.last_form is not None else page
                    if any(self._field(scope, field).input_value() for field in ("name", "email", "review")):
                        status, reason = "failed", "Review form fields were not cleared"
                except ActionFailedError as exc:
                    status, reason = "failed", str(exc)
            if status == "passed":
                try:
                    matched = True
                    if "within acceptable time" in lower:
                        self._verify_home_load(page)
                    elif "logo, nav menu" in lower and "sections are visible" in lower:
                        self._verify_home_elements(page, part)
                    elif "page lists products" in lower:
                        title = next(
                            (value for value in literals if value.upper().endswith("PRODUCTS")), None
                        )
                        if title is None:
                            raise UnsupportedStepError("Product list heading was not named")
                        self._verify_product_listing(title)
                    elif "detail page shows product name" in lower:
                        self._verify_product_detail(page, part, data)
                    elif "every displayed product" in lower:
                        self._verify_search_results(page, part, data)
                    elif "no products listed" in lower:
                        self._verify_empty_search(page, part)
                    elif "no error/crash" in lower:
                        title = next(
                            (value for value in self.listings if value.upper().endswith("PRODUCTS")), None
                        )
                        if page.is_closed() or not title or not self._text_visible(page, title):
                            raise ActionFailedError("Search results page did not remain available")
                        search_inputs = page.locator("input[type=search], input[name=search]")
                        if not any(item.is_visible() and item.is_enabled() for item in search_inputs.all()):
                            raise ActionFailedError("Search page is no longer usable")
                    elif "after each add" in lower or "cart shows both products with correct price" in lower:
                        self._verify_added_cart(page, part, data)
                    elif "shows only" in lower and "products" in lower:
                        self._verify_filtered_listing(page, part, data)
                    elif "invalid email triggers email format validation" in lower and "submission is blocked" in lower:
                        self._verify_invalid_subscription()
                    elif "delivery address matches registration data" in lower:
                        if not self.checkout_address_verified or not self.checkout_order_verified:
                            raise ActionFailedError("Checkout address and order were not verified")
                    elif "download invoice" in lower and "available" in lower:
                        links = [
                            item for item in self._interactive_matches(page, "Download Invoice")
                            if item.evaluate("el => el.tagName.toLowerCase() === 'a'")
                            and item.get_attribute("href")
                        ]
                        if len(links) != 1:
                            raise ActionFailedError("Download Invoice link is not available")
                    else:
                        matched = False
                    behavior_verified = behavior_verified or matched
                except UnsupportedStepError as exc:
                    status, reason = "needs_review", str(exc)
                except ActionFailedError as exc:
                    status, reason = "failed", str(exc)
            complex_claim = re.search(
                r"within acceptable time|\bevery\b|\bonly\b|\bcorrect\b|\bmatches\b|"
                r"count matches|\btotal\b|no products listed|lists products|"
                r"after each add|no error/crash|delivery address",
                lower,
            )
            if status == "passed" and complex_claim and not behavior_verified:
                status, reason = "needs_review", f"Assertion is not automated: {complex_claim.group()}"
            if status == "passed" and not literals and not any(
                phrase in lower for phrase in (
                    "required-field validation", "html5", "not logged in", "logged out",
                    "no account is created", "user is logged in", "stays on signup page",
                    "redirected to the login page", "form is not submitted",
                    "returns user to home page", "form fields are cleared",
                    "product is removed from the cart", "checkout page is not opened",
                )
            ) and not behavior_verified:
                status, reason = "needs_review", "No deterministic assertion for this text"
            checks.append({"expectation": part, "status": status, "reason": reason})
        return checks

    def run_case(self, page, test_case: dict) -> dict:
        self.generic.reset()
        self.last_form = None
        self.last_field = None
        self.last_submit_url = None
        self.signup_heading = None
        self.review_notes = []
        self.cart_products = []
        self.removed_cart_products = []
        self.add_feedback = []
        self.listings = {}
        self.product_list_verified = False
        self.home_load_seconds = None
        self.brand_sidebar_count = None
        self.invalid_subscription = None
        self.cart_snapshot = {}
        self.checkout_address_verified = False
        self.checkout_order_verified = False
        data = self._prepare_data(test_case.get("data"))
        steps = split_steps(test_case.get("step"))
        expected = str(test_case.get("expected") or "")
        result = {"status": "needs_review", "steps": [], "checks": [], "url": None}
        if not steps:
            result["error"] = "Test Step is empty"
            return result
        observed: set[str] = set()
        self._active_expected = expected
        self._active_observed = observed
        try:
            response = page.goto(self.base_url, wait_until="load", timeout=self.timeout_ms)
            if response is None or response.status >= 400:
                raise ActionFailedError(f"Test site did not load (HTTP {response.status if response else 'none'})")
            self._raise_if_origin_error(page)
            self._observe_expected(page, expected, observed)
            for number, step in enumerate(steps, 1):
                try:
                    detail = self._execute_step(page, step, data)
                    page.wait_for_load_state("load", timeout=self.timeout_ms)
                    self._raise_if_origin_error(page)
                    result["steps"].append({"number": number, "text": step, "status": "passed", "detail": detail})
                    self._observe_expected(page, expected, observed)
                except UnsupportedStepError as exc:
                    result["steps"].append({"number": number, "text": step, "status": "needs_review", "error": str(exc)})
                    result["error"] = str(exc)
                    break
                except Exception as exc:
                    result["steps"].append({"number": number, "text": step, "status": "failed", "error": str(exc)})
                    result["status"] = "failed"
                    result["error"] = str(exc)
                    break
            else:
                result["checks"] = self._verify_expected(page, expected, observed, data)
                if any(check["status"] == "failed" for check in result["checks"]):
                    result["status"] = "failed"
                elif any(check["status"] == "needs_review" for check in result["checks"]) or self.review_notes:
                    result["status"] = "needs_review"
                else:
                    result["status"] = "passed"
            if result["status"] in {"passed", "needs_review"} and "ACCOUNT CREATED!" in observed:
                if data.get("email") and data.get("password"):
                    self.registered_account = {"email": data["email"], "password": data["password"]}
            if result["status"] == "passed" and "ACCOUNT DELETED!" in observed:
                self.registered_account = None
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)
        result["url"] = page.url if not page.is_closed() else None
        if self.review_notes:
            result["notes"] = self.review_notes.copy()
        return result
