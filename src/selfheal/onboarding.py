"""Convert uploaded suites, discover site controls, and run cached browser tests."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import typer
import yaml

from selfheal.config import load_app_config, load_environment
from selfheal.executor.browser import split_steps
from selfheal.executor.generic import ACTION_COMMANDS, GenericStepExecutor
from selfheal.healing.cache import LocatorCache
from selfheal.healing.locator import verified_locator
from selfheal.llm.openai_client import DEFAULT_OPENAI_MODEL, OpenAILLMClient, OpenAIQuotaError
from selfheal.runner import run_browser_suite
from selfheal.site_policy import blocked_resource_hosts, install_blocked_resource_routes
from selfheal.telemetry.langsmith import LangSmithTracker
from selfheal.time_utils import now_ist

DISCOVER_COMMANDS = ACTION_COMMANDS | {
    "ASSERT_VISIBLE", "ASSERT_TEXT", "ASSERT_VALUE", "ASSERT_VALIDATION", "WAIT_VISIBLE",
}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedSite:
    profile_path: Path
    app_name: str
    discovery_calls: int


def normalize_site_url(value: str) -> str:
    """Accept an HTTP(S) site origin, not a credential-bearing or local file URL."""
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enter a website origin such as https://example.com (without a path or credentials)")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Website URL has an invalid port") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}{f':{port}' if port else ''}"


def site_name(url: str) -> str:
    """Keep site cache and generated profiles stable even for alternate ports."""
    host = re.sub(r"[^a-z0-9]+", "-", urlsplit(url).netloc.lower()).strip("-")[:45]
    return f"site-{host}-{hashlib.sha256(url.encode()).hexdigest()[:10]}"


def generated_profile_path(root: Path, url: str) -> Path:
    return root / "config" / "apps" / "generated" / f"{site_name(url)}.yaml"


def _direct_locator(reference: str) -> bool:
    return GenericStepExecutor._is_direct_reference(reference)


def _suite_targets(tests: list[dict[str, Any]], base_url: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    pages = {"/": base_url + "/"}
    targets: dict[tuple[str, str, str], dict[str, str]] = {}
    for case in tests:
        if str(case.get("expected") or "").strip():
            raise ValueError(
                f"{case.get('id')}: use explicit ASSERT_* steps and leave Expected Result blank"
            )
        path = "/"
        has_assertion = False
        for step in split_steps(case.get("step")):
            if not GenericStepExecutor.is_generic_step(step):
                raise ValueError(f"{case.get('id')}: unsupported step: {step}")
            parts = [part.strip() for part in step.split("|")]
            command = parts[0].upper()
            if command.startswith("ASSERT_"):
                has_assertion = True
            if command == "OPEN":
                if len(parts) < 2 or not parts[1] or "${" in parts[1]:
                    raise ValueError(f"{case.get('id')}: OPEN needs a literal same-site URL")
                destination = urljoin(base_url + "/", parts[1].lstrip("/"))
                parsed = urlsplit(destination)
                if f"{parsed.scheme}://{parsed.netloc}" != base_url:
                    raise ValueError(f"{case.get('id')}: OPEN must stay on the target website")
                path = parsed.path or "/"
                pages[path] = destination
            elif command in DISCOVER_COMMANDS:
                if len(parts) < 2 or not parts[1] or "${" in parts[1]:
                    raise ValueError(f"{case.get('id')}: {command} needs a literal target name")
                target = parts[1]
                if not _direct_locator(target):
                    key = (path, command, target.casefold())
                    request = {"page": path, "command": command, "target": target}
                    if command == "ASSERT_TEXT" and len(parts) > 2:
                        request["expected_text"] = parts[2]
                    targets[key] = request
            elif command in {"ASSERT_HIDDEN", "WAIT_HIDDEN"}:
                if len(parts) < 2 or not _direct_locator(parts[1]):
                    raise ValueError(
                        f"{case.get('id')}: {command} needs an explicit locator for an absent element"
                    )
        if not has_assertion:
            raise ValueError(f"{case.get('id')}: add an ASSERT_* step to verify the result")
    return pages, list(targets.values())


def _clicked_link_path(page: Any, reference: str, base_url: str) -> str | None:
    try:
        locator = (
            GenericStepExecutor._string_locator(page, reference)
            if _direct_locator(reference) else page.get_by_role("link", name=reference, exact=True)
        )
        links = [
            item for item in locator.all()
            if item.is_visible() and item.evaluate("el => el.tagName.toLowerCase()") == "a"
        ]
        if len(links) != 1:
            return None
        href = links[0].get_attribute("href")
        if not href:
            return None
        destination = urlsplit(urljoin(page.url, href))
        if f"{destination.scheme}://{destination.netloc}" != base_url:
            return None
        return destination.path or "/"
    except Exception:
        return None


def _discovery_requests(
    tests: list[dict[str, Any]], base_url: str, pages: dict[str, Any],
    open_page: Any,
) -> list[dict[str, str]]:
    """Associate each target with the page reached by preceding same-site links."""
    requests: dict[tuple[str, str, str], dict[str, str]] = {}
    for case in tests:
        path = "/"
        for step in split_steps(case.get("step")):
            parts = [part.strip() for part in step.split("|")]
            command = parts[0].upper()
            if command == "OPEN":
                path = urlsplit(urljoin(base_url + "/", parts[1].lstrip("/"))).path or "/"
                continue
            if command not in DISCOVER_COMMANDS or len(parts) < 2:
                continue
            if path not in pages:
                path = open_page(path) or path
            if path not in pages:
                continue
            reference = parts[1]
            if not _direct_locator(reference):
                key = (path, command, reference.casefold())
                request = {"page": path, "command": command, "target": reference}
                if command == "ASSERT_TEXT" and len(parts) > 2 and "${" not in parts[2]:
                    request["expected_text"] = parts[2]
                requests[key] = request
            if command == "CLICK":
                next_path = _clicked_link_path(pages[path], reference, base_url)
                if next_path:
                    path = next_path
    return list(requests.values())


def _visible_discovery_requests(
    requests: list[dict[str, str]], snapshots: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Defer controls absent from the initial DOM to runtime locator healing."""
    html_by_page = {item["page"]: item["html"].casefold() for item in snapshots}
    visible = []
    for request in requests:
        html = html_by_page.get(request["page"], "")
        anchor = request["target"].split(" in ", 1)[0].strip().casefold()
        anchor = re.sub(r"(?:\s+(?:submit|button|heading|message|link|field|input|label))+$", "", anchor)
        if len(anchor) > 1 and anchor in html:
            visible.append(request)
    return visible


