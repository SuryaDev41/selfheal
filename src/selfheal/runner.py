"""Execute normalized spreadsheet cases in Playwright and persist run results."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import typer

from .config import DEFAULT_CONFIG, load_app_config, load_environment
from .executor.browser import BrowserExecutor
from .telemetry.langsmith import LangSmithTracker
from .telemetry.run_history import append_run_history
from .time_utils import now_ist


def write_results(path: Path, results: dict[str, Any]) -> None:
    """Recalculate suite totals and write the current run snapshot."""
    results["passed"] = sum(item["status"] == "passed" for item in results["tests"])
    results["failed"] = sum(item["status"] == "failed" for item in results["tests"])
    results["needs_review"] = sum(item["status"] == "needs_review" for item in results["tests"])
    results["status"] = (
        "failed"
        if results.get("run_error") or results["failed"]
        else "needs_review"
        if results["needs_review"] or len(results["tests"]) < results["total"]
        else "passed"
    )
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")


def run_browser_suite(
    test_cases: list[dict[str, Any]],
    suite: str | Path,
    env: str = "prod",
    headless: bool = False,
    config_path: str | Path = DEFAULT_CONFIG,
    verbose: bool = False,
    output_root: str | Path = Path("data/runs"),
    history_path: str | Path = Path("data/logs/run_history.xlsx"),
    tracker_factory: Callable[[], LangSmithTracker] = LangSmithTracker,
) -> Path:
    """Run all supplied cases and return the directory containing ``results.json``."""
    from playwright.sync_api import sync_playwright

    load_environment()
    config = load_app_config(config_path, env)
    langsmith = tracker_factory()
    browser_name = config.get("execution", {}).get("browser", "chromium")
    if browser_name not in {"chromium", "firefox", "webkit"}:
        raise ValueError(f"Unsupported browser in config: {browser_name}")

    run_id = "run_" + now_ist().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = Path(output_root) / run_id
    screenshots_dir = run_dir / "screenshots"
    screenshot_dirs = {
        "passed": screenshots_dir / "passed",
        "failed": screenshots_dir / "failed",
        "needs_review": screenshots_dir / "review",
    }
    for directory in screenshot_dirs.values():
        directory.mkdir(parents=True)
    results: dict[str, Any] = {
        "run_id": run_id,
        "suite": str(suite),
        "environment": env,
        "base_url": config["environments"][env]["base_url"],
        "browser": browser_name,
        "headless": headless,
        "started_at": now_ist().isoformat(),
        "total": len(test_cases),
        "langsmith_tracing": {
            "enabled": langsmith.enabled,
            "project": langsmith.project if langsmith.enabled else None,
        },
        "tests": [],
    }
    results_file = run_dir / "results.json"
    write_results(results_file, results)
    executor = BrowserExecutor(config, env, Path(suite), langsmith=langsmith)

    try:
        with sync_playwright() as playwright:
            launch_options = {"headless": headless}
            if browser_name == "chromium":
                launch_options["channel"] = "chromium"
            browser = getattr(playwright, browser_name).launch(**launch_options)
            try:
                for index, case in enumerate(test_cases, 1):
                    if index > 1:
                        time.sleep(float(config.get("execution", {}).get("delay_between_cases_seconds", 0)))
                    test_id = str(case.get("id") or f"TC_{index:03d}")
                    typer.echo(f"[{index}/{len(test_cases)}] {test_id}: {case.get('title', '')}")
                    context = browser.new_context()
                    for host in config.get("execution", {}).get("blocked_resource_host_suffixes", []):
                        context.route(
                            re.compile(rf"^https?://(?:[^/]+\.)?{re.escape(host)}(?::\d+)?/", re.I),
                            lambda route: route.abort(),
                        )
                    page = context.new_page()
                    executor.healer.set_context(run_id, test_id)
                    http_errors: list[dict[str, Any]] = []

                    def record_http_error(response, errors=http_errors):
                        if (
                            urlparse(response.url).hostname == urlparse(executor.base_url).hostname
                            and response.status >= 400
                            and response.request.resource_type
                            in {"document", "xhr", "fetch", "stylesheet", "font"}
                        ):
                            errors.append(
                                {
                                    "status": response.status,
                                    "method": response.request.method,
                                    "path": urlparse(response.url).path,
                                }
                            )

                    page.on("response", record_http_error)
                    try:
                        with langsmith.case_context(run_id, test_id, env):
                            result = executor.run_case(page, case)
                        take_screenshot = config.get("execution", {}).get(
                            "screenshot_on_pass"
                            if result["status"] == "passed"
                            else "screenshot_on_error",
                            True,
                        )
                        if take_screenshot and not page.is_closed():
                            safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", test_id)
                            screenshot = screenshot_dirs[result["status"]] / f"{index:03d}_{safe_id}.png"
                            try:
                                page.screenshot(path=str(screenshot), full_page=True, timeout=10000)
                                result["screenshot"] = str(screenshot)
                            except Exception as exc:
                                result["screenshot_error"] = str(exc)
                    except Exception as exc:
                        result = {"status": "failed", "error": str(exc), "steps": [], "checks": []}
                    finally:
                        context.close()

                    if http_errors:
                        result["http_errors"] = http_errors
                    result.update({"id": test_id, "title": case.get("title"), "row": case.get("row")})
                    results["tests"].append(result)
                    write_results(results_file, results)
                    unmet = next(
                        (check for check in result.get("checks", []) if check["status"] != "passed"),
                        None,
                    )
                    summary = result.get("error") or (unmet or {}).get("reason")
                    if summary is None:
                        summary = (
                            f"{len(result.get('steps', []))} steps, "
                            f"{len(result.get('checks', []))} checks verified"
                        )
                    typer.echo(f"  {result['status'].upper()}: {summary}")
                    if verbose:
                        for step in result.get("steps", []):
                            typer.echo(
                                f"    {step['number']}. {step['status']}: "
                                f"{step.get('detail') or step.get('error')}"
                            )
                        for check in result.get("checks", []):
                            typer.echo(
                                f"    check {check['status']}: "
                                f"{check['reason'] or check['expectation']}"
                            )
                        for note in result.get("notes", []):
                            typer.echo(f"    note: {note}")
            finally:
                browser.close()
    except Exception as exc:
        results["run_error"] = str(exc)
        typer.echo(f"Browser run failed: {exc}", err=True)
    finally:
        try:
            langsmith.flush()
        except Exception as exc:
            results["langsmith_error"] = str(exc)
            results.setdefault("run_error", "LangSmith trace upload failed")
            typer.echo(f"LangSmith trace upload failed: {exc}", err=True)
        results["finished_at"] = now_ist().isoformat()
        results["healing"] = executor.healer.stats()
        write_results(results_file, results)
        try:
            append_run_history(results, config, run_dir, history_path, results_root=output_root)
        except Exception as exc:
            results["history_error"] = str(exc)
            write_results(results_file, results)
            typer.echo(f"Excel run history update failed: {exc}", err=True)

    return run_dir
