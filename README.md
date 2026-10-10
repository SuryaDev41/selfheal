# Selfheal

Selfheal runs spreadsheet-driven browser tests with Playwright. It uses semantic
Playwright locators first and can recover a broken selector through a verified
SQLite cache and OpenAI fallback.

## Quick Start

```powershell
.\my.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
python -m playwright install chromium
selfheal doctor
python -m selfheal.cli run suites/tests.xlsx --env prod
```

Keep credentials and API keys in the ignored `.env` file. See `SETUP.md` for
account setup, LangSmith tracing, and safe demo-order requirements.

## Project Layout

```text
config/apps/       Application and execution settings
suites/            Spreadsheet suites and local upload fixtures
src/selfheal/
  config.py        Environment and YAML configuration loading
  suite_loader.py  Excel workbook parsing
  runner.py        Browser suite orchestration and result files
  executor/        Spreadsheet-step execution in Playwright
  healing/         Locator cache and AI selector recovery
  llm/             OpenAI integration and provider helpers
  telemetry/       LangSmith tracing and local fallback totals
tests/             Offline browser, healing, and telemetry tests
```

## Healing Lifecycle

1. By default, the runner tries configured or semantic Playwright locators,
   then a verified SQLite cache entry, then the configured OpenAI model.
2. With `resolver.discovery_mode: ai_first`, actionable targets use the cache
   first and call the model on a cache miss or stale selector, before trying
   configured or semantic locators. DemoWebShop uses this mode.
3. An AI selector must be unique, visible, and above the confidence threshold.
   It is cached only after approval and a successful browser action.
4. Assertions are evaluated against the real browser state, never AI output.

`healing.auto_approve` is `false` by default. This reports a new AI selector
for review rather than automatically using it on a live site.
