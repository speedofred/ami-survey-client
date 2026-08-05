"""One ISO-8601 representation, used everywhere.

Timestamps arrive from three places - transcript entries, the API clock, and
agent-supplied overrides - and are compared against each other constantly
(window filtering, stage attribution, min/max aggregation). Mixed precision
makes lexicographic comparison silently wrong ("...128000Z" sorts before
"...128Z"), so every timestamp is parsed and re-emitted at millisecond
precision, and every comparison is done on datetimes.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_ts(raw: str | datetime) -> datetime:
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc)
    dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def normalize(raw: str | None) -> str | None:
    return None if raw in (None, "") else iso(parse_ts(raw))


def utcnow() -> str:
    return iso(datetime.now(timezone.utc))


def min_ts(values) -> str | None:
    values = [v for v in values if v]
    return min(values, key=parse_ts) if values else None


def max_ts(values) -> str | None:
    values = [v for v in values if v]
    return max(values, key=parse_ts) if values else None