def _rendered_html(page: Any) -> str:
    """Send visible controls with form context, never input values or scripts."""
    elements = page.evaluate(
        """() => Array.from(document.querySelectorAll(
            'a,button,input,select,textarea,label,h1,h2,h3,[role="button"]'
        )).filter(el => el.getClientRects().length).slice(0, 300).map(el => {
            const form = el.closest('form');
            const heading = form?.parentElement?.querySelector('h1,h2,h3');
            const attrs = Object.fromEntries(
                ['id','class','name','type','href','for','role','aria-label','placeholder']
                .filter(name => el.hasAttribute(name))
                .map(name => [name, el.getAttribute(name)])
            );
            return {
                tag: el.tagName.toLowerCase(),
                text: (el.textContent || '').trim().slice(0, 100),
                attrs,
                context: {
                    labels: Array.from(el.labels || []).map(label => label.textContent.trim()).join(' ').slice(0, 100),
                    form_id: form?.id || '',
                    form_class: String(form?.className || ''),
                    form_action: form?.getAttribute('action') || '',
                    form_heading: heading?.textContent.trim().slice(0, 100) || ''
                }
            };
        })"""
    )
    lines = []
    for element in elements:
        tag = element["tag"]
        attrs = "".join(
            f' {name}="{escape(str(value), quote=True)}"'
            for name, value in element["attrs"].items()
        )
        context = {key: value for key, value in element["context"].items() if value}
        prefix = f"<!-- context {escape(json.dumps(context))} --> " if context else ""
        lines.append(f"{prefix}<{tag}{attrs}>{escape(element['text'])}</{tag}>")
    return "\n".join(lines)[:30000]


