"""Time as a dependency, so tests never sleep."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def timestamp(self) -> str:
        """The wall time as an ISO 8601 UTC string, for files."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def timestamp(self) -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """A clock where sleeping moves time on at once. For tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def timestamp(self) -> str:
        return datetime.fromtimestamp(1_700_000_000 + self.now, UTC).isoformat(timespec="seconds")

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)  # Let other tasks run, as a real sleep would.
