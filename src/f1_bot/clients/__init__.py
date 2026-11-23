"""External F1 API clients."""

from f1_bot.clients.jolpica import (
    ConstructorStanding,
    DriverStanding,
    JolpicaClient,
    RaceResultEntry,
    RaceResults,
    RaceWeekend,
    SessionInfo,
)
from f1_bot.clients.openf1 import OpenF1AuthRequiredError, OpenF1Client

__all__ = [
    "ConstructorStanding",
    "DriverStanding",
    "JolpicaClient",
    "OpenF1AuthRequiredError",
    "OpenF1Client",
    "RaceResultEntry",
    "RaceResults",
    "RaceWeekend",
    "SessionInfo",
]
