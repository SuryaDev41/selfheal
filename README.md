# SelfHeal

SelfHeal runs spreadsheet test cases against websites in a real Playwright browser. The dashboard accepts a website URL and an XLSX workbook; it can review natural-language cases with OpenAI, create a separate executable copy, discover page locators, and reuse verified selectors on later runs. A test passes only when its browser steps and assertions succeed.

## Start

From the project folder in PowerShell (Python 3.11 or newer):

```powershell
python -m venv my.venv
.\my.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m playwright install chromium
python -m selfheal.cli doctor
python -m streamlit run src/selfheal/dashboard/app.py
```

Put `OPENAI_API_KEY` in the Git-ignored `.env` file before onboarding a site. Open the URL printed by Streamlit, usually `http://localhost:8501`. Enter the website **origin** (for example, `https://example.com`), upload an `.xlsx` file, and click **Discover and Run**. The test browser is visible by default. See [SETUP.md](SETUP.md) for workbook format, CLI commands, and troubleshooting.

## How Runs Work

1. **Upload review:** Each dashboard **Discover and Run** starts one workbook-level OpenAI review/conversion request. The upload stays unchanged. A separate copy with executable steps and review notes is saved under `suites/converted/`.
2. **Locator discovery:** The framework inspects rendered pages, asks OpenAI for missing locators when needed, validates suggested selectors in the browser, and saves a site profile under `config/apps/generated/` and selectors in `data/store/elements.db`.
3. **Execution:** Playwright runs each case in a fresh browser context. Failed or stale locators may trigger a separate healing call; a repair is cached only after the browser action or assertion succeeds. An assertion mismatch is not healed into a pass.
4. **Repeat runs:** **Discover and Run** reviews the uploaded workbook again but reuses valid cached selectors. After a completed browser run, **Configured Websites > Rerun Saved Suite** runs the saved copy without another upload review. It can still heal a stale locator.

OpenAI calls are conditional: discovery can be zero when selectors are already cached, and healing can be zero when none fail. Direct selectors can also execute without a cache hit. `PASSED` means the executable checks succeeded; `FAILED` means a browser action or assertion failed; `NEEDS_REVIEW` means the case could not be fully and reliably verified. A review case is never counted as passed.

## Project Map

| Path | Purpose |
| --- | --- |
| `src/selfheal/dashboard/` | Streamlit UI, run history, and cache views |
| `src/selfheal/onboarding.py`, `suite_converter.py` | New-site discovery and workbook conversion |
| `src/selfheal/executor/`, `healing/` | Browser actions, locator resolution, and cache |
| `src/selfheal/llm/`, `telemetry/` | OpenAI requests and optional LangSmith tracing |
| `tests/` | Framework regression tests |
| `data/`, `suites/converted/`, `config/apps/generated/` | Runtime-generated uploads, runs, histories, cache, and profiles (Git-ignored) |

The repository does not require a bundled website profile. Clearing generated data removes saved suites, profiles, history, and locator cache; onboard the site again to restore them. Keep credentials out of tracked files.
