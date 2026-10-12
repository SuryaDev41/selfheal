# Setup and Usage

## 1. Install

Use Python 3.11 or newer. In PowerShell, from the repository root:

```powershell
python -m venv my.venv
.\my.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m playwright install chromium
python -m selfheal.cli doctor
```

Skip the first command if `my.venv` already exists. `doctor` checks the Python dependencies and whether Playwright can launch Chromium. For framework tests, install the dev dependencies with `python -m pip install -e ".[dev]"`.

## 2. Configure Keys

Create or edit `.env` in the project root. It is Git-ignored. Use your own key values; do not put them in YAML, workbooks, screenshots, or commits.

```dotenv
OPENAI_API_KEY=your-openai-api-key

# Optional overrides; the code defaults to gpt-6-sol and max reasoning.
# OPENAI_MODEL=gpt-6-sol
# OPENAI_REASONING_EFFORT=xhigh

# Optional LangSmith tracing and dashboard call history.
# LANGSMITH_TRACING=true
# LANGSMITH_API_KEY=your-langsmith-api-key
# LANGSMITH_PROJECT=selfheal_framework
```

The framework uses the configured OpenAI key for workbook review, locator discovery, and healing. If the API rejects `max` reasoning with a supported `xhigh` alternative, the client retries with `xhigh`. Shell environment variables take precedence over `.env`; restart the dashboard after changing settings. LangSmith records LLM traces but does not select locators. Its history view needs access to `api.smith.langchain.com:443`.

## 3. Run the Dashboard

```powershell
python -m streamlit run src/selfheal/dashboard/app.py
```

Open the local address printed in the terminal, normally `http://localhost:8501`.

1. Enter a website origin such as `https://example.com`. Do not include a page path, query, credentials, or fragment.
2. Upload a `.xlsx` workbook (maximum 5 MB). Enable the site-action authorization checkbox only when you intend to submit the workbook's test data or perform account/order actions on that site.
3. Leave **Headless** off to watch browser execution, then click **Discover and Run**. Conversion and discovery may inspect pages in a background browser before the visible test browser starts.
4. Read the status output. Download the converted suite to inspect generated commands and any `Conversion Note`. Use **Run Details** for steps, checks, screenshots, and cached selectors used by each case.
5. After a completed browser run, use **Configured Websites > Rerun Saved Suite** to rerun its saved workbook without another upload review. **Cache History** lists stored selectors; **LLM Call History** reads the configured LangSmith project.

Each **Discover and Run** click requests one workbook-level OpenAI review. Locator discovery and self-healing are separate, conditional calls. A repeat upload is reviewed again; a saved-suite rerun skips that review and uses the generated profile and locator cache. A cached selector must still work on the live page.

## 4. Prepare a Workbook

The active sheet needs these exact column headers: `TestCase ID`, `Title`, `Test Step`, `Test Data`, `Expected Result`. Put one case in each row. Numbered or unnumbered steps may occupy separate lines in one `Test Step` cell.

Natural-language steps and expected results are accepted for conversion, but the converter cannot invent credentials, files, numeric limits, or proof of a result. It saves an executable copy with an `Original Cases` sheet and notes for cases needing review. The uploaded workbook is not changed. Cases with incomplete checks, missing test data, or unsupported outcomes remain `NEEDS_REVIEW`.

For hand-written executable cases, put one pipe-separated command per line in `Test Step`, leave `Expected Result` empty, and include at least one `ASSERT_*` command. For example, on `https://example.com`:

```text
OPEN | /
ASSERT_TITLE | Example Domain
```

`Test Data` accepts one `key: value` per line. Reference a value with `${DATA:key}` or a shell/`.env` variable with `${ENV:NAME}`. For example, `FILL | Email | ${DATA:email}`. The runner creates a **fresh browser context for every case**, so login-dependent cases need their own login steps or data. Placeholder values such as `<registered email>` are not usable test credentials. For generated uploads, give `File` an existing absolute path on the machine running the dashboard; hand-written `UPLOAD` also supports a path relative to the workbook's folder.

Supported commands include:

