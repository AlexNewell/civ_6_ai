"""Count and time every game call, per handler, so a live run shows where the seconds go.

Each call to civ6-mcp's `GameState` is a round trip to the game that costs a fixed few tenths of
a second whatever it reads, so a handler's time is mostly how many calls it makes. The turn loop
tells the timer which handler is running. Calls made outside a handler (reading the turn, ending
it) are not counted. Overhead is two clock reads and a dict update per call.
"""

import inspect
import time
from collections.abc import Callable
from functools import wraps
from typing import Any


class CallTimer:
    """A game that records `[count, seconds]` per method for the handler now running."""

    def __init__(self, game: Any) -> None:
        self._game = game
        self._handler: str | None = None
        self._calls: dict[str, dict[str, list[float]]] = {}

    def begin(self, handler: str | None) -> None:
        """Attribute the calls that follow to this handler, or to none."""
        self._handler = handler

    def take(self) -> dict[str, dict[str, list[float]]]:
        """Return what was recorded since the last `take`, with seconds rounded, and start over."""
        taken, self._calls = self._calls, {}
        return {
            handler: {name: [int(n), round(seconds, 3)] for name, (n, seconds) in methods.items()}
            for handler, methods in taken.items()
        }

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._game, name)
        if not inspect.iscoroutinefunction(attribute):
            return attribute  # Only game calls are timed. Anything else passes straight through.
        return self._timed(name, attribute)

    def _timed(self, name: str, call: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(call)
        async def timed(*args: Any, **kwargs: Any) -> Any:
            handler = self._handler
            started = time.perf_counter()
            try:
                return await call(*args, **kwargs)
            finally:
                if handler is not None:
                    entry = self._calls.setdefault(handler, {}).setdefault(name, [0, 0.0])
                    entry[0] += 1
                    entry[1] += time.perf_counter() - started

        return timed
