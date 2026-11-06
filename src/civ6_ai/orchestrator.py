"""The single process that owns the tuner connection and plays turns."""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

MAX_DETAIL_LENGTH = 300


class Game(Protocol):
    """The part of civ6-mcp's `GameState` that the turn loop uses."""

    async def check_game_over(self): ...

    async def get_game_overview(self): ...

    async def end_turn(self) -> str: ...


class Handler(Protocol):
    """One step of a turn. Handlers run in order before the turn ends."""

    name: str

    async def run(self, game: Game) -> None: ...


class DoNothing:
    """A handler with no effect, for proving the loop itself."""

    name = "do_nothing"

    async def run(self, game: Game) -> None:
        pass


class StopReason(StrEnum):
    TURN_LIMIT = "turn_limit"
    BLOCKED = "blocked"  # Something the handlers left unresolved stops the turn ending.
    HUNG = "hung"  # The turn was ended but never came back.
    GAME_OVER = "game_over"
    RUN_ABORTED = "run_aborted"  # civ6-mcp refused to continue the run.


_REPORT_PREFIXES = {
    "GAME OVER": StopReason.GAME_OVER,
    "RUN ABORTED": StopReason.RUN_ABORTED,
    "HANG:": StopReason.HUNG,
}


def stop_reason_in_report(report: str) -> StopReason | None:
    """Return the stop reason in `end_turn`'s text. civ6-mcp reports these without raising.

    A marker counts at the start of any line, because a report can open with a turn summary such
    as "Turn 5 -> 6" before it.
    """
    lines = [line.lstrip() for line in report.splitlines()]
    for prefix, reason in _REPORT_PREFIXES.items():
        if any(line.startswith(prefix) for line in lines):
            return reason
    return None


@dataclass(frozen=True)
class TurnRecord:
    turn: int
    handler_seconds: dict[str, float]
    handler_errors: dict[str, str]
    end_turn_seconds: float
    total_seconds: float
    stop_reason: str | None = None
    detail: str = ""


@dataclass
class TurnLog:
    """Writes one JSON line per turn, flushed as it goes so a crash loses nothing."""

    path: Path
    records: list[TurnRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: TurnRecord) -> None:
        self.records.append(record)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(record)) + "\n")


# Answers whether the local player has been defeated or eliminated. It's only asked after a turn
# failed to end, because it costs a game read.
DefeatCheck = Callable[[], Awaitable[bool]]


class Orchestrator:
    def __init__(
        self,
        game: Game,
        handlers: list[Handler],
        turn_log: TurnLog,
        defeat_check: DefeatCheck | None = None,
    ) -> None:
        self._game = game
        self._handlers = handlers
        self._log = turn_log
        self._defeat_check = defeat_check

    async def play(self, turns: int) -> StopReason:
        """Play up to `turns` turns, stopping early if the game can't continue."""
        for _ in range(turns):
            reason = await self._play_turn()
            if reason is not None:
                return reason
        return StopReason.TURN_LIMIT

    async def _play_turn(self) -> StopReason | None:
        if await self._game.check_game_over() is not None:
            return StopReason.GAME_OVER  # The run was started on a game that's already over.

        started = time.perf_counter()
        turn = await self._read_turn()
        seconds, errors = await self._run_handlers()

        end_turn_started = time.perf_counter()
        try:
            result = await self._game.end_turn()
        except ConnectionError:
            raise
        except Exception:
            # A defeat can make the call itself fail. If it wasn't one, this is still an error.
            if not await self._defeated():
                raise
            log.warning("end_turn failed and the player is defeated", exc_info=True)
            result = "GAME OVER: end_turn failed after the player was defeated"
        end_turn_seconds = time.perf_counter() - end_turn_started

        reason = stop_reason_in_report(result)
        if reason is None:
            reason = await self._check_after_end_turn(turn)
        if reason in (StopReason.BLOCKED, StopReason.HUNG) and await self._defeated():
            # With no city, the game-over check can't see a defeat, so the turn just never ends.
            reason = StopReason.GAME_OVER

        self._log.write(
            TurnRecord(
                turn=turn,
                handler_seconds=seconds,
                handler_errors=errors,
                end_turn_seconds=end_turn_seconds,
                total_seconds=time.perf_counter() - started,
                stop_reason=reason,
                detail=result[:MAX_DETAIL_LENGTH],
            )
        )
        return reason

    async def _check_after_end_turn(self, turn_before: int) -> StopReason | None:
        """Confirm the turn advanced. Only called when the report shows the game can be read."""
        # A defeat lands during the AI's turn, and normal reads can fail on the defeat screen.
        if await self._game.check_game_over() is not None:
            return StopReason.GAME_OVER
        try:
            if await self._read_turn() > turn_before:
                return None
            # A popup such as a natural disaster can hold the game's UI on the old turn although
            # the turn has ended (seen live on turn 1: the report said "Turn 1 -> 2" but the UI
            # still read 1). Clearing it lets the UI catch up.
            if await self._dismiss_popup() and await self._read_turn() > turn_before:
                return None
            return StopReason.BLOCKED
        except ConnectionError:
            raise
        except Exception:
            if await self._defeated():
                return StopReason.GAME_OVER  # Reads can fail once the defeat screen is up.
            raise

    async def _dismiss_popup(self) -> bool:
        """Clear any popup through civ6-mcp (Lua only, no clicks). True if the call was made."""
        dismiss = getattr(self._game, "dismiss_popup", None)
        if dismiss is None:
            return False
        try:
            log.info("Turn did not read as advanced; dismissed: %s", await dismiss())
        except ConnectionError:
            raise
        except Exception:
            log.exception("Popup dismissal failed")
            return False
        return True

    async def _defeated(self) -> bool:
        """Ask the defeat check, if there is one. A check that fails counts as not defeated."""
        if self._defeat_check is None:
            return False
        try:
            return await self._defeat_check()
        except ConnectionError:
            raise
        except Exception:
            log.exception("Defeat check failed")
            return False

    async def _run_handlers(self) -> tuple[dict[str, float], dict[str, str]]:
        """Run each handler, timing it. A failing handler must not stop the turn from ending."""
        seconds: dict[str, float] = {}
        errors: dict[str, str] = {}
        for handler in self._handlers:
            started = time.perf_counter()
            try:
                await handler.run(self._game)
            except ConnectionError:
                raise  # The game is gone, which no later handler can work around.
            except Exception as error:
                log.exception("Handler %s failed", handler.name)
                errors[handler.name] = repr(error)[:MAX_DETAIL_LENGTH]
            seconds[handler.name] = time.perf_counter() - started
        return seconds, errors

    async def _read_turn(self) -> int:
        return (await self._game.get_game_overview()).turn
