"""The expansion target and its per-turn log line (roadmap item 1.41).

Cities and what is in them make up most of the score gap, so the bot follows `SCHEDULE`, a
front-loaded list of (turn, cities) points, up to a ceiling `TARGET_CITIES`. `expected_cities`
reads it and a city count below it is "behind"; `settler_cap` says how many cities settlers may
reach. Production reads `ExpansionState` to raise the settler priority
while behind and safe sites exist. Settling fills the state when it surveys the map.

`ExpansionLog` is a turn handler that writes one line a turn for live checking. It runs first in
the turn, before anything changes the game, so its reads are the ones the monitors already made
and cost no game call. Site figures are therefore from the last survey, at most a turn old.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from civ6_ai.state import read_cities

log = logging.getLogger(__name__)

# Where the schedule comes from (issue 316): a strong human on the same map held 2 cities at turn
# 28, 3 at 48, 4 at 60, 9 at 100 and 15 at 140. The old straight line to 6 by turn 150 expected only
# 2 cities at turn 58, so the bot was never "behind" while it was far behind a human. These points
# sit well under the human's late numbers (the bot defends and settles more carefully) but ahead of
# the CivBench medians at turn 100 (4.0 to 4.6 cities). Each point is (turn, cities held by then).
# Between points the expectation steps: it holds the earlier value until the next turn is reached.
SCHEDULE: tuple[tuple[int, int], ...] = (
    (0, 1),
    (25, 2),
    (45, 3),
    (60, 4),
    (90, 6),
    (120, 8),
    (150, 9),
)
# The long-run ceiling on cities (and the default `target_cities`). The policy document can lower
# or raise it. Settlers stop here whatever the schedule says.
TARGET_CITIES = 12
STALE_TURNS = 10  # A survey older than this says nothing about sites.
SETTLER = "UNIT_SETTLER"


def expected_cities(
    turn: int,
    ceiling: int = TARGET_CITIES,
    schedule: tuple[tuple[int, int], ...] = SCHEDULE,
) -> int:
    """Cities we should hold by `turn`: the last schedule point reached, never above `ceiling`.

    Past the last point the figure stays there; the ceiling only ever lowers it.
    """
    expected = 1
    for from_turn, cities in schedule:
        if turn >= from_turn:
            expected = cities
    return max(1, min(ceiling, expected))


def settler_cap(
    turn: int,
    cities: int,
    sites_ok: bool,
    ceiling: int = TARGET_CITIES,
    schedule: tuple[tuple[int, int], ...] = SCHEDULE,
) -> int:
    """Most cities (held plus settlers in flight) before a settler stops being built.

    The schedule plus one lets the bot run a city ahead of it. While a safe site is known the cap
    also stays one above the cities held, so the schedule never stops a bot that is finding room
    (the old fixed 6 did, at turn 150 and beyond). With no safe site only the schedule's cap
    applies. The ceiling bounds both. This only raises how many settlers may be built; safety
    (threat, sites, escorts) is checked elsewhere and is not relaxed by it.
    """
    cap = expected_cities(turn, ceiling, schedule) + 1
    if sites_ok:
        cap = max(cap, cities + 1)
    return min(ceiling, cap)


@dataclass
class ExpansionState:
    """What settling last saw, shared with production and the log line."""

    survey_turn: int | None = None
    safe_sites: int = 0  # Sites a settler could be sent to now.
    best_site_score: float | None = None  # Best adjusted score among them.
    settlers_on_map: int = 0
    # Settlers that could move this turn and were held (no site, or no safe route and no escort).
    # Production builds no settler while one is held: the next could do no more (issue 283).
    held_settlers: int = 0
    target_cities: int = TARGET_CITIES  # Set by production from its weights.
    # Why surveyed sites were refused, by reason, so a stall shows its cause in the log line.
    rejected: dict[str, int] = field(default_factory=dict)

    def sites_ok(self, turn: int) -> bool:
        """False only when a recent survey found no safe site. Unknown counts as ok."""
        if self.survey_turn is None or turn - self.survey_turn > STALE_TURNS:
            return True
        return self.safe_sites > 0


def expansion_line(
    turn: int,
    cities: int,
    settlers_building: int,
    state: ExpansionState,
    target: int = TARGET_CITIES,
) -> str:
    expected = expected_cities(turn, target)
    best = "unknown" if state.best_site_score is None else f"{state.best_site_score:.0f}"
    age = "none" if state.survey_turn is None else str(turn - state.survey_turn)
    return (
        f"expansion: turn={turn} cities={cities} ceiling={target} "
        f"expected={expected} behind={max(0, expected - cities)} "
        f"settlers_building={settlers_building} settlers_map={state.settlers_on_map} "
        f"safe_sites={state.safe_sites} best_site={best} survey_age={age}"
        + (
            " rejected=" + ",".join(f"{k}:{v}" for k, v in sorted(state.rejected.items()))
            if state.rejected
            else ""
        )
    )


class ExpansionLog:
    """Turn handler: log one expansion line a turn."""

    name = "expansion_log"

    def __init__(self, state: ExpansionState) -> None:
        self.state = state

    async def run(self, game: Any) -> None:
        turn = (await game.get_game_overview()).turn
        cities = await read_cities(game)
        building = sum(1 for c in cities if c.building == SETTLER)
        log.info("%s", expansion_line(turn, len(cities), building, self.state))
