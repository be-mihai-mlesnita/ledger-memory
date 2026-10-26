from datetime import datetime, timezone

TIER_1 = 1
TIER_2 = 2
TIER_3 = 3

DEFAULT_IDLE_SECONDS = 180


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def is_present(last_activity_at: str | None, now: str, *, idle_seconds: int = DEFAULT_IDLE_SECONDS) -> bool:
    if last_activity_at is None:
        return False
    try:
        last = _parse_dt(last_activity_at)
        now_dt = _parse_dt(now)
    except ValueError:
        return False
    if last > now_dt:
        return False
    delta = (now_dt - last).total_seconds()
    return delta <= idle_seconds


def seconds_until_expiry(last_activity_at: str | None, now: str, *, idle_seconds: int = DEFAULT_IDLE_SECONDS) -> int:
    if last_activity_at is None:
        return 0
    try:
        last = _parse_dt(last_activity_at)
        now_dt = _parse_dt(now)
    except ValueError:
        return 0
    if last > now_dt:
        return 0
    delta = (now_dt - last).total_seconds()
    remaining = idle_seconds - delta
    if remaining <= 0:
        return 0
    return int(remaining)


def effective_ceiling(requested_ceiling: int, present: bool) -> int:
    if not present:
        if requested_ceiling == TIER_1:
            return TIER_2
    return requested_ceiling
