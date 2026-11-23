"""Spoiler-delay buffering, deduplication, replay timing, and watch-party sync clock helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from f1_bot.clients.openf1 import parse_openf1_timestamp

COLOR_RED = 0xE10600
COLOR_SAFETY_CAR = 0xF59E0B
COLOR_YELLOW = 0xEAB308
COLOR_GREEN = 0x22C55E
COLOR_WEATHER = 0x38BDF8
COLOR_PIT = 0x94A3B8


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(slots=True)
class RaceFeedEvent:
    """Normalized race-control, weather, or pit event ready for delayed Discord delivery."""

    kind: str
    event_time: datetime
    title: str
    description: str
    color: int
    dedupe_key: str


@dataclass(slots=True)
class _QueuedEvent:
    release_at: datetime
    event: RaceFeedEvent


class SpoilerDelayQueue:
    """Buffers incoming race events by a configurable delay and deduplicates repeated polls."""

    def __init__(self, delay_seconds: int = 45) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        self.delay_seconds = delay_seconds
        self._seen_keys: set[str] = set()
        self._pending: list[_QueuedEvent] = []

    def __len__(self) -> int:
        return len(self._pending)

    def enqueue(self, event: RaceFeedEvent, ingested_at: datetime | None = None) -> bool:
        """Queue an event for delayed release. Returns False if already seen."""
        if event.dedupe_key in self._seen_keys:
            return False

        self._seen_keys.add(event.dedupe_key)
        base_time = _ensure_utc(ingested_at or datetime.now(timezone.utc))
        release_at = base_time + timedelta(seconds=self.delay_seconds)
        self._pending.append(_QueuedEvent(release_at=release_at, event=event))
        return True

    def pop_ready(self, now: datetime | None = None) -> list[RaceFeedEvent]:
        """Pop and return all matured events in chronological order."""
        current = _ensure_utc(now or datetime.now(timezone.utc))
        ready: list[_QueuedEvent] = []
        remaining: list[_QueuedEvent] = []

        for item in self._pending:
            if item.release_at <= current:
                ready.append(item)
            else:
                remaining.append(item)

        self._pending = remaining
        ready.sort(key=lambda item: (item.release_at, _ensure_utc(item.event.event_time)))
        return [item.event for item in ready]


class ReplayCursor:
    """Maps real-world elapsed watch-party time onto a historical OpenF1 session timeline."""

    def __init__(
        self,
        session_start: datetime,
        party_start: datetime,
        *,
        offset_seconds: int = 0,
    ) -> None:
        self.session_start = _ensure_utc(session_start)
        self.party_start = _ensure_utc(party_start)
        self.offset_seconds = offset_seconds

    def virtual_now(self, real_now: datetime | None = None) -> datetime:
        current = _ensure_utc(real_now or datetime.now(timezone.utc))
        elapsed = max(current - self.party_start, timedelta(0))
        return self.session_start + elapsed + timedelta(seconds=self.offset_seconds)


def parse_clock(text: str) -> int:
    """Parse an `HH:MM:SS` or `MM:SS` clock string into total seconds."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("Timestamp cannot be empty.")

    parts = stripped.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("Timestamp must be in MM:SS or HH:MM:SS format.")

    if not all(part.isdigit() for part in parts):
        raise ValueError("Timestamp segments must contain digits only.")

    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        hours = 0
        minutes, seconds = numbers
    else:
        hours, minutes, seconds = numbers

    if minutes >= 60 or seconds >= 60:
        raise ValueError("Minutes and seconds must be below 60.")

    return hours * 3600 + minutes * 60 + seconds


