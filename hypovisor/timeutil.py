"""Timezone helpers.

Vendor exports (Dexcom Clarity, Tandem Source) contain *local* timestamps with no offset.
We store everything as naive UTC plus the local offset in minutes that was in effect, so
analyses that care about "hour of day" can reconstruct local time without guessing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings


def tz(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or settings.timezone)


def local_naive_to_utc(local_dt: datetime, tz_name: str | None = None) -> tuple[datetime, int]:
    """Interpret a naive local datetime in the configured zone. Returns (naive UTC, offset minutes)."""
    if local_dt.tzinfo is not None:
        aware = local_dt
    else:
        aware = local_dt.replace(tzinfo=tz(tz_name))
    utc = aware.astimezone(timezone.utc)
    offset = int(aware.utcoffset().total_seconds() // 60)
    return utc.replace(tzinfo=None), offset


def utc_to_local(utc_naive: datetime, offset_min: int | None = None, tz_name: str | None = None) -> datetime:
    """Convert naive UTC to a local naive datetime, using the stored offset when available."""
    if offset_min is not None:
        return utc_naive + timedelta(minutes=offset_min)
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(tz(tz_name)).replace(tzinfo=None)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_iso_local(text: str) -> datetime:
    """Parse ISO-ish local timestamps used by vendor exports: 2026-03-05T04:51:44 or with a space."""
    text = text.strip().replace(" ", "T")
    if "." in text:
        text = text.split(".")[0]
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%YT%H:%M:%S",
        "%m/%d/%YT%H:%M:%S",
        "%m/%d/%YT%H:%M",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(text)
