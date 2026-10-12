"""Read-only data access helpers for the SelfHeal operations dashboard."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from selfheal.config import load_environment
from selfheal.time_utils import format_ist


def project_root() -> Path:
    """Return the repository root regardless of the Streamlit launch directory."""
    return Path(__file__).resolve().parents[3]


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def discover_profiles(root: Path | None = None) -> list[dict[str, Any]]:
    """Return valid application profiles from config/apps."""
    root = root or project_root()
    profiles: list[dict[str, Any]] = []
    for path in sorted((root / "config" / "apps").rglob("*.yaml")):
        try:
            content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(content, Mapping):
                continue
            dashboard = content.get("dashboard", {})
            if not isinstance(dashboard, Mapping) or not dashboard.get("visible", False):
                continue
            environments = content.get("environments", {})
            if not isinstance(environments, Mapping):
                continue
            profiles.append(
                {
                    "name": str(content.get("app_name") or path.stem),
                    "path": path,
                    "path_label": _relative(root, path),
                    "environments": {
                        str(name): str(values.get("base_url", ""))
                        for name, values in environments.items()
                        if isinstance(values, Mapping)
                    },
                }
            )
        except (OSError, yaml.YAMLError):
            continue
    return profiles


def _count_step_source(results: Mapping[str, Any], source: str) -> int:
    marker = f"({source})"
    return sum(
        marker in str(step.get("detail", ""))
        for case in results.get("tests", [])
        if isinstance(case, Mapping)
        for step in case.get("steps", [])
        if isinstance(step, Mapping)
    )


def run_summary(results: Mapping[str, Any], results_file: Path, root: Path) -> dict[str, Any]:
    """Normalize current and legacy results files into one dashboard row."""
    healing = results.get("healing") if isinstance(results.get("healing"), Mapping) else {}
    ai_calls = int(results.get("llm_calls", healing.get(
        "ai_calls", _count_step_source(results, "ai") + _count_step_source(results, "healed")
    )) or 0)
    cache_hits = int(healing.get("cache_hits", _count_step_source(results, "cache")) or 0)
    total = int(results.get("total", 0) or 0)
    passed = int(results.get("passed", 0) or 0)
    return {
        "run_id": str(results.get("run_id", results_file.parent.name)),
        "started_at": format_ist(results.get("started_at")),
        "status": str(results.get("status", "unknown")),
        "app_url": str(results.get("base_url", "")),
        "suite": str(results.get("suite", "")),
        "environment": str(results.get("environment", "")),
        "total": total,
        "passed": passed,
        "failed": int(results.get("failed", 0) or 0),
        "needs_review": int(results.get("needs_review", 0) or 0),
        "pass_rate": round((passed / total * 100) if total else 0, 1),
        "llm_calls": ai_calls,
        "cache_hits": cache_hits,
        "results_file": _relative(root, results_file),
    }


def load_runs(root: Path | None = None, limit: int | None = 100) -> list[dict[str, Any]]:
    """Load recent run summaries directly from durable result snapshots."""
    root = root or project_root()
    rows: list[dict[str, Any]] = []
    for results_file in (root / "data" / "runs").glob("run_*/results.json"):
        try:
            results = json.loads(results_file.read_text(encoding="utf-8"))
            if isinstance(results, Mapping):
                rows.append(run_summary(results, results_file, root))
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda item: item["started_at"], reverse=True)
    return rows[:limit] if limit else rows


def load_run_details(run_id: str, root: Path | None = None) -> dict[str, Any] | None:
    """Return one run snapshot, constrained to the framework's run directory."""
    root = root or project_root()
    if not run_id.startswith("run_") or Path(run_id).name != run_id:
        return None
    path = root / "data" / "runs" / run_id / "results.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return result if isinstance(result, dict) else None


def _workspace_xlsx(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(value)
        resolved = (path if path.is_absolute() else root / path).resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved if resolved.is_file() and resolved.suffix.lower() == ".xlsx" else None


def configured_sites(root: Path | None = None) -> list[dict[str, Any]]:
    """Expose generated profiles only after a completed browser run with a saved suite."""
    root = root or project_root()
    generated_dir = (root / "config" / "apps" / "generated").resolve()
    profiles = {}
    for profile in discover_profiles(root):
        try:
            path = profile["path"].resolve()
            path.relative_to(generated_dir)
            profiles[path] = profile
        except (OSError, ValueError):
            continue

    sites = []
    seen = set()
    for run in load_runs(root, limit=None):
        details = load_run_details(run["run_id"], root)
        if not details or not details.get("finished_at") or details.get("run_error"):
            continue
        if not any(case.get("steps") for case in details.get("tests", []) if isinstance(case, dict)):
            continue
        discovery = details.get("discovery") or {}
        profile_value = discovery.get("profile") if isinstance(discovery, Mapping) else None
        if not isinstance(profile_value, str):
            continue
        try:
            profile_path = Path(profile_value)
            profile_path = (profile_path if profile_path.is_absolute() else root / profile_path).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        profile = profiles.get(profile_path)
        if not profile or profile_path in seen:
            continue
        url = profile["environments"].get("prod", "")
        if not url or url.rstrip("/").casefold() != str(details.get("base_url", "")).rstrip("/").casefold():
            continue
        suite = _workspace_xlsx(root, details.get("suite"))
        if suite is None:
            continue
        sites.append({
            "name": profile["name"], "url": url, "profile": profile_path,
            "suite": suite, "last_run": run["run_id"], "status": run["status"],
            "total": run["total"], "passed": run["passed"],
        })
        seen.add(profile_path)
    return sites


def _sqlite_rows(db_path: Path, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    try:
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, tuple(params)).fetchall()]
    except sqlite3.Error:
        return []


