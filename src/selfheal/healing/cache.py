"""SQLite persistence for verified element locators."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from selfheal.time_utils import now_ist

DEFAULT_CACHE_PATH = Path("data/store/elements.db")
LOGGER = logging.getLogger(__name__)


class LocatorCache:
    """Store selectors by application, environment, page path, and target description."""

    def __init__(self, db_path: str | Path = DEFAULT_CACHE_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS elements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        app TEXT NOT NULL,
                        env TEXT NOT NULL,
                        page TEXT NOT NULL,
                        target TEXT NOT NULL,
                        locator TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(app, env, page, target)
                    )
                    """
                )
        except sqlite3.Error:
            LOGGER.warning("Could not initialize the locator cache", exc_info=True)

    def get(self, app: str, environment: str, page: str, target: str) -> str | None:
        """Return a saved locator, if present."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT locator FROM elements
                    WHERE app = ? AND env = ? AND page = ? AND target = ?
                    """,
                    (app, environment, page, target),
                ).fetchone()
        except sqlite3.Error:
            LOGGER.warning("Could not read from the locator cache", exc_info=True)
            return None
        return str(row[0]) if row else None

    def put(self, app: str, environment: str, page: str, target: str, locator: str) -> None:
        """Insert or replace a verified locator."""
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO elements (app, env, page, target, locator, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (app, environment, page, target, locator, now_ist().isoformat()),
                )
        except sqlite3.Error:
            LOGGER.warning("Could not save to the locator cache", exc_info=True)

    def put_missing(
        self, app: str, environment: str, entries: list[tuple[str, str, str]],
    ) -> None:
        """Save passed-case selectors without replacing discovered or healed entries."""
        if not entries:
            return
        try:
            with self._connect() as connection:
                connection.executemany(
                    """INSERT OR IGNORE INTO elements
                       (app, env, page, target, locator, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (app, environment, page, target, selector, now_ist().isoformat())
                        for page, target, selector in entries
                    ],
                )
        except sqlite3.Error:
            LOGGER.warning("Could not save passed-case locators", exc_info=True)

    def replace_app(
        self, app: str, environment: str, entries: list[tuple[str, str, str]],
    ) -> None:
        """Atomically replace a site's discovery selectors after revalidation."""
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM elements WHERE app = ? AND env = ?", (app, environment)
                )
                connection.executemany(
                    """INSERT INTO elements (app, env, page, target, locator, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (app, environment, page, target, selector, now_ist().isoformat())
                        for page, target, selector in entries
                    ],
                )
        except sqlite3.Error as exc:
            raise RuntimeError("Could not persist the discovered locator cache") from exc

    def merge_app(
        self, app: str, environment: str, entries: list[tuple[str, str, str]],
    ) -> None:
        """Atomically add verified selectors without removing other suite targets."""
        try:
            with self._connect() as connection:
                connection.executemany(
                    """INSERT INTO elements (app, env, page, target, locator, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(app, env, page, target)
                       DO UPDATE SET locator = excluded.locator,
                                     created_at = excluded.created_at""",
                    [
                        (app, environment, page, target, selector, now_ist().isoformat())
                        for page, target, selector in entries
                    ],
                )
        except sqlite3.Error as exc:
            raise RuntimeError("Could not persist the discovered locator cache") from exc

    def clear(self, app: str | None = None) -> None:
        """Clear cache entries for one application or every application."""
        try:
            with self._connect() as connection:
                if app is None:
                    connection.execute("DELETE FROM elements")
                else:
                    connection.execute("DELETE FROM elements WHERE app = ?", (app,))
        except sqlite3.Error:
            LOGGER.warning("Could not clear the locator cache", exc_info=True)

    def count(self) -> int:
        """Return the number of stored selectors."""
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT COUNT(*) FROM elements").fetchone()
        except sqlite3.Error:
            LOGGER.warning("Could not read locator cache statistics", exc_info=True)
            return 0
        return int(row[0]) if row else 0
