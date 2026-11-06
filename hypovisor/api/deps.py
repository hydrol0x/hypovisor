from __future__ import annotations

from datetime import datetime, timedelta

from ..timeutil import now_utc


def default_range(days: int) -> tuple[datetime, datetime]:
    end = now_utc()
    return end - timedelta(days=days), end
