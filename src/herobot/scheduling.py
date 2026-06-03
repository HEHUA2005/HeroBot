from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


ISO_RANGE_RE = re.compile(
    r"(\d{4}-\d{1,2}-\d{1,2})\s+(\d{1,2}:\d{2})\s*(?:-|到|至|~)\s*(\d{1,2}:\d{2})"
)


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime


def parse_working_hours(value: str, tz: ZoneInfo, target_date: date) -> TimeWindow:
    start_text, end_text = value.split("-", 1)
    start_hour, start_minute = [int(part) for part in start_text.split(":")]
    end_hour, end_minute = [int(part) for part in end_text.split(":")]
    return TimeWindow(
        datetime.combine(target_date, time(start_hour, start_minute), tz),
        datetime.combine(target_date, time(end_hour, end_minute), tz),
    )


def parse_iso_text_window(text: str, timezone_name: str = "Asia/Shanghai") -> TimeWindow | None:
    tz = ZoneInfo(timezone_name)
    iso = ISO_RANGE_RE.search(text)
    if iso:
        target = date.fromisoformat(iso.group(1))
        start_hour, start_minute = [int(part) for part in iso.group(2).split(":")]
        end_hour, end_minute = [int(part) for part in iso.group(3).split(":")]
        return TimeWindow(
            datetime.combine(target, time(start_hour, start_minute), tz),
            datetime.combine(target, time(end_hour, end_minute), tz),
        )
    return None


def extract_iso_windows(text: str, timezone_name: str = "Asia/Shanghai") -> list[TimeWindow]:
    tz = ZoneInfo(timezone_name)
    windows: list[TimeWindow] = []
    for match in ISO_RANGE_RE.finditer(text):
        target = date.fromisoformat(match.group(1))
        start_hour, start_minute = [int(part) for part in match.group(2).split(":")]
        end_hour, end_minute = [int(part) for part in match.group(3).split(":")]
        windows.append(
            TimeWindow(
                datetime.combine(target, time(start_hour, start_minute), tz),
                datetime.combine(target, time(end_hour, end_minute), tz),
            )
        )
    return windows


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(value: str, timezone_name: str = "Asia/Shanghai") -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(timezone_name))
    return dt


def display_dt(dt: datetime, timezone_name: str = "Asia/Shanghai") -> str:
    local = dt.astimezone(ZoneInfo(timezone_name))
    return local.strftime("%Y-%m-%d %H:%M")


def display_window(start_iso: str, end_iso: str, timezone_name: str = "Asia/Shanghai") -> str:
    start = from_iso(start_iso).astimezone(ZoneInfo(timezone_name))
    end = from_iso(end_iso).astimezone(ZoneInfo(timezone_name))
    return f"{start:%Y-%m-%d %H:%M}-{end:%H:%M}"


def event_overlaps(event: dict[str, Any], start: datetime, end: datetime) -> bool:
    event_start = from_iso(event["start_at"])
    event_end = from_iso(event["end_at"])
    return event_start < end and event_end > start


def find_free_windows(
    events: list[dict[str, Any]],
    window: TimeWindow,
    duration_minutes: int,
    limit: int = 3,
) -> list[TimeWindow]:
    busy = sorted(
        (
            TimeWindow(from_iso(event["start_at"]), from_iso(event["end_at"]))
            for event in events
            if event["status"] != "cancelled" and event_overlaps(event, window.start, window.end)
        ),
        key=lambda item: item.start,
    )
    cursor = window.start
    slots: list[TimeWindow] = []
    duration = timedelta(minutes=duration_minutes)
    for item in busy:
        if item.start > cursor and item.start - cursor >= duration:
            slots.append(TimeWindow(cursor, cursor + duration))
            if len(slots) >= limit:
                return slots
        if item.end > cursor:
            cursor = item.end
    if window.end - cursor >= duration:
        slots.append(TimeWindow(cursor, cursor + duration))
    return slots[:limit]


def intersect_windows(
    left: list[TimeWindow], right: list[TimeWindow], duration_minutes: int, limit: int = 3
) -> list[TimeWindow]:
    duration = timedelta(minutes=duration_minutes)
    candidates: list[TimeWindow] = []
    for a in left:
        for b in right:
            start = max(a.start, b.start)
            end = min(a.end, b.end)
            if end - start >= duration:
                candidates.append(TimeWindow(start, start + duration))
                if len(candidates) >= limit:
                    return candidates
    return candidates
