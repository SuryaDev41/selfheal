"""Persist concise, privacy-safe locator-healing events for operations reporting."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from selfheal.time_utils import now_ist

DEFAULT_HEALING_LOG_PATH = Path("data/store/healing_events.db")


class HealingEventStore:
    """Store cache and AI recovery events without retaining page contents."""

    def __init__(self, db_path: str | Path = DEFAULT_HEALING_LOG_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS healing_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    test_case_id TEXT,
                    app TEXT NOT NULL,
                    env TEXT NOT NULL,
                    page TEXT NOT NULL,
                    target TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    selector TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def record(
        self,
        *,
        run_id: str | None,
        test_case_id: str | None,
        app: str,
        environment: str,
        page: str,
        target: str,
        event_type: str,
        selector: str | None = None,
    ) -> None:
        """Add one recovery event using locator metadata only."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO healing_events (
                    run_id, test_case_id, app, env, page, target, event_type, selector, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    test_case_id,
                    app,
                    environment,
                    page,
                    target,
                    event_type,
                    selector,
                    now_ist().isoformat(),
                ),
            )