| Purpose | Commands |
| --- | --- |
| Navigation and actions | `OPEN`, `CLICK`, `FILL`, `CLEAR`, `SELECT`, `CHECK`, `UNCHECK`, `PRESS`, `HOVER`, `UPLOAD` |
| Waiting and visibility | `WAIT_VISIBLE`, `WAIT_HIDDEN`, `ASSERT_VISIBLE`, `ASSERT_HIDDEN` |
| Page and field checks | `ASSERT_TEXT`, `ASSERT_VALUE`, `ASSERT_VALIDATION`, `ASSERT_URL`, `ASSERT_URL_CONTAINS`, `ASSERT_URL_NOT_CONTAINS`, `ASSERT_TITLE` |
| Collection, numeric, and data checks | `ASSERT_COUNT`, `ASSERT_EACH_TEXT_CONTAINS`, `ASSERT_COUNT_MATCHES`, `ASSERT_NUMBER_EQUALS`, `ASSERT_MULTIPLY`, `ASSERT_DATA_FIELDS`, `ASSERT_LOAD_TIME` |
| Browser dialogs | `WATCH_DIALOG`, `ASSERT_DIALOG` |

`OPEN` must stay on the target website. Use explicit CSS/XPath locators for collection and numeric assertions. `ASSERT_LOAD_TIME | 3000` checks the preceding `OPEN` against a 3,000 ms limit; the converter will not choose a limit for "acceptable time." For an alert, use `WATCH_DIALOG | accept` before the triggering action, then `ASSERT_DIALOG | any` afterward. The generated suite must retain an assertion for each expected outcome, not merely a successful click.

If conversion produces review notes, correct missing data or unclear expectations in the original workbook and upload it again. **Retry Reviewed Cases** regenerates reviewed rows from their originals while preserving accepted rows. Do not clear notes or remove assertions just to make a case run.

## 5. Run from PowerShell

The dashboard-equivalent upload review and run is:

```powershell
python -m selfheal.cli onboard https://example.com "C:\path\to\tests.xlsx" --review-upload
```

Without `--review-upload`, `onboard` may reuse a prior converted copy. Use `--retry-reviewed` to retry review cases; add `--allow-site-actions` only for authorized site actions and `--headless` to hide the test browser.

To run a saved executable suite directly, supply its generated profile. This skips workbook review but may still heal a locator:

```powershell
python -m selfheal.cli run "suites\converted\site-name\tests.xlsx" --config "config\apps\generated\site-name.yaml" --env prod
```

Replace the sample paths with the actual paths printed after onboarding. `run` accepts `--case TC_001` (repeatable), `--headless`, and `--verbose`. It does not select a website automatically. A converted workbook's reviewed rows still appear as `NEEDS_REVIEW` in the result, even when only executable rows open a browser. The CLI exits nonzero unless the run fully passes.

## Results and Data

| Location | Contents |
| --- | --- |
| `data/uploads/` | Copies of dashboard uploads |
| `suites/converted/` | Separate executable/review workbooks |
| `config/apps/generated/` | Generated site YAML profiles |
| `data/store/elements.db` | Verified, site-scoped locator cache |
| `data/runs/run_*/results.json` | Per-run status, steps, checks, and LLM/cache counts |
| `data/runs/run_*/screenshots/{passed,failed,review}/` | Screenshots, when captured |
| `data/logs/run_history.xlsx` | Excel suite history |
| `data/dashboard/dashboard_history.xlsx` | Dashboard launch history |

These paths are generated and Git-ignored. Clearing them resets local dashboard history, saved-site reruns, and cache reuse; it does not delete remote LangSmith traces. Timestamps are recorded and shown in IST. A test is `PASSED` only if its browser steps and assertions pass, `FAILED` if an action or assertion fails, and `NEEDS_REVIEW` if the available data or checks cannot prove the expected result.

## Troubleshooting

- **Chromium will not open:** Run `python -m selfheal.cli doctor`. If the browser is missing, run `python -m playwright install chromium`. For TLS/certificate download errors, check the system clock, proxy, and trusted certificates; do not disable TLS verification.
- **OpenAI quota or key error:** Check `OPENAI_API_KEY`, the API project's billing/limits, and network access. A LangSmith key cannot pay for OpenAI calls.
- **Many cases need review:** Open the converted workbook's `Conversion Note` and `Original Cases`. Supply real test data and explicit, verifiable outcomes; more locator calls cannot repair missing inputs.
- **LangSmith logs are unavailable:** Check optional tracing settings and outbound access to `api.smith.langchain.com:443`. Disable `LANGSMITH_TRACING` if tracing is not needed.
- **Excel history cannot be written:** Close the history workbook in Excel and rerun; a lock can cause `Permission denied`.
- **Saved site is missing:** Generated profiles, suites, or run data may have been cleaned. Onboard the site again.

Run the framework regression tests with:

```powershell
python -m pytest tests -q
```
