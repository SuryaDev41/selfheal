"""Timezone helpers for consistent India Standard Time reporting."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Return the current timezone-aware India Standard Time."""
    return datetime.now(IST)


def to_ist(value: datetime | str | None, *, assume_utc: bool = False) -> datetime | None:
    """Convert a timestamp to IST, with an optional UTC interpretation for naive values."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, datetime):
        parsed = value
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC if assume_utc else IST).astimezone(IST)
    return parsed.astimezone(IST)


def format_ist(value: datetime | str | None, *, assume_utc: bool = False) -> str:
    """Format a timestamp for concise dashboard display in IST."""
    converted = to_ist(value, assume_utc=assume_utc)
    return converted.strftime("%Y-%m-%d %H:%M:%S IST") if converted else ""
