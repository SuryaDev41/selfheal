# Selfheal Setup

## Install

In PowerShell, from the project folder:

```powershell
.\my.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
selfheal doctor
```

If `my.venv` does not exist, create it with `python -m venv my.venv` first.
If the doctor reports Chromium missing, run `python -m playwright install chromium`.
The runner uses the installed full Chromium browser, including in headless mode.

## Configure

Edit `config/apps/automationexercise_openai.yaml` for the base URL and
environment. Put the separate checkout account's `TEST_EMAIL` and
`TEST_PASSWORD` in the ignored `.env` file or your PowerShell session.
For TC_017, also set `TEST_ACCOUNT_NAME`, `TEST_ACCOUNT_ADDRESS`,
`TEST_ACCOUNT_CITY`, `TEST_ACCOUNT_STATE`, `TEST_ACCOUNT_ZIP`,
`TEST_ACCOUNT_COUNTRY`, and `TEST_ACCOUNT_MOBILE` to that account's
registration details so its delivery address can be checked. Put provider keys,
including `OPENAI_API_KEY`, in the same ignored `.env` file.

```powershell
$env:TEST_EMAIL = "your-registered-test-email"
$env:TEST_PASSWORD = "your-test-password"
```

Spreadsheet `Test Data` values are used as written. TC_002 registers a
timestamped disposable account; TC_003 through TC_008 reuse its email in
the same run, and TC_008 deletes it. TC_017 uses the separate account from
`.env`. Running TC_004, TC_007, or TC_008 alone requires credentials that
match those rows' literal workbook password.

## Run

```powershell
python -m selfheal.cli run suites/tests.xlsx --env prod
```

The browser is visible by default. Add `--headless` to run without a window.
Use `--case TC_014` to run one workbook row, or repeat `--case` to select
several. TC_014 is a public cart smoke test that needs no account.
Use `--env staging` or `--config path/to/app.yaml` for another configured
environment or app. Each enabled spreadsheet row runs in a fresh browser
context.

Results are written under `data/runs/run_*/results.json`, with screenshots
in the same run folder. Suite-level history is appended to
`data/logs/run_history.xlsx`, which can be opened in Excel and filtered by
run status, environment, or date. A case passes only when every step executes and its
automated assertions pass. Missing elements and failed assertions are
`failed`; ambiguous steps or outcomes that cannot be checked automatically
are `needs_review`. The command returns a nonzero exit code when any case is
not passed.
All newly recorded framework timestamps use India Standard Time (`Asia/Kolkata`);
the dashboard also displays older run, cache, healing, and LangSmith times in IST.

## Testing another website without Python changes

Copy `config/apps/generic_site_example.yaml`, change `base_url`, and define
stable page locators under `locators`. A locator can use YAML mappings with
`by: css`, `xpath`, `role`, `text`, `label`, `placeholder`, or `testid`. It
can also use string shorthand such as `css=#submit`, `text=Welcome`, or
`role=button|Sign in`.

Use explicit pipe-separated commands in the spreadsheet `Test Step` cell:

```text
OPEN | /login
FILL | email | ${ENV:TEST_EMAIL}
FILL | password | ${ENV:TEST_PASSWORD}
CLICK | sign_in
ASSERT_VISIBLE | dashboard_heading
ASSERT_TEXT | dashboard_heading | Welcome
ASSERT_URL | /dashboard
```

Supported commands are `OPEN`, `CLICK`, `FILL`, `CLEAR`, `SELECT`, `CHECK`,
`UNCHECK`, `PRESS`, `HOVER`, `UPLOAD`, `WAIT_VISIBLE`, `WAIT_HIDDEN`,
`ASSERT_VISIBLE`, `ASSERT_HIDDEN`, `ASSERT_TEXT`, `ASSERT_VALUE`,
`ASSERT_URL`, `ASSERT_URL_CONTAINS`, and `ASSERT_TITLE`. Use `${DATA:name}`
for a `Test Data` value, `${ENV:NAME}` for an environment variable, and
`${BASE_URL}` for the configured site URL. A blank `Expected Result` is valid
when the steps contain at least one `ASSERT_*` command.

Run a new site with its own config and spreadsheet:

```powershell
python -m selfheal.cli run suites\new_site_tests.xlsx --env prod --config config\apps\my_site.yaml --headless
```

The home-load limit is `verification.max_home_load_seconds` in the app config.
The registration title defaults to `registration.default_title` when the row
does not specify one. `suites/sample.txt` supplies the Contact Us upload.
`safety.no_real_payments` remains enabled: TC_017 can submit only on the
configured `safety.demo_orders.hosts` with an allow-listed test card from
the workbook. Disable `safety.demo_orders.enabled` to block demo orders.
The full suite creates and deletes a disposable account and submits a demo
order, Contact Us form, and product review on automationexercise.com.

## Self-healing

The default locator flow tries configured or semantic Playwright locators first,
then a cached selector, then AI. Set `resolver.discovery_mode: ai_first` in an
app profile to check the cache and ask AI on the first use of each actionable
target, before falling back to a configured or semantic locator. The
DemoWebShop profile uses this mode; other profiles keep the default flow.

The cache is stored at `healing.cache_path`, scoped to app, environment, page,
action, and target. A cached selector must still match one visible element. If
it goes stale, AI is asked again. New selectors must be unique, visible, and
above the confidence threshold, and are cached only after the browser action
succeeds. With `healing.auto_approve: false`, a valid AI proposal is reported
as `needs_review` instead of being used. Assertions still check the browser's
actual state and are never changed by AI.

## LangSmith usage tracking

To trace real OpenAI fallback calls, add these values to the ignored `.env`
file (or set them in PowerShell). Keep API keys out of the tracked app YAML.

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your-langsmith-api-key
LANGSMITH_PROJECT=selfheal
```

If your LangSmith workspace uses another region or you have multiple
workspaces, also set `LANGSMITH_ENDPOINT` or `LANGSMITH_WORKSPACE_ID` as
required by your LangSmith account. Restart the CLI after changing the env.
`python -m selfheal.cli telemetry` shows the configured project. View actual
model calls, token usage, and latency in that project's Tracing view at
https://smith.langchain.com. Each trace is tagged with the suite run ID,
test case ID, and environment. Prompts and responses are hidden; token usage
is retained. A deterministic test run may have no LLM calls and thus no
LangSmith traces. The run's `results.json` records whether LangSmith tracing
was enabled. When tracing is off, `selfheal telemetry` shows local fallback
database totals. OpenAI fallback calls also require an `OPENAI_API_KEY` with
available API credits.

## Dashboard

Install the updated requirements and start the local operations dashboard:

```powershell
python -m pip install -r requirements.txt
streamlit run src/selfheal/dashboard/app.py
```

Open the local URL shown by Streamlit (normally `http://localhost:8501`). The
sidebar discovers every YAML file in `config/apps` and every XLSX suite in
`suites`, allowing you to choose the website, environment, cases, and headless
mode before starting a run. The dashboard reads immutable run snapshots from
`data/runs`, lists verified locator-cache entries, records cache and AI recovery
events in `data/store/healing_events.db`, and fetches metadata-only LLM traces
from the configured LangSmith project. It never displays prompt or response
contents.
Dashboard-triggered launches are recorded in
`data/dashboard/dashboard_history.xlsx` with IST start and finish times,
selected profile, suite, cases, exit status, and console output.

## Regression tests

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests -q
```