def load_cache_entries(
    root: Path | None = None, limit: int | None = 250, app: str | None = None,
) -> list[dict[str, Any]]:
    """Return verified locator cache entries, newest first."""
    root = root or project_root()
    query = "SELECT app, env, page, target, locator, created_at FROM elements"
    params: list[Any] = []
    if app is not None:
        query += " WHERE app = ?"
        params.append(app)
    query += " ORDER BY created_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    entries = _sqlite_rows(
        root / "data" / "store" / "elements.db",
        query, params,
    )
    for entry in entries:
        entry["created_at"] = format_ist(entry.get("created_at"), assume_utc=True)
    return entries


def clear_cache_entries(root: Path | None = None, app: str | None = None) -> int:
    """Delete verified selectors for one app, or every app when no app is supplied."""
    root = root or project_root()
    db_path = root / "data" / "store" / "elements.db"
    if not db_path.is_file():
        return 0
    try:
        with sqlite3.connect(db_path) as connection:
            if app is None:
                count = connection.execute("SELECT COUNT(*) FROM elements").fetchone()[0]
                connection.execute("DELETE FROM elements")
            else:
                count = connection.execute(
                    "SELECT COUNT(*) FROM elements WHERE app = ?", (app,)
                ).fetchone()[0]
                connection.execute("DELETE FROM elements WHERE app = ?", (app,))
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not clear the locator cache: {exc}") from exc
    return int(count)


def load_healing_events(root: Path | None = None, limit: int = 250) -> list[dict[str, Any]]:
    """Return cache and AI recovery activity without browser-page content."""
    root = root or project_root()
    events = _sqlite_rows(
        root / "data" / "store" / "healing_events.db",
        """SELECT run_id, test_case_id, app, env, page, target, event_type, selector, created_at
           FROM healing_events ORDER BY id DESC LIMIT ?""",
        (limit,),
    )
    for event in events:
        event["created_at"] = format_ist(event.get("created_at"), assume_utc=True)
    return events


def langsmith_settings(root: Path | None = None) -> tuple[str | None, str | None]:
    """Return project and a configuration error without returning a secret."""
    root = root or project_root()
    load_environment(root / ".env")
    if not os.getenv("LANGSMITH_API_KEY", "").strip():
        return None, "LANGSMITH_API_KEY is not configured."
    return os.getenv("LANGSMITH_PROJECT", "").strip() or "selfheal", None


def langsmith_history_cutoff(root: Path | None = None) -> datetime | None:
    """Return the earliest LangSmith trace time shown in this dashboard."""
    root = root or project_root()
    path = root / "data" / "dashboard" / "langsmith_history_cutoff.txt"
    try:
        cutoff = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return cutoff.astimezone(UTC) if cutoff.tzinfo else None


def clear_langsmith_history(root: Path | None = None) -> datetime:
    """Hide earlier remote traces in the UI without deleting them in LangSmith."""
    root = root or project_root()
    path = root / "data" / "dashboard" / "langsmith_history_cutoff.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(UTC)
    path.write_text(cutoff.isoformat(), encoding="utf-8")
    return cutoff


def _run_value(run: Any, name: str, default: Any = None) -> Any:
    if isinstance(run, Mapping):
        return run.get(name, default)
    return getattr(run, name, default)


def load_langsmith_runs(
    project: str, limit: int = 50, root: Path | None = None
) -> list[dict[str, Any]]:
    """Fetch recent LLM traces from LangSmith without requesting prompt contents."""
    from langsmith import Client

    root = root or project_root()
    profile_by_url = {
        url.rstrip("/"): profile["name"]
        for profile in discover_profiles(root)
        for url in profile["environments"].values()
        if url
    }
    app_by_run = {
        run["run_id"]: profile_by_url.get(run["app_url"].rstrip("/"), run["app_url"])
        for run in load_runs(root, limit=None)
    }
    client = Client(api_key=os.environ["LANGSMITH_API_KEY"])
    records: list[dict[str, Any]] = []
    query = {"project_name": project, "run_type": "llm", "limit": limit}
    cutoff = langsmith_history_cutoff(root)
    if cutoff is not None:
        query["start_time"] = cutoff
    for run in client.list_runs(**query):
        extra = _run_value(run, "extra", {}) or {}
        metadata = extra.get("metadata", {}) if isinstance(extra, Mapping) else {}
        serialized = _run_value(run, "serialized", {}) or {}
        serialized_kwargs = serialized.get("kwargs", {}) if isinstance(serialized, Mapping) else {}
        start_time = _run_value(run, "start_time")
        latency = _run_value(run, "latency") or _run_value(run, "latency_seconds")
        suite_run_id = str(metadata.get("suite_run_id") or "")
        try:
            trace_url = client.get_run_url(run=run)
        except Exception:
            trace_url = None
        records.append(
            {
                "started_at": format_ist(start_time),
                "application": str(metadata.get("app_name") or app_by_run.get(suite_run_id, "Unmatched")),
                "name": str(_run_value(run, "name", "LLM call")),
                "model": str(serialized_kwargs.get("model") or metadata.get("model_name", "")),
                "status": "error" if _run_value(run, "error") else "completed",
                "total_tokens": _run_value(run, "total_tokens", 0) or 0,
                "cost_usd": _run_value(run, "total_cost", 0) or 0,
                "latency_s": round(float(latency or 0), 3),
                "suite_run_id": suite_run_id,
                "test_case_id": metadata.get("test_case_id", ""),
                "trace_url": trace_url,
            }
        )
    return records
