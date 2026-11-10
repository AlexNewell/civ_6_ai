"""A stall watchdog: notice a game that has stopped making turn progress, and escalate.

Steps, each measured from the last turn that finished:

1. `warn_after`: log a warning.
2. `recover_after`: call the recovery hook once (the live slice supplies it).
3. `fail_after`: give up, cancel the game and report it as stalled.

Thresholds. In Run 3 slow turns took about 10 minutes, and the one hang (turn 156) ran for 12
minutes before `end_turn` reported it. civ6-mcp itself polls for about 9 minutes before it acts.
So warning at 20 minutes is about twice the slowest normal turn, the hook runs at 25 and the
game is failed at 35. A false alarm costs a recovery attempt. A missed stall costs a night.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from civ6_ai.runner.clock import Clock

log = logging.getLogger(__name__)

RecoveryHook = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class StallThresholds:
    warn_after: float = 20 * 60
    recover_after: float = 25 * 60
    fail_after: float = 35 * 60
    poll_every: float = 30.0

    def __post_init__(self) -> None:
        if not 0 < self.warn_after <= self.recover_after <= self.fail_after:
            raise ValueError("thresholds must satisfy 0 < warn <= recover <= fail")
        if self.poll_every <= 0:
            raise ValueError("poll_every must be positive")


class Step(StrEnum):
    NONE = "none"
    WARN = "warn"
    RECOVER = "recover"
    FAIL = "fail"


@dataclass
class Progress:
    """Where the game has got to. The game side calls `beat` after every finished turn."""

    clock: Clock
    turns: int = 0
    last_turn: int | None = None
    last_at: float = field(init=False)
    records: list = field(default_factory=list)  # The `TurnRecord`s, kept for the run record.
    log_paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.last_at = self.clock.monotonic()

    def beat(self, turn: int | None = None, record: object | None = None) -> None:
        self.turns += 1
        self.last_turn = turn
        if record is not None:
            self.records.append(record)
        self.last_at = self.clock.monotonic()


class Watchdog:
    def __init__(
        self, progress: Progress, thresholds: StallThresholds, recover: RecoveryHook | None = None
    ) -> None:
        self._progress = progress
        self._t = thresholds
        self._recover = recover
        self._seen_turns = progress.turns
        self._done: set[Step] = set()
        self.reason = ""

    def stalled_for(self) -> float:
        return self._progress.clock.monotonic() - self._progress.last_at

    def next_step(self) -> Step:
        """The next step due, if any. Each step is returned once per stall."""
        if self._progress.turns != self._seen_turns:
            self._seen_turns = self._progress.turns
            self._done.clear()  # The game moved, so a later stall starts again from the top.
        idle = self.stalled_for()
        for step, after in (
            (Step.FAIL, self._t.fail_after),
            (Step.RECOVER, self._t.recover_after),
            (Step.WARN, self._t.warn_after),
        ):
            if idle >= after and step not in self._done:
                self._done.add(step)
                return step
        return Step.NONE

    async def _act(self, step: Step) -> bool:
        """Carry out a step. True if the game should be failed."""
        idle = self.stalled_for()
        if step is Step.WARN:
            log.warning("No turn finished for %.0fs (last turn %s)", idle, self._progress.last_turn)
        elif step is Step.RECOVER:
            log.warning("Stalled for %.0fs, running the recovery hook", idle)
            if self._recover is None:
                log.warning("No recovery hook is set, so nothing was tried")
            else:
                try:
                    await self._recover()
                except Exception:
                    log.exception("The recovery hook failed")
        elif step is Step.FAIL:
            self.reason = f"stalled: no turn finished for {idle:.0f}s"
            log.error("Failing the game: %s", self.reason)
            return True
        return False

    async def supervise(self, game: "asyncio.Task") -> bool:
        """Watch a running game task. True if it was cancelled as stalled."""
        while not game.done():
            await self._progress.clock.sleep(self._t.poll_every)
            if game.done():
                break
            if await self._act(self.next_step()):
                game.cancel()
                await asyncio.gather(game, return_exceptions=True)
                return True
        return False
