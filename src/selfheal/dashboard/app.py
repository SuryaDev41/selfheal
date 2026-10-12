"""Streamlit UI for running and observing SelfHeal browser suites."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import dotenv_values
from langsmith.utils import LangSmithAuthError, LangSmithConnectionError, LangSmithRequestTimeout

from selfheal.dashboard import data
from selfheal.dashboard.history import (
    finish_dashboard_run,
    migrate_legacy_logs,
    start_dashboard_run,
)
from selfheal.onboarding import normalize_site_url, site_name
from selfheal.suite_converter import converted_suite_path, review_cases
from selfheal.suite_loader import read_excel_tests
from selfheal.time_utils import now_ist

ROOT = data.project_root()


def _style() -> None:
    st.markdown(
        """<style>
        :root {
            --sh-canvas: #f3f6f4;
            --sh-surface: #ffffff;
            --sh-ink: #1e2924;
            --sh-muted: #5e7067;
            --sh-line: #d9e3dc;
            --sh-teal: #087d70;
            --sh-mint: #d9f0e8;
            --sh-blue: #2e6fa8;
            --sh-coral: #d95850;
            --sh-rose: #fbeae8;
            --sh-gold: #a76c17;
            --sh-violet: #795ca2;
        }
        [data-testid="stAppViewContainer"], [data-testid="stMain"] {
            background: var(--sh-canvas);
            color: var(--sh-ink);
        }
        [data-testid="stAppHeader"] {background: var(--sh-canvas);}
        .block-container {
            max-width: 1360px;
            padding-top: 1.5rem;
            padding-bottom: 2.5rem;
        }
        [data-testid="stSidebar"] {
            background: var(--sh-surface);
            border-right: 1px solid var(--sh-line);
        }
        [data-testid="stSidebarUserContent"] {padding-top: 1.8rem;}
        [data-testid="stSidebar"] h3 {
            color: var(--sh-ink);
            font-size: 1.15rem;
            padding-bottom: 0.7rem;
            border-bottom: 3px solid var(--sh-teal);
        }
        [data-testid="stMain"] h1 {
            color: var(--sh-ink);
            font-size: 2rem;
            line-height: 1.2;
            font-weight: 700;
        }
        [data-testid="stMain"] h3 {
            color: var(--sh-ink);
            font-size: 1.2rem;
            font-weight: 650;
        }
        [data-testid="stCaptionContainer"] {color: var(--sh-muted);}
        [data-testid="stMain"] [data-testid="stRadio"] [role="radiogroup"] {
            gap: 0.2rem;
            border-bottom: 1px solid var(--sh-line);
            margin-top: 0.5rem;
        }
        [data-testid="stMain"] label[data-testid="stRadioOption"] {
            padding: 0.6rem 0.8rem;
            border-bottom: 3px solid transparent;
            color: var(--sh-muted);
        }
        [data-testid="stMain"] label[data-testid="stRadioOption"] > div > div:first-child {
            display: none;
        }
        [data-testid="stMain"] label[data-testid="stRadioOption"][data-selected="true"] {
            color: var(--sh-teal);
            border-bottom-color: var(--sh-teal);
            background: var(--sh-mint);
        }
        [data-testid="stMain"] label[data-testid="stRadioOption"]:hover {
            color: var(--sh-blue);
        }
        [data-testid="stMain"] label[data-testid="stRadioOption"]:has(input:focus-visible) {
            outline: 2px solid var(--sh-gold);
            outline-offset: -2px;
        }
        [data-testid="stMetric"] {
            min-height: 112px;
            padding: 0.85rem 1rem;
            background: var(--sh-surface);
            border: 1px solid var(--sh-line);
            border-top: 3px solid var(--sh-blue);
            border-radius: 6px;
        }
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"])
            > [data-testid="stColumn"]:nth-child(2) [data-testid="stMetric"] {
            border-top-color: var(--sh-teal);
        }
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"])
            > [data-testid="stColumn"]:nth-child(3) [data-testid="stMetric"] {
            border-top-color: var(--sh-gold);
        }
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"])
            > [data-testid="stColumn"]:nth-child(4) [data-testid="stMetric"] {
            border-top-color: var(--sh-violet);
        }
        [data-testid="stMetricLabel"] {color: var(--sh-muted);}
        [data-testid="stMetricValue"] {color: var(--sh-ink);}
        [data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
            background: var(--sh-canvas);
            border: 1px dashed var(--sh-blue);
            border-radius: 6px;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"] {
            background: var(--sh-teal);
            border-color: var(--sh-teal);
            color: var(--sh-surface);
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"]:not(:disabled):hover {
            background: var(--sh-blue);
            border-color: var(--sh-blue);
        }
        [data-testid="stSidebar"] [data-testid="stButton"] button[kind="primary"]:disabled {
            background: var(--sh-line);
            border-color: var(--sh-line);
            color: var(--sh-muted);
        }
        [data-testid="stButton"] button:focus-visible,
        [data-testid="stDownloadButton"] button:focus-visible {
            outline: 2px solid var(--sh-gold);
            outline-offset: 2px;
        }
        [data-testid="stDataFrame"] {
            background: var(--sh-surface);
            border: 1px solid var(--sh-line);
            border-radius: 6px;
        }
        [data-testid="stAlert"] {border-radius: 6px;}
        [data-testid="stAlert"]:has([data-testid="stNotificationContentError"]) {
            background: var(--sh-rose);
            border-left: 3px solid var(--sh-coral);
        }
        @media (max-width: 760px) {
            .block-container {padding: 1rem 0.8rem 2rem;}
            [data-testid="stMain"] [data-testid="stRadio"] [role="radiogroup"] {
                flex-wrap: wrap;
            }
            [data-testid="stMain"] label[data-testid="stRadioOption"] {
                padding: 0.55rem 0.6rem;
            }
            [data-testid="stMetric"] {
                min-height: 76px;
                padding: 0.55rem 0.8rem;
            }
            [data-testid="stMetricValue"] {font-size: 1.5rem;}
        }
        </style>""",
        unsafe_allow_html=True,
    )


def _tail(path: Path, lines: int = 80) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _launch_command(
    command: list[str], *, website: str, suite: str, environment: str,
    headless: bool, case_ids: list[str],
) -> None:
    dashboard_dir = ROOT / "data" / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    launch_id = f"launch_{now_ist():%Y%m%d_%H%M%S_%f}"
    output_path = Path(tempfile.gettempdir()) / f"selfheal_{launch_id}.log"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    start_dashboard_run(
        {
            "launch_id": launch_id,
            "started_at": now_ist(),
            "website": website,
            "suite": suite,
            "environment": environment,
            "headless": headless,
            "cases": case_ids,
        },
        dashboard_dir / "dashboard_history.xlsx",
    )
    with output_path.open("w", encoding="utf-8") as log_file:
        child_env = os.environ.copy()
        current_settings = dotenv_values(ROOT / ".env")
        for key in ("OPENAI_MODEL", "OPENAI_REASONING_EFFORT"):
            if current_settings.get(key):
                child_env[key] = str(current_settings[key])
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=child_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    st.session_state["active_run"] = {
        "process": process,
        "launch_id": launch_id,
        "output_path": output_path,
        "history_path": dashboard_dir / "dashboard_history.xlsx",
        "recorded": False,
    }


def _save_uploaded_suite(url: str, upload) -> Path:
    content = upload.getvalue()
    if not content or len(content) > 5 * 1024 * 1024 or not upload.name.lower().endswith(".xlsx"):
        raise ValueError("Upload a valid .xlsx file no larger than 5 MB")
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(upload.name).stem).strip("-")[:40] or "tests"
    digest = hashlib.sha256(content).hexdigest()[:12]
    path = ROOT / "data" / "uploads" / site_name(url) / f"{name}-{digest}.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if not read_excel_tests(path):
        raise ValueError("The workbook has no enabled test cases")
    return path


def _run_launcher() -> None:
    st.sidebar.subheader("Run Test")
    active = st.session_state.get("active_run")
    running = bool(active and active["process"].poll() is None)
    raw_url = st.sidebar.text_input("Website URL", placeholder="https://example.com")
    upload = st.sidebar.file_uploader("Test cases (.xlsx)", type=["xlsx"])
    allow_site_actions = st.sidebar.checkbox(
        "I authorize uploaded test data and account/order actions on this test site"
    )
    retry_available = False
    if raw_url and upload is not None:
        try:
            url = normalize_site_url(raw_url)
            uploaded_path = _save_uploaded_suite(url, upload)
            converted_path = converted_suite_path(
                ROOT, url, uploaded_path, allow_site_actions=allow_site_actions,
            )
            if converted_path.is_file():
                count = len(review_cases(converted_path)) + sum(
                    bool(case.get("conversion_note")) for case in read_excel_tests(converted_path)
                )
                retry_available = count > 0
                st.sidebar.caption(f"Converted suite: {count} case(s) need review")
                st.sidebar.download_button(
                    "Download Converted Suite", converted_path.read_bytes(),
                    file_name=converted_path.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
        except (ValueError, OSError):
            pass
    headless = st.sidebar.toggle("Headless", value=False)
    run_clicked = st.sidebar.button(
        "Discover and Run", type="primary", disabled=running or not raw_url or upload is None,
        use_container_width=True,
    )
    retry_clicked = st.sidebar.button(
        "Retry Reviewed Cases", disabled=running or not retry_available,
        help="Regenerate only cases needing review, then run the suite",
        use_container_width=True,
    )
    if run_clicked or retry_clicked:
        try:
            url = normalize_site_url(raw_url)
            suite = _save_uploaded_suite(url, upload)
            command = [sys.executable, "-m", "selfheal.cli", "onboard", url, str(suite)]
            if not headless:
                command.append("--no-headless")
            if allow_site_actions:
                command.append("--allow-site-actions")
            if retry_clicked:
                command.append("--retry-reviewed")
            else:
                command.append("--review-upload")
            _launch_command(
                command, website=site_name(url), suite=suite.name,
                environment="prod", headless=headless, case_ids=[],
            )
            st.rerun()
        except Exception as exc:
            st.sidebar.error(f"Could not prepare website: {exc}")

    active = st.session_state.get("active_run")
    if active:
        process = active["process"]
        state = "Running" if process.poll() is None else f"Finished (exit {process.returncode})"
        if process.poll() is not None and not active["recorded"]:
            output = _tail(active["output_path"], lines=1000)
            finish_dashboard_run(
                active["launch_id"],
                exit_code=process.returncode,
                output=output,
                path=active["history_path"],
            )
            active["output"] = output
            try:
                active["output_path"].unlink(missing_ok=True)
            except OSError:
                pass
            active["recorded"] = True
        st.sidebar.caption(state)
        st.sidebar.code(
            active.get("output") or _tail(active["output_path"]) or "Waiting for runner output...",
            language="text",
        )
        if process.poll() is None and st.sidebar.button("Refresh Run Status"):
            st.rerun()


def _overview(runs: list[dict]) -> None:
    total_cases = sum(run["total"] for run in runs)
    passed_cases = sum(run["passed"] for run in runs)
    review_cases_count = sum(run["needs_review"] for run in runs)
    llm_calls = sum(run["llm_calls"] for run in runs)
    columns = st.columns(4)
    columns[0].metric("Runs", len(runs))
    columns[1].metric("Pass Rate", f"{(passed_cases / total_cases * 100) if total_cases else 0:.1f}%")
    columns[2].metric("Needs Review", review_cases_count)
    columns[3].metric("LLM Calls", llm_calls)
    st.subheader("Recent Runs")
    if runs:
        st.dataframe(runs, hide_index=True, width="stretch", column_order=[
            "started_at", "run_id", "status", "suite", "environment", "total", "passed", "failed",
            "needs_review", "llm_calls",
        ])
    else:
        st.info("No completed runs yet.")


def _configured_websites() -> None:
    sites = data.configured_sites(ROOT)
    if not sites:
        st.info("No website has a completed browser run and saved suite yet.")
        return
    selected = st.selectbox("Website", sites, format_func=lambda site: site["url"])
    st.caption(
        f"Suite: {selected['suite'].name} | Last run: {selected['last_run']} "
        f"({selected['passed']}/{selected['total']} passed)"
    )
    try:
        cases = read_excel_tests(selected["suite"])
        reviewed = review_cases(selected["suite"])
    except (OSError, ValueError) as exc:
        st.error(f"Could not read the saved suite: {exc}")
        return
    case_ids = list(dict.fromkeys(case["id"] for case in [*cases, *reviewed]))
    selected_cases = st.multiselect("Cases", case_ids, placeholder="All enabled cases")
    headless = st.toggle("Headless", value=False, key="configured_headless")
    active = st.session_state.get("active_run")
    running = bool(active and active["process"].poll() is None)
    if st.button("Rerun Saved Suite", type="primary", disabled=running):
        command = [
            sys.executable, "-m", "selfheal.cli", "run", str(selected["suite"]),
            "--env", "prod", "--config", str(selected["profile"]),
        ]
        if headless:
            command.append("--headless")
        for case_id in selected_cases:
            command.extend(["--case", case_id])
        _launch_command(
            command, website=selected["name"], suite=selected["suite"].name,
            environment="prod", headless=headless, case_ids=selected_cases,
        )
        st.rerun()

    cached = data.load_cache_entries(ROOT, limit=None, app=selected["name"])
    st.subheader("Saved Selectors")
    if cached:
        st.dataframe(
            cached, hide_index=True, width="stretch",
            column_order=["page", "target", "locator", "created_at"],
        )
    else:
        st.info("No verified selectors are cached for this website.")


def _run_details(runs: list[dict]) -> None:
    if not runs:
        st.info("No completed runs are available yet.")
        return
    selected_id = st.selectbox("Run", [run["run_id"] for run in runs])
    details = data.load_run_details(selected_id, ROOT)
    if details is None:
        st.error("The selected result file is unavailable.")
        return
    st.caption(f"{details.get('suite', '')} | {details.get('base_url', '')}")
    st.caption(f"Healing AI calls: {details.get('healing', {}).get('ai_calls', 0)}")
    for test in details.get("tests", []):
        title = f"{test.get('id', '')}  {test.get('status', '').upper()}  {test.get('title', '')}"
        with st.expander(title):
            if test.get("healing_ai_calls", 0):
                st.caption(f"Healing AI calls: {test['healing_ai_calls']}")
            recovery_events = test.get("recovery_events") or []
            for event in recovery_events:
                if event.get("event") == "locator_failed":
                    st.caption(
                        f"LOCATOR FAILED: {event.get('step', '')} - cached selector "
                        f"{event.get('selector', '')!r} {event.get('reason', '')}"
                    )
                elif event.get("event") == "healing_requested":
                    st.caption(f"HEALING: asking OpenAI for a replacement for {event.get('step', '')}")
                elif event.get("event") == "healed":
                    st.caption(f"HEALED: {event.get('step', '')}")
                    st.caption(f"Replacement selector: {event.get('selector', '')}")
            healed_steps = test.get("healed_steps")
            if healed_steps is None:
                healed_steps = [
                    step.get("text", "") for step in test.get("steps", [])
                    if step.get("status") == "passed"
                    and str(step.get("detail") or "").endswith("(healed)")
                ]
            if not any(event.get("event") == "healed" for event in recovery_events):
                for healed_step in healed_steps:
                    st.caption(f"HEALED: {healed_step}")
            if test.get("cache_hits", 0):
                st.caption(f"Cache used: {test['cache_hits']} locator hit(s)")
            if test.get("cached_selectors"):
                st.caption("Cached selectors used")
                st.dataframe(
                    test["cached_selectors"], hide_index=True, width="stretch",
                    column_order=["page", "command", "target", "selector"],
                )
            st.dataframe(test.get("steps", []), hide_index=True, width="stretch")
            if test.get("checks"):
                st.dataframe(test["checks"], hide_index=True, width="stretch")
            if test.get("error"):
                st.error(str(test["error"]))
            screenshot = test.get("screenshot")
            if screenshot:
                image_path = ROOT / screenshot
                if image_path.is_file():
                    st.image(str(image_path), caption=Path(screenshot).name)


def _cache_history() -> None:
    cache = data.load_cache_entries(ROOT)
    apps = sorted(
        {str(item["app"]) for item in cache if item.get("app")}
    )
    selected_app = st.selectbox("Application", ["All applications", *apps], key="cache_history_app")
    if selected_app != "All applications":
        cache = [item for item in cache if item.get("app") == selected_app]

    with st.expander("Cache Controls"):
        scope = selected_app.casefold() if selected_app != "All applications" else "all applications"
        confirmation = st.checkbox(
            f"I understand this removes cached selectors for {scope}.",
            key="clear_cache_confirmation",
        )
        if st.button("Clear Cached Selectors", disabled=not confirmation or not cache):
            try:
                cleared = data.clear_cache_entries(
                    ROOT,
                    app=None if selected_app == "All applications" else selected_app,
                )
            except RuntimeError as exc:
                st.error(str(exc))
            else:
                st.success(f"Cleared {cleared} cached selector(s).")
                st.rerun()

    st.subheader("Cached Selectors")
    if cache:
        st.dataframe(cache, hide_index=True, width="stretch")
    else:
        st.info("No verified selectors are cached for this application.")


def _langsmith() -> None:
    project, error = data.langsmith_settings(ROOT)
    if error:
        st.warning(error)
        return
    st.caption(f"Project: {project}")
    cutoff = data.langsmith_history_cutoff(ROOT)
    if st.session_state.get("langsmith_history_cutoff") != cutoff:
        if "langsmith_runs" in st.session_state:
            st.session_state["langsmith_runs"] = []
        st.session_state["langsmith_history_cutoff"] = cutoff
    if st.button(
        "Clear LLM Call History",
        help="Hide earlier calls in this dashboard without deleting remote LangSmith traces.",
    ):
        try:
            cutoff = data.clear_langsmith_history(ROOT)
        except OSError:
            st.error("Could not clear the dashboard's LLM call history.")
        else:
            st.session_state["langsmith_history_cutoff"] = cutoff
            st.session_state["langsmith_runs"] = []
            st.rerun()
    if cutoff is not None:
        st.caption(
            f"Showing calls since {cutoff.astimezone(now_ist().tzinfo):%Y-%m-%d %H:%M IST}. "
            "Earlier traces remain in LangSmith."
        )
    if st.button("Refresh LangSmith Logs"):
        st.session_state.pop("langsmith_runs", None)
    if "langsmith_runs" not in st.session_state:
        try:
            with st.spinner("Loading LangSmith traces..."):
                st.session_state["langsmith_runs"] = data.load_langsmith_runs(project or "selfheal", root=ROOT)
        except (LangSmithConnectionError, LangSmithRequestTimeout):
            st.error(
                "LangSmith is unreachable from this dashboard process. "
                "Allow access to api.smith.langchain.com:443, then refresh the logs."
            )
            return
        except LangSmithAuthError:
            st.error("LangSmith rejected the configured API key. Check LANGSMITH_API_KEY in .env.")
            return
        except Exception as exc:
            st.error(f"LangSmith logs could not be loaded ({type(exc).__name__}).")
            return
    records = st.session_state["langsmith_runs"]
    if not records:
        st.info("No LLM calls to show.")
        return
    st.dataframe(records, hide_index=True, width="stretch", column_order=[
        "started_at", "application", "name", "model", "status", "total_tokens", "cost_usd", "latency_s",
        "suite_run_id", "test_case_id",
    ])
    trace_urls = [item["trace_url"] for item in records if item.get("trace_url")]
    if trace_urls:
        st.link_button("Open Latest Trace", trace_urls[0])


def main() -> None:
    st.set_page_config(page_title="SelfHeal Control", layout="wide")
    _style()
    migrate_legacy_logs(ROOT / "data" / "dashboard")
    _run_launcher()
    st.title("SelfHeal Control")
    st.caption(f"Updated {now_ist():%d %b %Y, %H:%M} IST")
    runs = data.load_runs(ROOT)
    view = st.radio(
        "View",
        ["Overview", "Configured Websites", "Run Details", "Cache History", "LLM Call History"],
        horizontal=True,
        label_visibility="collapsed",
    )
    if view == "Overview":
        _overview(runs)
    elif view == "Configured Websites":
        _configured_websites()
    elif view == "Run Details":
        _run_details(runs)
    elif view == "Cache History":
        _cache_history()
    else:
        _langsmith()


if __name__ == "__main__":
    main()
