"""Timestamp parsing, half-open windows and calendar-month bucketing.

All instants are normalized to timezone-aware UTC.  Naive timestamps are
assumed to already be UTC.

Windows are **half-open** ``[start, end)``.  Source meters report an
inclusive final instant (``...T23:59:59``, sometimes ``...T23:59``), so an
end-of-day reported end is advanced to the next midnight.  This yields a
clean ``...T00:00:00`` boundary of the next period and an exact 744-hour
January.  An end that is already exactly midnight is treated as that
exclusive boundary and kept as-is; any other reported end is advanced by one
second.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class WindowError(ValueError):
    """Raised when a window cannot be parsed or is not a valid interval."""


def parse_instant(raw: object) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise WindowError("missing timestamp")
    text = raw.strip()
    # datetime.fromisoformat in 3.11 understands "+00:00" but not "Z".
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WindowError(f"unparsable timestamp: {raw!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_day = calendar.monthrange(start.year, start.month)[1]
    end = start.replace(day=last_day) + timedelta(days=1)
    return start, end


def month_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


@dataclass(frozen=True)
class Window:
    """A normalized half-open interval in UTC."""

    start: datetime
    end: datetime
    reported_end: datetime

    @classmethod
    def from_reported(cls, raw_start: object, raw_end: object) -> "Window":
        start = parse_instant(raw_start)
        reported_end = parse_instant(raw_end)
        if reported_end < start:
            raise WindowError("window_end is before window_start")
        end = reported_end
        if end == start:
            raise WindowError("window is empty")
        if _is_midnight(end):
            # Already an exclusive boundary; do not advance.
            pass
        elif _is_close_of_day(end):
            # ``23:59`` or ``23:59:59`` are inclusive end-of-day stamps.
            end = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            end = end + timedelta(seconds=1)
        if end <= start:
            raise WindowError("window is empty")
        return cls(start=start, end=end, reported_end=reported_end)

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def month(self) -> str:
        return month_key(self.start)

    @property
    def month_start(self) -> datetime:
        return month_bounds(self.start)[0]

    @property
    def month_end(self) -> datetime:
        return month_bounds(self.start)[1]

    @property
    def crosses_month(self) -> bool:
        return self.end > self.month_end

    @property
    def coverage(self) -> float:
        """Fraction of the start month's calendar span covered by the window.

        A window that starts mid-month or ends mid-month is partial even when
        it is several weeks long.  A window crossing into the next month is
        partial for the start month and is also flagged as crossing.
        """
        month_start, month_end = month_bounds(self.start)
        clipped_start = max(self.start, month_start)
        clipped_end = min(self.end, month_end)
        if clipped_end <= clipped_start:
            return 0.0
        return (clipped_end - clipped_start).total_seconds() / (
            month_end - month_start
        ).total_seconds()

    def same_period(self, other: "Window") -> bool:
        return self.start == other.start and self.end == other.end


def _is_midnight(moment: datetime) -> bool:
    return (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0)


def _is_close_of_day(moment: datetime) -> bool:
    return (moment.hour, moment.minute) == (23, 59)
