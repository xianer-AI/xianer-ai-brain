"""Small, deterministic reminder schedule for production-date checks.

The schedule decides *when* a missing-date check may produce a reminder.  It
does not decide whether a date is missing; ``coverage_tables`` and the
authoritative ledger remain responsible for that decision.
"""
from __future__ import annotations

import datetime as dt


AUTO_WORKERS = frozenset(("A", "B", "C"))
CHECK_HOUR = 12
CHECK_MINUTE = 0


def check_time(
    worker: str,
    shift_end: dt.datetime,
    *,
    overtime_end: dt.datetime | None = None,
    defer_hour: int = 10,
) -> dt.datetime | None:
    """Return the next-natural-day noon check time.

    ``shift_end`` and the legacy keyword arguments remain in the signature so
    existing callers do not break, but the actual end time no longer affects
    scheduling.  A/B/C are checked at 12:00 on the next calendar day,
    including Saturdays, Sundays, and holidays.  D is irregular and returns
    ``None`` unless the caller explicitly supplies a confirmed workday through
    its own attendance gate.
    """
    if worker not in AUTO_WORKERS:
        return None
    target_date = shift_end.date() + dt.timedelta(days=1)
    return dt.datetime.combine(
        target_date,
        dt.time(CHECK_HOUR, CHECK_MINUTE),
        tzinfo=shift_end.tzinfo,
    )


def scheduled_check_at(
    worker: str,
    production_date: str | dt.date,
    *,
    tzinfo: dt.tzinfo | None = None,
) -> dt.datetime | None:
    """Return the scheduled check time for a production date."""
    value = (
        dt.date.fromisoformat(production_date)
        if isinstance(production_date, str)
        else production_date
    )
    anchor = dt.datetime.combine(value, dt.time(0, 0), tzinfo=tzinfo)
    return check_time(worker, anchor)


def should_remind(*, record_found: bool = False, already_reported: bool = False,
                  employee_said_not_worked: bool = False,
                  system_check_ok: bool = True) -> bool:
    """Return whether a verified missing-date check may notify an employee."""
    return bool(
        system_check_ok
        and not record_found
        and not already_reported
        and not employee_said_not_worked
    )