def _conversion_pages(url: str, tests: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Read the home page and relevant same-site pages for upload review."""
    from playwright.sync_api import sync_playwright

    source_text = " ".join(f"{case['title']} {case['step']}" for case in tests)
    prose = source_text.casefold()
    paths = ["/"]
    for case in tests:
        for step in split_steps(case["step"]):
            command, separator, target = step.partition("|")
            if command.strip().upper() != "OPEN" or not separator:
                continue
            destination = target.strip()
            if not destination.startswith("/") or destination.startswith("//"):
                continue
            path = urlsplit(destination).path or "/"
            if path not in paths and len(paths) < 8:
                paths.append(path)
    for match in re.findall(r"https?://[^\s'\"]+", source_text, re.I):
        destination = urlsplit(match.rstrip(".,;)"))
        if f"{destination.scheme.casefold()}://{destination.netloc.casefold()}" == url:
            path = destination.path or "/"
            if path not in paths and len(paths) < 8:
                paths.append(path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chromium")
        try:
            context = browser.new_context()
            install_blocked_resource_routes(context, blocked_resource_hosts(url))
            home = context.new_page()
            response = home.goto(url + "/", wait_until="load", timeout=30000)
            if response is None or response.status >= 400:
                raise ValueError("Conversion could not load the target website")
            actual_home = urlsplit(home.url)
            if f"{actual_home.scheme}://{actual_home.netloc}" != url:
                raise ValueError("Conversion redirected off the target website")
            links = home.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]'))
                    .filter(el => el.getClientRects().length)
                    .map(el => ({text: (el.textContent || '').trim(), href: el.href}))"""
            )
            for link in links:
                label = " ".join(str(link["text"]).split()).casefold()
                destination = urlsplit(str(link["href"]))
                path = destination.path or "/"
                if (
                    label and len(label) >= 3 and label in prose
                    and f"{destination.scheme}://{destination.netloc}" == url
                    and path not in paths
                ):
                    paths.append(path)
                if len(paths) >= 8:
                    break
            snapshots = [{"page": "/", "html": _rendered_html(home)[:12000]}]
            for path in paths[1:8]:
                page = context.new_page()
                try:
                    response = page.goto(urljoin(url + "/", path.lstrip("/")), wait_until="load", timeout=30000)
                    actual = urlsplit(page.url)
                    if response is not None and response.status < 400 and f"{actual.scheme}://{actual.netloc}" == url:
                        snapshots.append({"page": actual.path or "/", "html": _rendered_html(page)[:12000]})
                except Exception:
                    continue
            return snapshots
        finally:
            browser.close()


