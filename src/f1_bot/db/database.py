"""Async SQLite persistence layer for server configs, synced events, reminders, and watch parties."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    announce_channel_id INTEGER,
    ping_role_id INTEGER,
    vc_category_id INTEGER,
    spoiler_delay_seconds INTEGER NOT NULL DEFAULT 45,
    reminder_minutes INTEGER NOT NULL DEFAULT 30,
    vc_cleanup_minutes INTEGER NOT NULL DEFAULT 15,
    auto_create_events INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS synced_events (
    guild_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    round_num INTEGER NOT NULL,
    session_name TEXT NOT NULL,
    discord_event_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, season, round_num, session_name)
);

CREATE TABLE IF NOT EXISTS sent_reminders (
    guild_id INTEGER NOT NULL,
    season INTEGER NOT NULL,
    round_num INTEGER NOT NULL,
    session_name TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, season, round_num, session_name)
);

CREATE TABLE IF NOT EXISTS watch_parties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    text_channel_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    voice_channel_id INTEGER,
    session_key INTEGER,
    session_label TEXT NOT NULL,
    spoiler_delay_seconds INTEGER NOT NULL DEFAULT 45,
    sync_started_at TEXT,
    sync_offset_seconds INTEGER NOT NULL DEFAULT 0,
    is_replay INTEGER NOT NULL DEFAULT 0,
    empty_since TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watch_parties_guild_active
    ON watch_parties (guild_id, active);
"""


def _parse_optional_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(slots=True)
class GuildSettings:
    guild_id: int
    announce_channel_id: int | None = None
    ping_role_id: int | None = None
    vc_category_id: int | None = None
    spoiler_delay_seconds: int = 45
    reminder_minutes: int = 30
    vc_cleanup_minutes: int = 15
    auto_create_events: bool = True

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> GuildSettings:
        return cls(
            guild_id=int(row["guild_id"]),
            announce_channel_id=row["announce_channel_id"],
            ping_role_id=row["ping_role_id"],
            vc_category_id=row["vc_category_id"],
            spoiler_delay_seconds=int(row["spoiler_delay_seconds"]),
            reminder_minutes=int(row["reminder_minutes"]),
            vc_cleanup_minutes=int(row["vc_cleanup_minutes"]),
            auto_create_events=bool(row["auto_create_events"]),
        )


@dataclass(slots=True)
class WatchParty:
    id: int
    guild_id: int
    text_channel_id: int
    thread_id: int
    voice_channel_id: int | None
    session_key: int | None
    session_label: str
    spoiler_delay_seconds: int
    sync_started_at: datetime | None
    sync_offset_seconds: int
    is_replay: bool
    empty_since: datetime | None
    active: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> WatchParty:
        created = _parse_optional_dt(row["created_at"])
        assert created is not None
        return cls(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            text_channel_id=int(row["text_channel_id"]),
            thread_id=int(row["thread_id"]),
            voice_channel_id=row["voice_channel_id"],
            session_key=row["session_key"],
            session_label=str(row["session_label"]),
            spoiler_delay_seconds=int(row["spoiler_delay_seconds"]),
            sync_started_at=_parse_optional_dt(row["sync_started_at"]),
            sync_offset_seconds=int(row["sync_offset_seconds"]),
            is_replay=bool(row["is_replay"]),
            empty_since=_parse_optional_dt(row["empty_since"]),
            active=bool(row["active"]),
            created_at=created,
        )


