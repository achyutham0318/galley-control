from datetime import datetime, timezone

from app.core.clock import to_ist


def format_utc_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def format_ist_iso(dt: datetime) -> str:
    return to_ist(dt).isoformat()


def format_ist_human(dt: datetime) -> str:
    return to_ist(dt).strftime("%d %b %Y, %I:%M:%S %p IST")


def format_optional_timestamps(dt: datetime | None) -> tuple[str | None, str | None]:
    if dt is None:
        return None, None
    return format_utc_z(dt), format_ist_iso(dt)