def _validated_locators(
    suggestions: dict[str, Any], requests: list[dict[str, str]], pages: dict[str, Any],
    threshold: float = 0.85, *, allow_partial: bool = False,
) -> dict[str, dict[str, dict[str, str]]]:
    entries = suggestions.get("locators", [] if allow_partial else None)
    if not isinstance(entries, list):
        raise ValueError("OpenAI discovery did not return a locators list")
    expected_requests = {
        (item["page"], item["command"], item["target"].casefold()): item
        for item in requests
    }
    expected = set(expected_requests)
    found: dict[tuple[str, str, str], str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        page_path = str(entry.get("page") or "")
        command = str(entry.get("command") or "").upper()
        target = str(entry.get("target") or "")
        key = (page_path, command, target.casefold())
        if key not in expected:
            continue
        try:
            confidence = float(entry.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        selector = entry.get("selector")
        if confidence < threshold or not isinstance(selector, str) or not selector.strip():
            continue
        if page_path not in pages:
            continue
        verified = verified_locator(
            pages[page_path], selector, command, target,
            expected_text=expected_requests[key].get("expected_text"), allow_scope=True,
        )
        if verified is None:
            continue
        found[key] = verified[1]
    if not allow_partial and set(found) != expected:
        missing = sorted(f"{page} {command} {target}" for page, command, target in expected - set(found))
        raise ValueError("OpenAI could not verify these locators in the first-run HTML: " + ", ".join(missing))
    locators: dict[str, dict[str, dict[str, str]]] = {}
    for request in requests:
        page_path = request["page"]
        command = request["command"]
        target = request["target"]
        selector = found.get((page_path, command, target.casefold()))
        if selector:
            locators.setdefault(page_path, {}).setdefault(command, {})[target] = selector
    return locators


def _save_site_profile(
    root: Path, base_url: str, app_name: str, model: str,
    suggestions: dict[str, Any], locators: dict[str, dict[str, dict[str, str]]],
    requested_count: int, discovery_calls: int, target_fingerprint: str,
) -> PreparedSite:
    profile_path = generated_profile_path(root, base_url)
    previous_execution: dict[str, Any] = {}
    if profile_path.is_file():
        previous = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        if isinstance(previous, dict) and isinstance(previous.get("execution"), dict):
            previous_execution = previous["execution"]
    execution = {
        "browser": "chromium", "timeout_seconds": 30,
        "screenshot_on_pass": True, "screenshot_on_error": True,
        **previous_execution,
    }
    blocked = blocked_resource_hosts(
        base_url, execution.get("blocked_resource_host_suffixes") or [],
    )
    if blocked:
        execution["blocked_resource_host_suffixes"] = blocked
    cache_path = root / "data" / "store" / "elements.db"
    cache_entries = [
        (page, f"generic-{command.casefold()}:{target.casefold()}", selector)
        for page, commands in locators.items()
        for command, targets in commands.items()
        for target, selector in targets.items()
    ]
    LocatorCache(cache_path).merge_app(app_name, "prod", cache_entries)
    config = {
        "app_name": app_name,
        "dashboard": {"visible": True},
        "environments": {"prod": {"base_url": base_url}},
        "execution": execution,
        "llm": {"provider": "openai", "api_key_env": "OPENAI_API_KEY", "model": model},
        "resolver": {"ai_enabled": True, "discovery_mode": "batch_cached",
                     "confidence_threshold": 0.85},
        "healing": {"enabled": True, "auto_approve": True,
                    "confidence_threshold": 0.85, "max_heals_per_run": 50,
                    "cache_path": str(cache_path)},
        "discovery": {
            "schema_version": 4,
            "site_name": str(suggestions.get("site_name") or app_name)[:80],
            "requested_targets": requested_count,
            "cached_targets": len(cache_entries),
            "target_fingerprint": target_fingerprint,
        },
        "locators_by_page": locators,
    }
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = profile_path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
    temporary.replace(profile_path)
    return PreparedSite(profile_path, app_name, discovery_calls)


def prepare_site(
    url: str, tests: list[dict[str, Any]], *, root: Path, client: Any | None = None,
    allow_partial: bool = False, trace_run_id: str | None = None,
) -> PreparedSite:
    """Discover once, validate selectors, then persist a reusable site profile."""
    from playwright.sync_api import sync_playwright

    base_url = normalize_site_url(url)
    profile_path = generated_profile_path(root, base_url)
    app_name = site_name(base_url)
    page_urls, initial_requests = _suite_targets(tests, base_url)
    target_fingerprint = hashlib.sha256(
        json.dumps(initial_requests, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    profile_config: dict[str, Any] | None = None
    if profile_path.is_file():
        config = load_app_config(profile_path, "prod")
        profile_config = config
        if config.get("app_name") != app_name or config["environments"]["prod"]["base_url"] != base_url:
            raise ValueError("Generated profile does not match the requested website")
        if not tests:
            return PreparedSite(profile_path, app_name, 0)
        if not initial_requests:
            return PreparedSite(profile_path, app_name, 0)
        if (
            config.get("discovery", {}).get("schema_version") == 4
            and config.get("discovery", {}).get("target_fingerprint") == target_fingerprint
            and (config.get("discovery", {}).get("requested_targets", 0) or not initial_requests)
        ):
            cache_path = root / "data" / "store" / "elements.db"
            cache = LocatorCache(cache_path) if cache_path.is_file() else None
            cached = cache is not None and all(
                cache.get(app_name, "prod", page, f"generic-{command.casefold()}:{target.casefold()}")
                for page, commands in config.get("locators_by_page", {}).items()
                for command, targets in commands.items()
                for target in targets
            )
            if cached:
                return PreparedSite(profile_path, app_name, 0)

    load_environment(root / ".env")
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    if not initial_requests:
        return _save_site_profile(
            root, base_url, app_name, model, {}, {}, 0, 0, target_fingerprint,
        )
    cache_path = root / "data" / "store" / "elements.db"
    cache = LocatorCache(cache_path) if cache_path.is_file() else None
    suggestions: dict[str, Any] = {
        "site_name": (
            (profile_config or {}).get("discovery", {}).get("site_name") or app_name
        ),
    }
    discovery_calls = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chromium")
        try:
            context = browser.new_context()
            install_blocked_resource_routes(
                context,
                blocked_resource_hosts(
                    base_url,
                    (profile_config or {}).get("execution", {}).get("blocked_resource_host_suffixes") or [],
                ),
            )
            browser_pages: dict[str, Any] = {}
            snapshots: list[dict[str, str]] = []
            redirects: dict[str, str] = {}

            def open_page(requested_path: str) -> str | None:
                if requested_path in redirects:
                    return redirects[requested_path]
                if len(browser_pages) >= 12:
                    return None
                page = context.new_page()
                try:
                    destination = page_urls.get(requested_path) or urljoin(
                        base_url + "/", requested_path.lstrip("/"),
                    )
                    response = page.goto(destination, wait_until="load", timeout=30000)
                    if response is None or response.status >= 400:
                        raise ValueError(f"Discovery could not load {requested_path}")
                    actual = urlsplit(page.url)
                    if f"{actual.scheme}://{actual.netloc}" != base_url:
                        raise ValueError(f"Discovery redirected off the target website: {requested_path}")
                except Exception:
                    page.close()
                    if allow_partial and requested_path != "/":
                        return None
                    raise
                actual_path = actual.path or "/"
                redirects[requested_path] = actual_path
                if actual_path in browser_pages:
                    page.close()
                    return actual_path
                browser_pages[actual_path] = page
                snapshots.append({"page": actual_path, "html": _rendered_html(page)})
                return actual_path

            for requested_path in page_urls:
                open_page(requested_path)
            requested = _discovery_requests(tests, base_url, browser_pages, open_page)
            if allow_partial:
                requested = _visible_discovery_requests(requested, snapshots)
            cached_suggestions = {"locators": [
                {"page": request["page"], "command": request["command"],
                 "target": request["target"], "selector": selector, "confidence": 1.0}
                for request in requested
                if cache is not None and (selector := cache.get(
                    app_name, "prod", request["page"],
                    f"generic-{request['command'].casefold()}:{request['target'].casefold()}",
                ))
            ]}
            locators = _validated_locators(
                cached_suggestions, requested, browser_pages, allow_partial=True,
            )
            missing = [
                request for request in requested
                if request["target"] not in locators.get(request["page"], {}).get(request["command"], {})
            ]
            if missing:
                api_key = os.getenv("OPENAI_API_KEY", "").strip()
                if not api_key and client is None:
                    raise ValueError("OPENAI_API_KEY is not configured in the environment or .env")
                tracker = LangSmithTracker()
                llm = client or OpenAILLMClient(api_key, model, langsmith=tracker, max_retries=1)
                with tracker.case_context(
                    trace_run_id or "discovery", "DISCOVERY", "prod", app_name=app_name,
                ):
                    suggestions = llm.discover_site(snapshots, missing)
                try:
                    tracker.flush()
                except RuntimeError as exc:
                    LOGGER.warning("LangSmith discovery trace upload failed: %s", exc)
                discovered = _validated_locators(
                    suggestions, missing, browser_pages, allow_partial=allow_partial,
                )
                for page_path, commands in discovered.items():
                    for command, targets in commands.items():
                        locators.setdefault(page_path, {}).setdefault(command, {}).update(targets)
                discovery_calls = 1
        finally:
            browser.close()

    return _save_site_profile(
        root, base_url, app_name, model, suggestions, locators, len(requested), discovery_calls,
        target_fingerprint,
    )


def _convert_cases(
    llm: Any, pages: list[dict[str, str]], tests: list[dict[str, Any]],
    base_url: str, *, allow_site_actions: bool = False,
    one_call: bool = False, review_all: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    from selfheal.suite_converter import (
        SENSITIVE_ACTION,
        missing_time_limit,
        model_cases,
        needs_conversion,
        validate_conversion,
    )

    if one_call and tests:
        selected = tests if review_all else [case for case in tests if needs_conversion([case])]
        if selected:
            response = llm.convert_site(pages, model_cases(selected))
            proposals = response.get("cases")
            originals = {case["id"]: case for case in selected}
            explicit_ids = {
                case["id"] for case in selected
                if (steps := split_steps(case["step"]))
                and all(GenericStepExecutor.is_generic_step(step) for step in steps)
            } if review_all else set()
            if review_all and isinstance(proposals, list):
                for proposal in proposals:
                    source = originals.get(proposal.get("id")) if isinstance(proposal, dict) else None
                    if source and source["id"] in explicit_ids:
                        proposal["steps"] = split_steps(source["step"])
                        if not str(source["expected"] or "").strip():
                            proposal["checks"] = []
            converted = validate_conversion(
                selected, response, base_url, allow_site_actions=allow_site_actions,
            )
            for case in converted:
                if case["id"] in explicit_ids:
                    case["step"] = originals[case["id"]]["step"]
            by_id = {case["id"]: case for case in converted}
            return [
                by_id.get(case["id"], {
                    **case, "conversion_note": "", "execute_partial": False,
                    "original_step": case["step"], "original_expected": case["expected"],
                })
                for case in tests
            ], 1

    prose = [case for case in tests if needs_conversion([case])]
    proposals: dict[str, dict[str, Any]] = {}
    calls = 0
    for start in range(0, len(prose), 4):
        batch = prose[start:start + 4]
        calls += 1
        initial = llm.convert_site(pages, model_cases(batch))
        converted = validate_conversion(
            batch, initial, base_url, allow_site_actions=allow_site_actions,
        )
        proposals.update({item["id"]: item for item in initial["cases"]})
        retry_pairs = [
            (source, result) for source, result in zip(batch, converted, strict=True)
            if (
                (allow_site_actions or not SENSITIVE_ACTION.search(source["step"]))
                and not missing_time_limit(source["expected"])
                and (
                    not any(step.upper().startswith("ASSERT_") for step in split_steps(result["step"]))
                    or any(marker in (result["conversion_note"] or "") for marker in (
                        "unknown Test Data field", "Expected Result clause", "Expected Result coverage",
                        "quoted expected value", "quoted UI text", "Invalid command arguments",
                        "Mapped assertion", "needs ASSERT_", "needs an in-page", "not verify",
                        "snapshot-only metadata", "unsupported CSS",
                    ))
                )
            )
        ]
        if not retry_pairs:
            continue
        for source, original in retry_pairs:
            calls += 1
            try:
                retry_case = model_cases([source])[0]
                retry_case["validation_feedback"] = original["conversion_note"] or "Add a verifiable assertion"
                retry_case["previous_steps"] = split_steps(original["step"])
                retry_case["previous_checks"] = proposals[source["id"]].get("checks", [])
                retry = llm.convert_site(pages, [retry_case])
                result = validate_conversion(
                    [source], retry, base_url, allow_site_actions=allow_site_actions,
                )[0]
            except OpenAIQuotaError:
                raise
            except Exception as exc:
                LOGGER.warning("Focused conversion retry for %s was rejected: %s", source["id"], exc)
                continue
            if original["conversion_note"] and (
                not result["conversion_note"]
                or len(result["conversion_issues"]) < len(original["conversion_issues"])
                or (result["execute_partial"] and not original["execute_partial"])
            ):
                proposals[source["id"]] = retry["cases"][0]
    converted = validate_conversion(
        prose, {"cases": list(proposals.values())}, base_url,
        allow_site_actions=allow_site_actions,
    ) if prose else []
    by_id = {case["id"]: case for case in converted}
    return [
        by_id.get(case["id"], {
            **case, "conversion_note": "", "execute_partial": False,
            "original_step": case["step"], "original_expected": case["expected"],
        })
        for case in tests
    ], calls


def run_new_site(
    url: str, suite: Path, *, root: Path, headless: bool = True, client: Any | None = None,
    allow_site_actions: bool = False, retry_reviewed_cases: bool = False,
    review_upload: bool = False,
) -> Path:
    from selfheal.suite_converter import (
        converted_suite_path,
        incomplete_converted_suite,
        is_converted_suite,
        needs_conversion,
        recover_passed_conversions,
        remove_generated_execute_column,
        replace_reviewed_conversions,
        reuse_valid_conversions,
        review_cases,
        reviewed_attempts,
        save_converted_suite,
    )
    from selfheal.suite_loader import read_excel_tests

    if not suite.is_file():
        raise FileNotFoundError(f"Test suite not found: {suite}")
    tests = read_excel_tests(suite)
    reviews: list[dict[str, Any]] = review_cases(suite)
    if not tests and not reviews:
        raise ValueError("No enabled test cases found in the workbook")
    base_url = normalize_site_url(url)
    trace_run_id = "run_" + now_ist().strftime("%Y%m%d_%H%M%S_%f")
    converted_input = is_converted_suite(suite)
    if converted_input and (review_upload or incomplete_converted_suite(
        suite, tests, allow_site_actions=allow_site_actions,
    )):
        tests = read_excel_tests(suite, "Original Cases")
        if not tests:
            raise ValueError("The converted workbook has no original cases to repair")
    conversion_calls = 0
    source_path = suite
    requires_conversion = needs_conversion(tests) or review_upload
    if requires_conversion or (retry_reviewed_cases and converted_input):
        converted_path = (
            suite if converted_input and retry_reviewed_cases and not requires_conversion
            else converted_suite_path(root, base_url, suite, allow_site_actions=allow_site_actions)
        )

        def convert_selected(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal conversion_calls
            load_environment(root / ".env")
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key and client is None:
                raise ValueError("OPENAI_API_KEY is not configured in the environment or .env")
            tracker = LangSmithTracker()
            model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
            llm = client or OpenAILLMClient(api_key, model, langsmith=tracker, max_retries=1)
            pages = _conversion_pages(base_url, selected)
            with tracker.case_context(trace_run_id, "CONVERSION", "prod", app_name=site_name(base_url)):
                converted, calls = _convert_cases(
                    llm, pages, selected, base_url, allow_site_actions=allow_site_actions,
                    one_call=review_upload or retry_reviewed_cases,
                    review_all=review_upload or retry_reviewed_cases,
                )
            conversion_calls += calls
            try:
                tracker.flush()
            except RuntimeError as exc:
                LOGGER.warning("LangSmith conversion trace upload failed: %s", exc)
            return converted

        if review_upload or not converted_path.is_file():
            typer.echo(
                "Reviewing uploaded test cases with OpenAI..." if review_upload else
                "Converting uploaded test cases with OpenAI..."
            )
            converted = convert_selected(tests)
            save_converted_suite(
                suite, converted_path, converted, allow_site_actions=allow_site_actions,
            )
            if not converted_input and not review_upload:
                preserved = reuse_valid_conversions(
                    converted_path, source=source_path, base_url=base_url,
                    allow_site_actions=allow_site_actions,
                )
                if preserved:
                    typer.echo(f"Preserved {preserved} validated case(s) from an earlier conversion")
        elif retry_reviewed_cases:
            attempts = reviewed_attempts(converted_path)
            ids = set(attempts)
            original = read_excel_tests(converted_path, "Original Cases")
            pending = [{**case, **attempts[case["id"]]} for case in original if case["id"] in ids]
            if pending:
                typer.echo(f"Retrying {len(pending)} reviewed case(s) with OpenAI...")
                replace_reviewed_conversions(converted_path, convert_selected(pending))
        remove_generated_execute_column(converted_path)
        restored = 0
        if not review_upload:
            try:
                restored = recover_passed_conversions(
                    converted_path, root=root, base_url=base_url,
                    allow_site_actions=allow_site_actions,
                )
            except OSError as exc:
                LOGGER.warning("Could not restore earlier validated conversions: %s", exc)
        if restored:
            typer.echo(f"Restored {restored} previously passed case(s) from run history")
        reviews = review_cases(converted_path)
        suite = converted_path
        tests = read_excel_tests(suite)
        converted_input = True
        typer.echo(f"Running converted suite: {suite}")
    typer.echo("Loading cached locators or discovering this website with OpenAI...")
    prepared = prepare_site(
        base_url, tests, root=root, client=client,
        allow_partial=converted_input or bool(reviews) or conversion_calls > 0,
        trace_run_id=trace_run_id,
    )
    typer.echo("Starting browser tests...")
    return run_browser_suite(
        tests, suite, "prod", headless, prepared.profile_path,
        output_root=root / "data" / "runs",
        history_path=root / "data" / "logs" / "run_history.xlsx",
        discovery_calls=prepared.discovery_calls,
        conversion_calls=conversion_calls,
        review_cases=reviews,
        allow_runtime_discovery=True,
        run_id=trace_run_id,
    )
