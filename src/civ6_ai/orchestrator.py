"""The single process that owns the tuner connection and plays turns."""

import json
import logging
import time
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
    """Return the stop reason in `end_turn`'s text. civ6-mcp reports these without raising."""
    for prefix, reason in _REPORT_PREFIXES.items():
        if report.startswith(prefix):
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


class Orchestrator:
    def __init__(self, game: Game, handlers: list[Handler], turn_log: TurnLog) -> None:
        self._game = game
        self._handlers = handlers
        self._log = turn_log

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
        result = await self._game.end_turn()
        end_turn_seconds = time.perf_counter() - end_turn_started

        reason = stop_reason_in_report(result) or await self._check_after_end_turn(turn)

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
        return None if await self._read_turn() > turn_before else StopReason.BLOCKED

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
