"""Streamlit UI for running and observing SelfHeal browser suites."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st

from selfheal.dashboard import data
from selfheal.dashboard.history import (
    finish_dashboard_run,
    migrate_legacy_logs,
    start_dashboard_run,
)
from selfheal.suite_loader import read_excel_tests
from selfheal.time_utils import now_ist

ROOT = data.project_root()


def _style() -> None:
    st.markdown(
        """<style>
        .block-container {max-width: 1400px; padding-top: 2rem; padding-bottom: 2rem;}
        [data-testid="stMetric"] {border-left: 3px solid #207a72; padding-left: 0.8rem;}
        [data-testid="stSidebar"] {border-right: 1px solid #d8dee4;}
        </style>""",
        unsafe_allow_html=True,
    )


def _label(profile: dict) -> str:
    environments = profile["environments"]
    base_url = environments.get("prod") or next(iter(environments.values()), "")
    return f"{profile['name']}  |  {base_url}"


def _tail(path: Path, lines: int = 80) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _start_run(profile: dict, suite: Path, environment: str, headless: bool, case_ids: list[str]) -> None:
    dashboard_dir = ROOT / "data" / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    launch_id = f"launch_{now_ist():%Y%m%d_%H%M%S_%f}"
    output_path = Path(tempfile.gettempdir()) / f"selfheal_{launch_id}.log"
    command = [
        sys.executable,
        "-m",
        "selfheal.cli",
        "run",
        str(suite),
        "--env",
        environment,
        "--config",
        str(profile["path"]),
    ]
    if headless:
        command.append("--headless")
    for case_id in case_ids:
        command.extend(["--case", case_id])
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    start_dashboard_run(
        {
            "launch_id": launch_id,
            "started_at": now_ist(),
            "website": profile["name"],
            "suite": suite.name,
            "environment": environment,
            "headless": headless,
            "cases": case_ids,
        },
        dashboard_dir / "dashboard_history.xlsx",
    )
    with output_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
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


def _run_launcher() -> None:
    profiles = data.discover_profiles(ROOT)
    suites = data.discover_suites(ROOT)
    st.sidebar.subheader("Run Test")
    if not profiles or not suites:
        st.sidebar.error("Add an app profile and an XLSX suite to enable test runs.")
        return
    selected = st.sidebar.selectbox("Website", profiles, format_func=_label)
    environments = list(selected["environments"])
    environment = st.sidebar.selectbox("Environment", environments)
    default_suites = [suite for suite in suites if selected["name"].replace("-", "") in suite.stem.replace("-", "")]
    suite = st.sidebar.selectbox("Suite", suites, index=suites.index(default_suites[0]) if default_suites else 0,
                                 format_func=lambda item: item.name)
    try:
        cases = read_excel_tests(suite)
        case_ids = [str(case["id"]) for case in cases]
    except Exception as exc:
        st.sidebar.error(f"Cannot read suite: {exc}")
        case_ids = []
    selected_cases = st.sidebar.multiselect("Cases", case_ids, placeholder="All enabled cases")
    headless = st.sidebar.toggle("Headless", value=True)
    active = st.session_state.get("active_run")
    running = bool(active and active["process"].poll() is None)
    if st.sidebar.button("Run Selected Suite", type="primary", disabled=running):
        _start_run(selected, suite, environment, headless, selected_cases)
        st.rerun()

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
    llm_calls = sum(run["llm_calls"] for run in runs)
    cache_hits = sum(run["cache_hits"] for run in runs)
    columns = st.columns(4)
    columns[0].metric("Runs", len(runs))
    columns[1].metric("Pass Rate", f"{(passed_cases / total_cases * 100) if total_cases else 0:.1f}%")
    columns[2].metric("LLM Calls", llm_calls)
    columns[3].metric("Cache Hits", cache_hits)
    st.subheader("Recent Runs")
    st.dataframe(runs, hide_index=True, width="stretch", column_order=[
        "started_at", "run_id", "status", "suite", "environment", "total", "passed", "failed",
        "needs_review", "llm_calls", "cache_hits",
    ])


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
    for test in details.get("tests", []):
        title = f"{test.get('id', '')}  {test.get('status', '').upper()}  {test.get('title', '')}"
        with st.expander(title):
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
    events = data.load_healing_events(ROOT)
    cache = data.load_cache_entries(ROOT)
    apps = sorted(
        {profile["name"] for profile in data.discover_profiles(ROOT)}
        | {str(item["app"]) for item in [*cache, *events] if item.get("app")}
    )
    selected_app = st.selectbox("Application", ["All applications", *apps], key="cache_history_app")
    if selected_app != "All applications":
        cache = [item for item in cache if item.get("app") == selected_app]
        events = [item for item in events if item.get("app") == selected_app]
    cache_events = [event for event in events if str(event.get("event_type", "")).startswith("cache_")]

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
                st.success(f"Cleared {cleared} cached selector(s). Cache history was retained.")
                st.rerun()

    columns = st.columns(3)
    columns[0].metric("Cached Selectors", len(cache))
    columns[1].metric("Cache Hits", sum(item.get("event_type") == "cache_hit" for item in cache_events))
    columns[2].metric("Stale Entries", sum(item.get("event_type") == "cache_stale" for item in cache_events))

    st.subheader("Locator Activity")
    if events:
        st.dataframe(events, hide_index=True, width="stretch")
    else:
        st.info("No locator discovery or cache activity has been recorded for this application.")

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
    if st.button("Refresh LangSmith Logs"):
        st.session_state.pop("langsmith_runs", None)
    if "langsmith_runs" not in st.session_state:
        try:
            with st.spinner("Loading LangSmith traces..."):
                st.session_state["langsmith_runs"] = data.load_langsmith_runs(project or "selfheal", root=ROOT)
        except Exception as exc:
            st.error(f"LangSmith logs could not be loaded: {exc}")
            return
    records = st.session_state["langsmith_runs"]
    apps = sorted(
        {profile["name"] for profile in data.discover_profiles(ROOT)}
        | {item["application"] for item in records if item.get("application")}
    )
    selected_app = st.selectbox("Application", ["All applications", *apps], key="langsmith_history_app")
    if selected_app != "All applications":
        records = [item for item in records if item.get("application") == selected_app]
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
    st.caption("Test operations, healing activity, and LLM observability")
    runs = data.load_runs(ROOT)
    view = st.radio(
        "View",
        ["Overview", "Run Details", "Cache History", "LangSmith"],
        horizontal=True,
        label_visibility="collapsed",
    )
    if view == "Overview":
        _overview(runs)
    elif view == "Run Details":
        _run_details(runs)
    elif view == "Cache History":
        _cache_history()
    else:
        _langsmith()


if __name__ == "__main__":
    main()
