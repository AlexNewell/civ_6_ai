"""The expansion target and its per-turn log line (roadmap item 1.41).

Cities and what is in them make up most of the score gap, so the bot aims for `TARGET_CITIES`
living cities by `TARGET_TURN`. `expected_cities` is the straight-line schedule towards that, and a
city count below it is "behind". Production reads `ExpansionState` to raise the settler priority
while behind and safe sites exist. Settling fills the state when it surveys the map.

`ExpansionLog` is a turn handler that writes one line a turn for live checking. It runs first in
the turn, before anything changes the game, so its reads are the ones the monitors already made
and cost no game call. Site figures are therefore from the last survey, at most a turn old.
"""

import logging
import math
from dataclasses import dataclass
from typing import Any

from civ6_ai.state import read_cities

log = logging.getLogger(__name__)

TARGET_CITIES = 6
TARGET_TURN = 150
STALE_TURNS = 10  # A survey older than this says nothing about sites.
SETTLER = "UNIT_SETTLER"


def expected_cities(turn: int, target: int = TARGET_CITIES, by_turn: int = TARGET_TURN) -> int:
    """Cities we should hold by `turn`: one at the start, rising in a line to `target`."""
    if turn >= by_turn:
        return target
    return min(target, 1 + math.floor((target - 1) * max(turn, 0) / by_turn))


@dataclass
class ExpansionState:
    """What settling last saw, shared with production and the log line."""

    survey_turn: int | None = None
    safe_sites: int = 0  # Sites a settler could be sent to now.
    best_site_score: float | None = None  # Best adjusted score among them.
    settlers_on_map: int = 0

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
    by_turn: int = TARGET_TURN,
) -> str:
    expected = expected_cities(turn, target, by_turn)
    best = "unknown" if state.best_site_score is None else f"{state.best_site_score:.0f}"
    age = "none" if state.survey_turn is None else str(turn - state.survey_turn)
    return (
        f"expansion: turn={turn} cities={cities} target={target}@{by_turn} "
        f"expected={expected} behind={max(0, expected - cities)} "
        f"settlers_building={settlers_building} settlers_map={state.settlers_on_map} "
        f"safe_sites={state.safe_sites} best_site={best} survey_age={age}"
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
