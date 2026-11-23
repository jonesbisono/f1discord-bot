"""Unit tests for SpoilerDelayQueue, ReplayCursor, sync clock math, and event formatters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from f1_bot.services.spoiler_queue import (
    COLOR_RED,
    COLOR_SAFETY_CAR,
    COLOR_WEATHER,
    RaceFeedEvent,
    ReplayCursor,
    SpoilerDelayQueue,
    compute_current_playback_seconds,
    format_clock,
    format_pit_event,
    format_race_control_event,
    format_weather_event,
    parse_clock,
)


def test_spoiler_delay_queue_buffering_ordering_and_dedupe() -> None:
    queue = SpoilerDelayQueue(delay_seconds=45)
    t0 = datetime(2026, 11, 23, 14, 0, 0, tzinfo=timezone.utc)

    ev1 = RaceFeedEvent(
        kind="race_control",
        event_time=t0,
        title="Yellow Flag",
        description="Yellow in Sector 2",
        color=0xEAB308,
        dedupe_key="rc:1",
    )
    ev2 = RaceFeedEvent(
        kind="race_control",
        event_time=t0 + timedelta(seconds=5),
        title="Safety Car",
        description="SAFETY CAR DEPLOYED",
        color=COLOR_SAFETY_CAR,
        dedupe_key="rc:2",
    )

    assert queue.enqueue(ev1, ingested_at=t0) is True
    # Duplicate key rejected
    assert queue.enqueue(ev1, ingested_at=t0 + timedelta(seconds=10)) is False
    assert queue.enqueue(ev2, ingested_at=t0 + timedelta(seconds=5)) is True
    assert len(queue) == 2

    # Not ready at +40s
    assert queue.pop_ready(now=t0 + timedelta(seconds=40)) == []

    # At +46s only ev1 has matured
    ready_first = queue.pop_ready(now=t0 + timedelta(seconds=46))
    assert [e.dedupe_key for e in ready_first] == ["rc:1"]
    assert len(queue) == 1

    # At +50s ev2 matures
    ready_second = queue.pop_ready(now=t0 + timedelta(seconds=50))
    assert [e.dedupe_key for e in ready_second] == ["rc:2"]
    assert len(queue) == 0


def test_replay_cursor_and_sync_clock_helpers() -> None:
    session_start = datetime(2023, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
    party_start = datetime(2026, 11, 23, 20, 0, 0, tzinfo=timezone.utc)

    cursor = ReplayCursor(session_start=session_start, party_start=party_start, offset_seconds=300)
    # Before party_start clamps to 0 elapsed + offset
    assert cursor.virtual_now(party_start - timedelta(seconds=10)) == session_start + timedelta(seconds=300)
    # 120s after party_start -> session_start + 420s
    assert cursor.virtual_now(party_start + timedelta(seconds=120)) == session_start + timedelta(seconds=420)

    assert parse_clock("42:15") == 42 * 60 + 15
    assert parse_clock("01:05:09") == 3600 + 300 + 9
    assert format_clock(3909) == "01:05:09"
    assert format_clock(-5) == "00:00:00"

    with pytest.raises(ValueError):
        parse_clock("12:75")
    with pytest.raises(ValueError):
        parse_clock("abc")

    playback = compute_current_playback_seconds(
        party_start,
        sync_offset_seconds=600,
        now=party_start + timedelta(seconds=45),
    )
    assert playback == 645


def test_openf1_event_formatters() -> None:
    rc_red = format_race_control_event(
        {
            "date": "2026-05-03T18:15:00Z",
            "category": "Flag",
            "flag": "RED",
            "lap_number": 12,
            "message": "RED FLAG",
        }
    )
    assert rc_red is not None
    assert rc_red.color == COLOR_RED
    assert "Lap 12" in rc_red.description

    # Weather only fires on rainfall transitions (or rain present initially)
    ev, rain = format_weather_event({"date": "2026-05-03T18:00:00Z", "rainfall": 0}, prev_rainfall=None)
    assert ev is None
    assert rain == 0

    ev_same, rain = format_weather_event({"date": "2026-05-03T18:01:00Z", "rainfall": 0}, prev_rainfall=rain)
    assert ev_same is None

    ev_rain, rain = format_weather_event(
        {"date": "2026-05-03T18:02:00Z", "rainfall": 1, "track_temperature": 27.5},
        prev_rainfall=rain,
    )
    assert ev_rain is not None
    assert ev_rain.color == COLOR_WEATHER
    assert rain == 1
    assert "Rain Detected" in ev_rain.title

    pit = format_pit_event(
        {
            "date": "2026-05-03T18:20:00Z",
            "driver_number": 1,
            "lap_number": 18,
            "pit_duration": 21.4,
        },
        drivers={1: "VER"},
    )
    assert pit is not None
    assert "VER (#1)" in pit.description
    assert "21.4s" in pit.description
