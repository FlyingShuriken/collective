from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def days_between(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    return (
        Decimal(delta.days)
        + (Decimal(delta.seconds) + Decimal(delta.microseconds) / 1_000_000) / 86400
    )


def days_delta(days: Decimal) -> timedelta:
    return timedelta(microseconds=int(days * Decimal(86_400_000_000)))
