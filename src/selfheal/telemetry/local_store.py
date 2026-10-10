"""Optional local usage totals when LangSmith tracing is disabled."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from selfheal.time_utils import now_ist

DEFAULT_TELEMETRY_PATH = Path("data/store/telemetry.db")


def _connect(db_path: str | Path = DEFAULT_TELEMETRY_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def init_local_store(db_path: str | Path = DEFAULT_TELEMETRY_PATH) -> None:
    """Create the optional local LLM-usage table."""
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                test_case_id TEXT,
                step_no INTEGER,
                reason TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                cost_usd REAL,
                latency_ms INTEGER,
                status TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def log_llm_call(
    run_id: str,
    test_case_id: str,
    step_no: int,
    reason: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    latency_ms: int,
    status: str = "success",
    db_path: str | Path = DEFAULT_TELEMETRY_PATH,
) -> None:
    """Persist one local LLM usage record for installations without LangSmith."""
    init_local_store(db_path)
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO llm_calls (
                run_id, test_case_id, step_no, reason, model,
                input_tokens, output_tokens, total_tokens, cost_usd,
                latency_ms, status, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                test_case_id,
                step_no,
                reason,
                model,
                input_tokens,
                output_tokens,
                input_tokens + output_tokens,
                cost,
                latency_ms,
                status,
                now_ist().isoformat(),
            ),
        )


def get_run_llm_stats(
    run_id: str, db_path: str | Path = DEFAULT_TELEMETRY_PATH
) -> dict[str, Any]:
    """Return local LLM usage totals for a run."""
    init_local_store(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost,
                COUNT(*) AS call_count,
                COALESCE(AVG(latency_ms), 0) AS avg_latency
            FROM llm_calls
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
    return dict(row)


def get_total_usage(db_path: str | Path = DEFAULT_TELEMETRY_PATH) -> dict[str, Any]:
    """Return all local LLM usage totals."""
    init_local_store(db_path)
    with _connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost,
                COUNT(*) AS call_count
            FROM llm_calls
            """
        ).fetchone()
    return dict(row)
