"""Command-line interface for spreadsheet-driven browser tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from .config import DEFAULT_CONFIG, load_environment
from .runner import run_browser_suite as _run_browser_suite
from .suite_loader import read_excel_tests
from .telemetry import LangSmithTracker, get_total_usage, init_local_store

load_environment()
app = typer.Typer(help="Self-Healing Test Automation Framework")


def run_browser_suite(*args: Any, **kwargs: Any) -> Path:
    """Run a suite while keeping the CLI tracker injectable for tests."""
    kwargs.setdefault("tracker_factory", LangSmithTracker)
    return _run_browser_suite(*args, **kwargs)

@app.command()
def run(
    suite: Annotated[Path, typer.Argument(help="Path to test suite Excel file")],
    env: Annotated[str, typer.Option(help="Configured environment")] = "prod",
    config: Annotated[Path, typer.Option(help="App YAML config")] = DEFAULT_CONFIG,
    headless: Annotated[bool, typer.Option(help="Run browser without a visible window")] = False,
    verbose: Annotated[bool, typer.Option(help="Print each executed step")] = False,
    case: Annotated[list[str] | None, typer.Option("--case", help="TestCase ID to run; repeat for more IDs")] = None,
) -> None:
    """Execute spreadsheet tests in a real browser."""
    try:
        if not suite.is_file():
            raise FileNotFoundError(f"Test suite not found: {suite}")
        tests = read_excel_tests(suite)
        if case:
            selected = {item.casefold() for item in case}
            enabled = {item["id"].casefold() for item in tests}
            unknown = selected - enabled
            if unknown:
                raise ValueError("Unknown or disabled TestCase ID: " + ", ".join(sorted(unknown)))
            tests = [item for item in tests if item["id"].casefold() in selected]
        if not tests:
            raise ValueError("No test cases are enabled in the suite")
        run_dir = run_browser_suite(tests, suite, env, headless, config, verbose)
        results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, ImportError) as exc:
        typer.echo(f"Cannot run suite: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Results: {run_dir / 'results.json'}")
    typer.echo(
        f"Passed: {results['passed']}  Failed: {results['failed']}  "
        f"Needs review: {results['needs_review']}"
    )
    if results["status"] != "passed":
        raise typer.Exit(1)


@app.command()
def doctor() -> None:
    """Check dependencies and the Playwright browser installation."""
    typer.echo(f"Python {sys.version.split()[0]}")
    for module in ("playwright", "openpyxl", "yaml", "typer", "dotenv", "langsmith"):
        try:
            __import__(module)
            typer.echo(f"{module}: installed")
        except ImportError:
            typer.echo(f"{module}: missing")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.exists():
                typer.echo(f"Chromium: missing ({executable})")
                return
            browser = playwright.chromium.launch(headless=True, channel="chromium")
            browser.close()
            typer.echo(f"Chromium: launchable ({executable})")
    except Exception as exc:
        typer.echo(f"Chromium check failed: {exc}")


@app.command()
def telemetry() -> None:
    """Show where current LLM traces are recorded."""
    try:
        langsmith = LangSmithTracker()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if langsmith.enabled:
        typer.echo(f"LangSmith project: {langsmith.project}")
        typer.echo("View LLM calls and token usage at https://smith.langchain.com")
        return

    init_local_store()
    usage = get_total_usage()
    typer.echo("LangSmith tracing is off; these are local fallback database totals.")
    typer.echo(f"API calls: {usage['call_count']}")
    typer.echo(f"Tokens: {usage['total_tokens']}")
    typer.echo(f"Cost: ${usage['total_cost']:.6f}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
