from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

_clock_provider: ContextVar[Callable[[], datetime] | None] = ContextVar(
    "clock_provider", default=None
)


def now() -> datetime:
    provider = _clock_provider.get()
    if provider is not None:
        current = provider()
        if current.tzinfo is None:
            return current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def set_clock_provider(provider: Callable[[], datetime] | None) -> None:
    _clock_provider.set(provider)


def get_ist_zone() -> ZoneInfo:
    return ZoneInfo(settings.TIMEZONE)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_ist_zone())


def ist_now() -> datetime:
    return to_ist(now())


def is_surge_window(dt: datetime | None = None) -> bool:
    target = to_ist(dt) if dt is not None else ist_now()
    return settings.SURGE_START_HOUR <= target.hour < settings.SURGE_END_HOUR
