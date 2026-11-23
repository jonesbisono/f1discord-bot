"""Async client for the Jolpica-F1 (Ergast-compatible) REST API."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

BASE_URL = "https://api.jolpi.ca/ergast/f1/"
USER_AGENT = "F1WatchPartyBot/1.0.0"


def parse_jolpica_datetime(date_str: str, time_str: str | None) -> datetime:
    """Parse Ergast/Jolpica date and optional UTC time strings into a UTC datetime."""
    clean_time = (time_str or "12:00:00Z").strip()
    if clean_time.endswith("Z"):
        clean_time = clean_time[:-1] + "+00:00"
    elif "+" not in clean_time and "-" not in clean_time:
        clean_time = clean_time + "+00:00"
    dt = datetime.fromisoformat(f"{date_str.strip()}T{clean_time}")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(slots=True)
class SessionInfo:
    name: str
    start_time_utc: datetime


@dataclass(slots=True)
class RaceWeekend:
    season: int
    round_num: int
    race_name: str
    circuit_name: str
    locality: str
    country: str
    sessions: list[SessionInfo]

    @property
    def race_session(self) -> SessionInfo | None:
        for session in self.sessions:
            if session.name == "Race":
                return session
        return self.sessions[-1] if self.sessions else None


@dataclass(slots=True)
class DriverStanding:
    position: int
    position_text: str
    driver_code: str
    driver_name: str
    constructor_name: str
    points: float
    wins: int


@dataclass(slots=True)
class ConstructorStanding:
    position: int
    position_text: str
    constructor_name: str
    points: float
    wins: int


@dataclass(slots=True)
class RaceResultEntry:
    position: int
    position_text: str
    driver_code: str
    driver_name: str
    constructor_name: str
    grid: int
    laps: int
    status: str
    time_text: str | None
    points: float
    fastest_lap_rank: int | None


@dataclass(slots=True)
class RaceResults:
    season: int
    round_num: int
    race_name: str
    circuit_name: str
    date: str
    entries: list[RaceResultEntry]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class JolpicaClient:
    """Async Jolpica-F1 HTTP client with TTL caching."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        schedule_ttl_seconds: int = 900,
        stats_ttl_seconds: int = 300,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
        )
        self._schedule_ttl = schedule_ttl_seconds
        self._stats_ttl = stats_ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def clear_cache(self) -> None:
        self._cache.clear()

    async def _get_json(self, path: str, *, ttl_seconds: int) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._cache.get(path)
        if cached is not None:
            expires_at, data = cached
            if now < expires_at:
                return data

        response = await self._client.get(path, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        payload = response.json()
        self._cache[path] = (now + ttl_seconds, payload)
        return payload

    async def get_schedule(self, season: int | str = "current") -> list[RaceWeekend]:
        payload = await self._get_json(
            f"{season}.json?limit=100",
            ttl_seconds=self._schedule_ttl,
        )
        races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
        weekends: list[RaceWeekend] = []

        session_keys = (
            ("FirstPractice", "FP1"),
            ("SecondPractice", "FP2"),
            ("ThirdPractice", "FP3"),
            ("SprintQualifying", "Sprint Qualifying"),
            ("Sprint", "Sprint"),
            ("Qualifying", "Qualifying"),
        )

        for race in races:
            sessions: list[SessionInfo] = []
            for json_key, label in session_keys:
                raw_session = race.get(json_key)
                if raw_session and raw_session.get("date"):
                    sessions.append(
                        SessionInfo(
                            name=label,
                            start_time_utc=parse_jolpica_datetime(
                                raw_session["date"], raw_session.get("time")
                            ),
                        )
                    )

            if race.get("date"):
                sessions.append(
                    SessionInfo(
                        name="Race",
                        start_time_utc=parse_jolpica_datetime(
                            race["date"], race.get("time")
                        ),
                    )
                )

            sessions.sort(key=lambda s: s.start_time_utc)
            circuit = race.get("Circuit", {})
            location = circuit.get("Location", {})
            weekends.append(
                RaceWeekend(
                    season=_safe_int(race.get("season")),
                    round_num=_safe_int(race.get("round")),
                    race_name=str(race.get("raceName", "Grand Prix")),
                    circuit_name=str(circuit.get("circuitName", "Unknown Circuit")),
                    locality=str(location.get("locality", "")),
                    country=str(location.get("country", "")),
                    sessions=sessions,
                )
            )

        return weekends

    async def get_next_race(self, now: datetime | None = None) -> RaceWeekend | None:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        schedule = await self.get_schedule("current")
        for weekend in schedule:
            race_session = weekend.race_session
            if race_session is None:
                continue
            if race_session.start_time_utc + timedelta(hours=3) >= current_time:
                return weekend
        return None

    async def get_driver_standings(
        self, season: int | str = "current"
    ) -> tuple[int, list[DriverStanding]]:
        payload = await self._get_json(
            f"{season}/driverstandings.json?limit=100",
            ttl_seconds=self._stats_ttl,
        )
        standings_lists = (
            payload.get("MRData", {}).get("StandingsTable", {}).get("StandingsLists", [])
        )
        if not standings_lists:
            return (0, [])

        latest = standings_lists[0]
        resolved_season = _safe_int(latest.get("season"))
        entries: list[DriverStanding] = []
        for idx, item in enumerate(latest.get("DriverStandings", []), start=1):
            driver = item.get("Driver", {})
            constructors = item.get("Constructors", [])
            constructor_name = (
                constructors[0].get("name", "Unknown") if constructors else "Unknown"
            )
            full_name = (
                f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()
            )
            code = driver.get("code") or driver.get("familyName", "UNK")[:3].upper()
            pos_text = str(item.get("positionText") or item.get("position") or idx)
            entries.append(
                DriverStanding(
                    position=_safe_int(item.get("position"), idx),
                    position_text=pos_text,
                    driver_code=str(code),
                    driver_name=full_name or str(code),
                    constructor_name=str(constructor_name),
                    points=_safe_float(item.get("points")),
                    wins=_safe_int(item.get("wins")),
                )
            )
        return (resolved_season, entries)

    async def get_constructor_standings(
        self, season: int | str = "current"
    ) -> tuple[int, list[ConstructorStanding]]:
        payload = await self._get_json(
            f"{season}/constructorstandings.json?limit=100",
            ttl_seconds=self._stats_ttl,
        )
        standings_lists = (
            payload.get("MRData", {}).get("StandingsTable", {}).get("StandingsLists", [])
        )
        if not standings_lists:
            return (0, [])

        latest = standings_lists[0]
        resolved_season = _safe_int(latest.get("season"))
        entries: list[ConstructorStanding] = []
        for idx, item in enumerate(latest.get("ConstructorStandings", []), start=1):
            constructor = item.get("Constructor", {})
            pos_text = str(item.get("positionText") or item.get("position") or idx)
            entries.append(
                ConstructorStanding(
                    position=_safe_int(item.get("position"), idx),
                    position_text=pos_text,
                    constructor_name=str(constructor.get("name", "Unknown")),
                    points=_safe_float(item.get("points")),
                    wins=_safe_int(item.get("wins")),
                )
            )
        return (resolved_season, entries)

    async def get_race_results(
        self,
        season: int | str = "current",
        round_num: int | str = "last",
    ) -> RaceResults | None:
        payload = await self._get_json(
            f"{season}/{round_num}/results.json?limit=100",
            ttl_seconds=self._stats_ttl,
        )
        races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
        if not races:
            return None

        race = races[0]
        circuit = race.get("Circuit", {})
        entries: list[RaceResultEntry] = []
        for idx, item in enumerate(race.get("Results", []), start=1):
            driver = item.get("Driver", {})
            constructor = item.get("Constructor", {})
            full_name = (
                f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip()
            )
            code = driver.get("code") or driver.get("familyName", "UNK")[:3].upper()
            time_info = item.get("Time") or {}
            fastest = item.get("FastestLap") or {}
            fastest_rank = _safe_int(fastest.get("rank")) if fastest.get("rank") else None
            pos_text = str(item.get("positionText") or item.get("position") or idx)
            entries.append(
                RaceResultEntry(
                    position=_safe_int(item.get("position"), idx),
                    position_text=pos_text,
                    driver_code=str(code),
                    driver_name=full_name or str(code),
                    constructor_name=str(constructor.get("name", "Unknown")),
                    grid=_safe_int(item.get("grid")),
                    laps=_safe_int(item.get("laps")),
                    status=str(item.get("status", "Finished")),
                    time_text=time_info.get("time"),
                    points=_safe_float(item.get("points")),
                    fastest_lap_rank=fastest_rank,
                )
            )

        return RaceResults(
            season=_safe_int(race.get("season")),
            round_num=_safe_int(race.get("round")),
            race_name=str(race.get("raceName", "Grand Prix")),
            circuit_name=str(circuit.get("circuitName", "Unknown Circuit")),
            date=str(race.get("date", "")),
            entries=entries,
        )
