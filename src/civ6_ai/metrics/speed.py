"""How long our player takes to play a turn, measured against the speed targets.

A turn's `total_seconds` has two parts. Our *decision* time is everything before the end-turn
request: reading state, running the policies and any model calls. The *end-turn wait* is the game
processing the turn, including every other player's moves, which a human sees in any Civ game.
The targets are about decision time, because that is what each LLM opponent adds to a human's wait.
Both are reported, so a slow engine doesn't hide behind a fast bot or the other way round.

The targets are in docs/decisions.md under "Speed". The development bar is a monitored figure, not
an acceptance criterion.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import mean, median

DEV_BAR_SECONDS = 60.0
GOAL_SECONDS = 10.0


@dataclass(frozen=True)
class Spread:
    mean: float
    median: float
    p90: float
    max: float

    def __str__(self) -> str:
        return (
            f"mean {self.mean:.1f}s, median {self.median:.1f}s, "
            f"p90 {self.p90:.1f}s, max {self.max:.1f}s"
        )


@dataclass(frozen=True)
class SpeedSummary:
    turns: int
    decision: Spread
    end_turn: Spread
    total: Spread

    @property
    def over_dev_bar(self) -> bool:
        return self.decision.mean > DEV_BAR_SECONDS

    @property
    def over_goal(self) -> bool:
        return self.decision.mean > GOAL_SECONDS

    def lines(self) -> list[str]:
        if self.over_dev_bar:
            verdict = f"over the {DEV_BAR_SECONDS:.0f}s development bar"
        elif self.over_goal:
            verdict = (
                f"under the {DEV_BAR_SECONDS:.0f}s development bar, "
                f"over the {GOAL_SECONDS:.0f}s goal"
            )
        else:
            verdict = f"under the {GOAL_SECONDS:.0f}s goal"
        return [
            f"Decision time per turn: {self.decision} ({verdict})",
            f"End-turn wait per turn: {self.end_turn}",
            f"Total per turn: {self.total}",
        ]


def _spread(values: list[float]) -> Spread:
    ordered = sorted(values)
    # Nearest rank: the smallest value at or above 90% of the turns.
    p90 = ordered[max(0, -(-len(ordered) * 9 // 10) - 1)]
    return Spread(mean(ordered), median(ordered), p90, ordered[-1])


def summarise(turns: Iterable[tuple[float, float]]) -> SpeedSummary | None:
    """Summarise `(total_seconds, end_turn_seconds)` pairs, one per turn. None for no turns."""
    pairs = list(turns)
    if not pairs:
        return None
    return SpeedSummary(
        turns=len(pairs),
        # Clamped: a clock adjustment must not report a negative decision time.
        decision=_spread([max(0.0, total - end) for total, end in pairs]),
        end_turn=_spread([end for _, end in pairs]),
        total=_spread([total for total, _ in pairs]),
    )