class Database:
    """Async SQLite repository for F1 Watch-Party Bot state."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first.")
        return self._conn

    async def connect(self) -> None:
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- Guild Settings ---

    async def get_guild_settings(self, guild_id: int) -> GuildSettings | None:
        async with self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return GuildSettings.from_row(row) if row is not None else None

    async def upsert_guild_settings(
        self,
        guild_id: int,
        *,
        announce_channel_id: int | None = None,
        ping_role_id: int | None = None,
        vc_category_id: int | None = None,
        spoiler_delay_seconds: int | None = None,
        reminder_minutes: int | None = None,
        vc_cleanup_minutes: int | None = None,
        auto_create_events: bool | None = None,
    ) -> GuildSettings:
        existing = await self.get_guild_settings(guild_id)
        merged = GuildSettings(
            guild_id=guild_id,
            announce_channel_id=(
                announce_channel_id
                if announce_channel_id is not None
                else (existing.announce_channel_id if existing else None)
            ),
            ping_role_id=(
                ping_role_id
                if ping_role_id is not None
                else (existing.ping_role_id if existing else None)
            ),
            vc_category_id=(
                vc_category_id
                if vc_category_id is not None
                else (existing.vc_category_id if existing else None)
            ),
            spoiler_delay_seconds=(
                spoiler_delay_seconds
                if spoiler_delay_seconds is not None
                else (existing.spoiler_delay_seconds if existing else 45)
            ),
            reminder_minutes=(
                reminder_minutes
                if reminder_minutes is not None
                else (existing.reminder_minutes if existing else 30)
            ),
            vc_cleanup_minutes=(
                vc_cleanup_minutes
                if vc_cleanup_minutes is not None
                else (existing.vc_cleanup_minutes if existing else 15)
            ),
            auto_create_events=(
                auto_create_events
                if auto_create_events is not None
                else (existing.auto_create_events if existing else True)
            ),
        )
        await self.conn.execute(
            """
            INSERT INTO guild_settings (
                guild_id,
                announce_channel_id,
                ping_role_id,
                vc_category_id,
                spoiler_delay_seconds,
                reminder_minutes,
                vc_cleanup_minutes,
                auto_create_events
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                announce_channel_id = excluded.announce_channel_id,
                ping_role_id = excluded.ping_role_id,
                vc_category_id = excluded.vc_category_id,
                spoiler_delay_seconds = excluded.spoiler_delay_seconds,
                reminder_minutes = excluded.reminder_minutes,
                vc_cleanup_minutes = excluded.vc_cleanup_minutes,
                auto_create_events = excluded.auto_create_events
            """,
            (
                merged.guild_id,
                merged.announce_channel_id,
                merged.ping_role_id,
                merged.vc_category_id,
                merged.spoiler_delay_seconds,
                merged.reminder_minutes,
                merged.vc_cleanup_minutes,
                int(merged.auto_create_events),
            ),
        )
        await self.conn.commit()
        return merged

    async def list_configured_guilds(self) -> list[GuildSettings]:
        async with self.conn.execute("SELECT * FROM guild_settings") as cursor:
            rows = await cursor.fetchall()
            return [GuildSettings.from_row(row) for row in rows]

    # --- Synced Scheduled Events ---

    async def is_event_synced(
        self, guild_id: int, season: int, round_num: int, session_name: str
    ) -> bool:
        async with self.conn.execute(
            """
            SELECT 1 FROM synced_events
            WHERE guild_id = ? AND season = ? AND round_num = ? AND session_name = ?
            """,
            (guild_id, season, round_num, session_name),
        ) as cursor:
            return (await cursor.fetchone()) is not None

    async def mark_event_synced(
        self,
        guild_id: int,
        season: int,
        round_num: int,
        session_name: str,
        discord_event_id: int,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO synced_events (
                guild_id, season, round_num, session_name, discord_event_id
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, season, round_num, session_name) DO UPDATE SET
                discord_event_id = excluded.discord_event_id
            """,
            (guild_id, season, round_num, session_name, discord_event_id),
        )
        await self.conn.commit()

    # --- Sent Session Reminders ---

    async def is_reminder_sent(
        self, guild_id: int, season: int, round_num: int, session_name: str
    ) -> bool:
        async with self.conn.execute(
            """
            SELECT 1 FROM sent_reminders
            WHERE guild_id = ? AND season = ? AND round_num = ? AND session_name = ?
            """,
            (guild_id, season, round_num, session_name),
        ) as cursor:
            return (await cursor.fetchone()) is not None

    async def mark_reminder_sent(
        self,
        guild_id: int,
        season: int,
        round_num: int,
        session_name: str,
        sent_at: datetime | None = None,
    ) -> None:
        timestamp = _format_dt(sent_at or datetime.now(timezone.utc))
        await self.conn.execute(
            """
            INSERT INTO sent_reminders (
                guild_id, season, round_num, session_name, sent_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, season, round_num, session_name) DO UPDATE SET
                sent_at = excluded.sent_at
            """,
            (guild_id, season, round_num, session_name, timestamp),
        )
        await self.conn.commit()

    # --- Watch Parties ---

    async def create_watch_party(
        self,
        *,
        guild_id: int,
        text_channel_id: int,
        thread_id: int,
        voice_channel_id: int | None,
        session_key: int | None,
        session_label: str,
        spoiler_delay_seconds: int = 45,
        is_replay: bool = False,
        created_at: datetime | None = None,
    ) -> WatchParty:
        now = created_at or datetime.now(timezone.utc)
        cursor = await self.conn.execute(
            """
            INSERT INTO watch_parties (
                guild_id,
                text_channel_id,
                thread_id,
                voice_channel_id,
                session_key,
                session_label,
                spoiler_delay_seconds,
                sync_started_at,
                sync_offset_seconds,
                is_replay,
                empty_since,
                active,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, NULL, 1, ?)
            """,
            (
                guild_id,
                text_channel_id,
                thread_id,
                voice_channel_id,
                session_key,
                session_label,
                spoiler_delay_seconds,
                int(is_replay),
                _format_dt(now),
            ),
        )
        await self.conn.commit()
        party_id = cursor.lastrowid
        assert party_id is not None
        party = await self.get_watch_party(party_id)
        assert party is not None
        return party

    async def get_watch_party(self, party_id: int) -> WatchParty | None:
        async with self.conn.execute(
            "SELECT * FROM watch_parties WHERE id = ?",
            (party_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return WatchParty.from_row(row) if row is not None else None

    async def get_active_watch_party(self, guild_id: int) -> WatchParty | None:
        async with self.conn.execute(
            """
            SELECT * FROM watch_parties
            WHERE guild_id = ? AND active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return WatchParty.from_row(row) if row is not None else None

    async def list_active_watch_parties(self) -> list[WatchParty]:
        async with self.conn.execute(
            "SELECT * FROM watch_parties WHERE active = 1 ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [WatchParty.from_row(row) for row in rows]

    async def update_watch_party_sync(
        self,
        party_id: int,
        sync_started_at: datetime | None,
        sync_offset_seconds: int,
    ) -> None:
        formatted = _format_dt(sync_started_at) if sync_started_at else None
        await self.conn.execute(
            """
            UPDATE watch_parties
            SET sync_started_at = ?, sync_offset_seconds = ?
            WHERE id = ?
            """,
            (formatted, sync_offset_seconds, party_id),
        )
        await self.conn.commit()

    async def update_watch_party_empty_since(
        self,
        party_id: int,
        empty_since: datetime | None,
    ) -> None:
        formatted = _format_dt(empty_since) if empty_since else None
        await self.conn.execute(
            "UPDATE watch_parties SET empty_since = ? WHERE id = ?",
            (formatted, party_id),
        )
        await self.conn.commit()

    async def close_watch_party(self, party_id: int) -> None:
        await self.conn.execute(
            "UPDATE watch_parties SET active = 0, empty_since = NULL WHERE id = ?",
            (party_id,),
        )
        await self.conn.commit()
