"""Unit tests for the async SQLite persistence layer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from f1_bot.db.database import Database


async def test_guild_settings_upsert_and_partial_update(tmp_path: Path) -> None:
    db_file = tmp_path / "nested" / "test.db"
    db = Database(db_file)
    await db.connect()
    try:
        assert await db.get_guild_settings(111) is None

        created = await db.upsert_guild_settings(
            111,
            announce_channel_id=222,
            ping_role_id=333,
            spoiler_delay_seconds=60,
        )
        assert created.guild_id == 111
        assert created.announce_channel_id == 222
        assert created.ping_role_id == 333
        assert created.spoiler_delay_seconds == 60
        assert created.reminder_minutes == 30
        assert created.vc_cleanup_minutes == 15
        assert created.auto_create_events is True

        # Partial update should preserve unspecified fields
        updated = await db.upsert_guild_settings(
            111,
            reminder_minutes=15,
            auto_create_events=False,
        )
        assert updated.announce_channel_id == 222
        assert updated.ping_role_id == 333
        assert updated.spoiler_delay_seconds == 60
        assert updated.reminder_minutes == 15
        assert updated.auto_create_events is False

        await db.upsert_guild_settings(999, announce_channel_id=888)
        all_guilds = await db.list_configured_guilds()
        assert {g.guild_id for g in all_guilds} == {111, 999}
    finally:
        await db.close()


async def test_synced_events_and_reminders_deduplication() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        assert not await db.is_event_synced(1, 2026, 5, "Qualifying")
        await db.mark_event_synced(1, 2026, 5, "Qualifying", 555001)
        assert await db.is_event_synced(1, 2026, 5, "Qualifying")
        assert not await db.is_event_synced(2, 2026, 5, "Qualifying")

        # Upsert existing key should not fail
        await db.mark_event_synced(1, 2026, 5, "Qualifying", 555002)
        assert await db.is_event_synced(1, 2026, 5, "Qualifying")

        assert not await db.is_reminder_sent(1, 2026, 5, "Race")
        await db.mark_reminder_sent(1, 2026, 5, "Race", datetime.now(timezone.utc))
        assert await db.is_reminder_sent(1, 2026, 5, "Race")
    finally:
        await db.close()


async def test_watch_party_lifecycle() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        party = await db.create_watch_party(
            guild_id=42,
            text_channel_id=100,
            thread_id=101,
            voice_channel_id=102,
            session_key=9158,
            session_label="Singapore Race (Replay)",
            spoiler_delay_seconds=45,
            is_replay=True,
        )
        assert party.active is True
        assert party.is_replay is True
        assert party.sync_started_at is None
        assert party.sync_offset_seconds == 0

        fetched = await db.get_active_watch_party(42)
        assert fetched is not None
        assert fetched.id == party.id

        sync_ts = datetime(2026, 11, 23, 18, 0, 0, tzinfo=timezone.utc)
        await db.update_watch_party_sync(party.id, sync_ts, 120)
        empty_ts = datetime(2026, 11, 23, 19, 30, 0, tzinfo=timezone.utc)
        await db.update_watch_party_empty_since(party.id, empty_ts)

        refreshed = await db.get_active_watch_party(42)
        assert refreshed is not None
        assert refreshed.sync_started_at == sync_ts
        assert refreshed.sync_offset_seconds == 120
        assert refreshed.empty_since == empty_ts
        assert len(await db.list_active_watch_parties()) == 1

        await db.close_watch_party(party.id)
        assert await db.get_active_watch_party(42) is None
        assert await db.list_active_watch_parties() == []
    finally:
        await db.close()