def format_clock(total_seconds: int) -> str:
    """Format total seconds as `HH:MM:SS`."""
    clamped = max(0, int(total_seconds))
    hours, rem = divmod(clamped, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def compute_current_playback_seconds(
    sync_started_at: datetime,
    sync_offset_seconds: int,
    now: datetime | None = None,
) -> int:
    """Calculate the current synced playback offset in seconds."""
    current = _ensure_utc(now or datetime.now(timezone.utc))
    started = _ensure_utc(sync_started_at)
    elapsed = max(0, int((current - started).total_seconds()))
    return max(0, elapsed + int(sync_offset_seconds))


def format_race_control_event(item: dict[str, Any]) -> RaceFeedEvent | None:
    """Convert an OpenF1 `/race_control` record into a normalized RaceFeedEvent."""
    raw_date = item.get("date")
    message = str(item.get("message") or "").strip()
    if not raw_date or not message:
        return None

    event_time = parse_openf1_timestamp(str(raw_date))
    flag = str(item.get("flag") or "").upper()
    category = str(item.get("category") or "")
    upper_msg = message.upper()

    if flag == "RED" or "RED FLAG" in upper_msg:
        title = "🚩 Red Flag"
        color = COLOR_RED
    elif category == "SafetyCar" or "SAFETY CAR" in upper_msg or "VSC" in upper_msg:
        is_vsc = "VIRTUAL" in upper_msg or "VSC" in upper_msg
        title = "🟡 Virtual Safety Car" if is_vsc else "🟡 Safety Car"
        color = COLOR_SAFETY_CAR
    elif flag in ("YELLOW", "DOUBLE YELLOW"):
        title = "🟨 Double Yellow Flag" if flag == "DOUBLE YELLOW" else "🟨 Yellow Flag"
        color = COLOR_YELLOW
    elif flag in ("GREEN", "CLEAR") or "TRACK CLEAR" in upper_msg:
        title = "🟢 Track Clear"
        color = COLOR_GREEN
    elif flag == "CHEQUERED" or "CHEQUERED FLAG" in upper_msg:
        title = "🏁 Chequered Flag"
        color = COLOR_GREEN
    elif "PENALTY" in upper_msg or "INVESTIGATION" in upper_msg or "NOTED" in upper_msg:
        title = "⚖️ Stewards / Race Control"
        color = COLOR_SAFETY_CAR
    elif flag == "BLACK AND WHITE":
        title = "🏴 Black & White Flag"
        color = COLOR_YELLOW
    else:
        title = f"📣 Race Control ({category or 'Notice'})"
        color = COLOR_PIT

    details: list[str] = []
    lap_num = item.get("lap_number")
    if lap_num is not None:
        details.append(f"Lap {lap_num}")
    sector = item.get("sector")
    if sector is not None:
        details.append(f"Sector {sector}")
    prefix = f"[{' · '.join(details)}] " if details else ""

    dedupe_key = f"rc:{event_time.isoformat()}:{category}:{flag}:{message}"
    return RaceFeedEvent(
        kind="race_control",
        event_time=event_time,
        title=title,
        description=f"{prefix}{message}",
        color=color,
        dedupe_key=dedupe_key,
    )


def format_weather_event(
    item: dict[str, Any],
    *,
    prev_rainfall: int | None,
) -> tuple[RaceFeedEvent | None, int | None]:
    """Emit a weather alert only when rainfall status changes."""
    raw_date = item.get("date")
    if not raw_date:
        return (None, prev_rainfall)

    raw_rain = item.get("rainfall")
    if raw_rain is None:
        return (None, prev_rainfall)

    rainfall = 1 if int(raw_rain) > 0 else 0
    if prev_rainfall is None:
        # Establish baseline without emitting noise unless already raining at start.
        if rainfall == 0:
            return (None, rainfall)
    elif rainfall == prev_rainfall:
        return (None, rainfall)

    event_time = parse_openf1_timestamp(str(raw_date))
    track_temp = item.get("track_temperature")
    air_temp = item.get("air_temperature")
    wind_speed = item.get("wind_speed")

    metrics: list[str] = []
    if track_temp is not None:
        metrics.append(f"Track {track_temp}°C")
    if air_temp is not None:
        metrics.append(f"Air {air_temp}°C")
    if wind_speed is not None:
        metrics.append(f"Wind {wind_speed} m/s")
    metrics_text = f" ({', '.join(metrics)})" if metrics else ""

    if rainfall == 1:
        title = "🌧️ Rain Detected"
        desc = f"Rainfall reported around the circuit{metrics_text}."
    else:
        title = "🌤️ Rain Stopped"
        desc = f"Rainfall has cleared at the circuit{metrics_text}."

    event = RaceFeedEvent(
        kind="weather",
        event_time=event_time,
        title=title,
        description=desc,
        color=COLOR_WEATHER,
        dedupe_key=f"weather:{event_time.isoformat()}:rain={rainfall}",
    )
    return (event, rainfall)


def format_pit_event(
    item: dict[str, Any],
    drivers: dict[int, str] | None = None,
) -> RaceFeedEvent | None:
    """Convert an OpenF1 `/pit` record into a normalized RaceFeedEvent."""
    raw_date = item.get("date")
    driver_num = item.get("driver_number")
    if not raw_date or driver_num is None:
        return None

    event_time = parse_openf1_timestamp(str(raw_date))
    driver_int = int(driver_num)
    driver_label = (
        f"{drivers[driver_int]} (#{driver_int})"
        if drivers and driver_int in drivers
        else f"Car #{driver_int}"
    )

    lap_num = item.get("lap_number")
    pit_duration = item.get("pit_duration")
    parts: list[str] = [f"**{driver_label}** boxed"]
    if lap_num is not None:
        parts.append(f"on Lap {lap_num}")
    if pit_duration is not None:
        parts.append(f"(pit lane: {float(pit_duration):.1f}s)")

    return RaceFeedEvent(
        kind="pit",
        event_time=event_time,
        title="🛞 Pit Stop",
        description=" ".join(parts),
        color=COLOR_PIT,
        dedupe_key=f"pit:{event_time.isoformat()}:{driver_int}:{lap_num}",
    )
