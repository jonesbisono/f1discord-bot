"""Async client for the OpenF1 API with optional OAuth2 live-session authentication."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.openf1.org/v1"
TOKEN_URL = "https://api.openf1.org/token"


class OpenF1AuthRequiredError(Exception):
    """Raised when an OpenF1 endpoint requires sponsor authentication (e.g. live sessions)."""


def parse_openf1_timestamp(value: str) -> datetime:
    """Parse an OpenF1 ISO-8601 timestamp into a timezone-aware UTC datetime."""
    clean = value.strip()
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class OpenF1Client:
    """Async OpenF1 HTTP client supporting both unauthenticated historical and OAuth2 live access."""

    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=15.0,
        )
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def has_credentials(self) -> bool:
        return bool(self._username and self._password)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _refresh_token(self) -> str:
        if not self._username or not self._password:
            raise OpenF1AuthRequiredError(
                "OpenF1 live session access requires OPENF1_USERNAME and OPENF1_PASSWORD."
            )

        response = await self._client.post(
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "username": self._username,
                "password": self._password,
            },
        )
        if response.status_code in (401, 403):
            raise OpenF1AuthRequiredError("OpenF1 authentication failed (invalid credentials).")
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise OpenF1AuthRequiredError("OpenF1 token response missing access_token.")

        expires_in = float(payload.get("expires_in", 3600))
        self._access_token = str(token)
        self._token_expires_at = time.monotonic() + max(expires_in - 60.0, 30.0)
        return self._access_token

    async def _ensure_token(self, *, force_refresh: bool = False) -> str | None:
        if not self.has_credentials:
            return None
        if force_refresh or self._access_token is None or time.monotonic() >= self._token_expires_at:
            return await self._refresh_token()
        return self._access_token

    async def _get_list(self, path_with_query: str) -> list[dict[str, Any]]:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = await self._client.get(path_with_query, headers=headers)

        if response.status_code == 401 and self.has_credentials:
            token = await self._ensure_token(force_refresh=True)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = await self._client.get(path_with_query, headers=headers)

        if response.status_code in (401, 403):
            raise OpenF1AuthRequiredError(
                "OpenF1 returned unauthorized for this endpoint. "
                "Live race data requires an OpenF1 sponsor account."
            )

        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []

    @staticmethod
    def _build_session_query(
        endpoint: str,
        session_key: int | str,
        since_iso: str | None = None,
    ) -> str:
        query = f"/{endpoint}?session_key={session_key}"
        if since_iso:
            encoded_since = quote(since_iso, safe=":+-.TZ")
            query += f"&date>={encoded_since}"
        return query

    async def get_latest_session(self) -> dict[str, Any] | None:
        sessions = await self._get_list("/sessions?session_key=latest")
        return sessions[0] if sessions else None

    async def get_session(self, session_key: int) -> dict[str, Any] | None:
        sessions = await self._get_list(f"/sessions?session_key={session_key}")
        return sessions[0] if sessions else None

    async def get_race_control(
        self,
        session_key: int | str,
        since_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        query = self._build_session_query("race_control", session_key, since_iso)
        return await self._get_list(query)

    async def get_weather(
        self,
        session_key: int | str,
        since_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        query = self._build_session_query("weather", session_key, since_iso)
        return await self._get_list(query)

    async def get_pit_stops(
        self,
        session_key: int | str,
        since_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        query = self._build_session_query("pit", session_key, since_iso)
        return await self._get_list(query)

    async def get_drivers(self, session_key: int | str) -> list[dict[str, Any]]:
        return await self._get_list(f"/drivers?session_key={session_key}")
