"""Unit tests for Jolpica-F1 and OpenF1 HTTP clients using respx."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from f1_bot.clients.jolpica import USER_AGENT, JolpicaClient
from f1_bot.clients.openf1 import OpenF1AuthRequiredError, OpenF1Client


@respx.mock
async def test_jolpica_schedule_parsing_cache_and_next_race() -> None:
    schedule_payload = {
        "MRData": {
            "RaceTable": {
                "Races": [
                    {
                        "season": "2026",
                        "round": "1",
                        "raceName": "Australian Grand Prix",
                        "Circuit": {
                            "circuitName": "Albert Park Grand Prix Circuit",
                            "Location": {"locality": "Melbourne", "country": "Australia"},
                        },
                        "date": "2026-03-15",
                        "time": "04:00:00Z",
                        "FirstPractice": {"date": "2026-03-13", "time": "01:30:00Z"},
                        "Qualifying": {"date": "2026-03-14", "time": "05:00:00Z"},
                    },
                    {
                        "season": "2026",
                        "round": "2",
                        "raceName": "Chinese Grand Prix",
                        "Circuit": {
                            "circuitName": "Shanghai International Circuit",
                            "Location": {"locality": "Shanghai", "country": "China"},
                        },
                        "date": "2026-03-22",
                        "time": "07:00:00Z",
                        "SprintQualifying": {"date": "2026-03-20", "time": "07:30:00Z"},
                        "Sprint": {"date": "2026-03-21", "time": "03:00:00Z"},
                        "Qualifying": {"date": "2026-03-21", "time": "07:00:00Z"},
                    },
                ]
            }
        }
    }

    route = respx.get("https://api.jolpi.ca/ergast/f1/current.json?limit=100").mock(
        return_value=httpx.Response(200, json=schedule_payload)
    )

    client = JolpicaClient(schedule_ttl_seconds=900)
    try:
        weekends = await client.get_schedule("current")
        assert len(weekends) == 2
        assert route.call_count == 1
        assert route.calls[0].request.headers["User-Agent"] == USER_AGENT

        assert weekends[0].race_name == "Australian Grand Prix"
        assert [s.name for s in weekends[0].sessions] == ["FP1", "Qualifying", "Race"]
        assert weekends[1].sessions[0].name == "Sprint Qualifying"

        # Second call hits TTL cache
        next_gp = await client.get_next_race(
            now=datetime(2026, 3, 15, 5, 0, 0, tzinfo=timezone.utc)
        )
        assert next_gp is not None
        assert next_gp.round_num == 1
        assert route.call_count == 1

        after_r1 = await client.get_next_race(
            now=datetime(2026, 3, 15, 8, 0, 0, tzinfo=timezone.utc)
        )
        assert after_r1 is not None
        assert after_r1.round_num == 2
    finally:
        await client.close()


@respx.mock
async def test_jolpica_standings_and_results() -> None:
    respx.get("https://api.jolpi.ca/ergast/f1/2026/driverstandings.json?limit=100").mock(
        return_value=httpx.Response(
            200,
            json={
                "MRData": {
                    "StandingsTable": {
                        "StandingsLists": [
                            {
                                "season": "2026",
                                "DriverStandings": [
                                    {
                                        "position": "1",
                                        "positionText": "1",
                                        "points": "400",
                                        "wins": "9",
                                        "Driver": {
                                            "code": "NOR",
                                            "givenName": "Lando",
                                            "familyName": "Norris",
                                        },
                                        "Constructors": [{"name": "McLaren"}],
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
        )
    )
    respx.get("https://api.jolpi.ca/ergast/f1/2026/constructorstandings.json?limit=100").mock(
        return_value=httpx.Response(
            200,
            json={
                "MRData": {
                    "StandingsTable": {
                        "StandingsLists": [
                            {
                                "season": "2026",
                                "ConstructorStandings": [
                                    {
                                        "position": "1",
                                        "positionText": "1",
                                        "points": "710",
                                        "wins": "14",
                                        "Constructor": {"name": "McLaren"},
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
        )
    )
    respx.get("https://api.jolpi.ca/ergast/f1/current/last/results.json?limit=100").mock(
        return_value=httpx.Response(
            200,
            json={
                "MRData": {
                    "RaceTable": {
                        "Races": [
                            {
                                "season": "2026",
                                "round": "20",
                                "raceName": "Las Vegas Grand Prix",
                                "date": "2026-11-21",
                                "Circuit": {"circuitName": "Las Vegas Strip Circuit"},
                                "Results": [
                                    {
                                        "position": "1",
                                        "positionText": "1",
                                        "points": "25",
                                        "grid": "3",
                                        "laps": "50",
                                        "status": "Finished",
                                        "Time": {"time": "1:22:10.123"},
                                        "Driver": {
                                            "code": "VER",
                                            "givenName": "Max",
                                            "familyName": "Verstappen",
                                        },
                                        "Constructor": {"name": "Red Bull"},
                                        "FastestLap": {"rank": "1"},
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
        )
    )

    client = JolpicaClient()
    try:
        season, drivers = await client.get_driver_standings(2026)
        assert season == 2026
        assert drivers[0].driver_code == "NOR"
        assert drivers[0].points == 400.0

        c_season, constructors = await client.get_constructor_standings(2026)
        assert c_season == 2026
        assert constructors[0].constructor_name == "McLaren"

        results = await client.get_race_results("current", "last")
        assert results is not None
        assert results.race_name == "Las Vegas Grand Prix"
        assert results.entries[0].fastest_lap_rank == 1
        assert results.entries[0].time_text == "1:22:10.123"
    finally:
        await client.close()


@respx.mock
async def test_openf1_auth_token_refresh_and_error() -> None:
    token_route = respx.post("https://api.openf1.org/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600}),
        ]
    )
    session_route = respx.get("https://api.openf1.org/v1/sessions").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=[{"session_key": 9999, "session_name": "Race"}]),
        ]
    )

    authed_client = OpenF1Client(username="u", password="p")
    try:
        latest = await authed_client.get_latest_session()
        assert latest is not None
        assert latest["session_key"] == 9999
        assert token_route.call_count == 2
        assert session_route.calls[0].request.headers["Authorization"] == "Bearer tok-1"
        assert session_route.calls[1].request.headers["Authorization"] == "Bearer tok-2"
    finally:
        await authed_client.close()

    respx.get("https://api.openf1.org/v1/race_control").mock(
        return_value=httpx.Response(403)
    )
    unauth_client = OpenF1Client()
    try:
        with pytest.raises(OpenF1AuthRequiredError):
            await unauth_client.get_race_control("latest")
    finally:
        await unauth_client.close()
