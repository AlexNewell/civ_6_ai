"""A threat state per city, shared by the military, production and gold policies.

A city counts as threatened (roadmap 1.35 and 1.36) when any of these holds:

- We are at war with a major civ. Every city counts, because Run 7's capital fell three turns after
  a declaration with no enemy unit seen at first (`docs/research/cities-lost.md`: 11 of 15 losses
  came within 10 turns of an AI's war).
- A hostile unit (a barbarian, or an owner we are at war with) is within `radius` tiles.
- A unit of a rival that has denounced us, or is unfriendly to us, is within `radius` tiles.

It is built from reads the other handlers make anyway (`get_diplomacy`, `get_threat_scan`,
`get_cities` and `get_units`), all of which `SharedReads` answers from memory within a turn. On a
turn with no war and no foreign unit in the scan it makes the two cheap reads and stops.

`DefencePolicy` fills a `DefenceState` first thing in the turn. The military, production and gold
policies read it, and add a note for each thing they do for a threatened city. `DefenceReport` runs
last and logs one line per threatened city: why, how many defenders, and what was done.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from civ6_ai.policies.military import is_land_military
from civ6_ai.state import City, Threat, Unit
from civ6_ai.state import read_cities as _read_cities
from civ6_ai.state import read_threats as _read_threats
from civ6_ai.state import read_units as _read_units
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance

log = logging.getLogger(__name__)

THREAT_RADIUS = 3  # Tiles. A land unit moves about 2 a turn, so this is one turn of warning.
WANTED_DEFENDERS = 2  # In the city or on a tile beside it.
# How a rival that means us harm shows up in civ6-mcp's `diplomatic_state`.
HOSTILE_STATES = ("DENOUNCED", "UNFRIENDLY", "WAR")


@dataclass(frozen=True)
class CityThreat:
    """Why a city is threatened and how well it is held."""

    city_id: int
    name: str
    x: int
    y: int
    reasons: tuple[str, ...]
    hostile_units: int = 0
    defenders_in: int = 0  # Land military units on the city tile.
    defenders_beside: int = 0  # On the six tiles around it.

    @property
    def defenders(self) -> int:
        return self.defenders_in + self.defenders_beside

    @property
    def short(self) -> bool:
        return self.defenders < WANTED_DEFENDERS


@dataclass
class DefenceState:
    """The threatened cities this turn, and what was done for each. One instance per game."""

    turn: int = 0
    cities: dict[int, CityThreat] = field(default_factory=dict)
    notes: dict[int, list[str]] = field(default_factory=dict)

    def reset(self, turn: int, cities: dict[int, CityThreat]) -> None:
        self.turn = turn
        self.cities = cities
        self.notes = {}

    @property
    def any(self) -> bool:
        return bool(self.cities)

    @property
    def threatened_ids(self) -> frozenset[int]:
        return frozenset(self.cities)

    @property
    def short_ids(self) -> frozenset[int]:
        return frozenset(i for i, c in self.cities.items() if c.short)

    def is_threatened(self, city_id: int) -> bool:
        return city_id in self.cities

    def note(self, city_id: int, text: str) -> None:
        if city_id in self.cities:
            self.notes.setdefault(city_id, []).append(text)

    def set_defenders(self, city_id: int, inside: int, beside: int) -> None:
        """Replace the counts after the military policy has moved units."""
        found = self.cities.get(city_id)
        if found is not None:
            self.cities[city_id] = CityThreat(
                found.city_id,
                found.name,
                found.x,
                found.y,
                found.reasons,
                found.hostile_units,
                inside,
                beside,
            )


def count_defenders(
    city: City, units: list[Unit], map_width: int | None = STANDARD_MAP_WIDTH
) -> tuple[int, int]:
    """Land military units in the city and beside it."""
    inside = beside = 0
    for unit in units:
        if not is_land_military(unit):
            continue
        distance = hex_distance(city.x, city.y, unit.x, unit.y, map_width)
        if distance == 0:
            inside += 1
        elif distance == 1:
            beside += 1
    return inside, beside


def assess(
    cities: list[City],
    units: list[Unit],
    threats: list[Threat] | tuple[Threat, ...],
    civs: list[Any],
    radius: int = THREAT_RADIUS,
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> dict[int, CityThreat]:
    """The threatened cities, by id. `civs` are `get_diplomacy` entries."""
    at_war = sorted(c.civ_name for c in civs if c.has_met and c.is_at_war)
    hostile_owners = {
        c.player_id for c in civs if c.has_met and c.diplomatic_state in HOSTILE_STATES
    }
    found: dict[int, CityThreat] = {}
    for city in cities:
        reasons: list[str] = []
        if at_war:
            reasons.append("war:" + "+".join(at_war))
        near = [
            t
            for t in threats
            if hex_distance(city.x, city.y, t.x, t.y, map_width) <= radius
            and (t.hostile or t.owner_id in hostile_owners)
        ]
        if any(t.hostile for t in near):
            reasons.append(f"hostile_units:{sum(t.hostile for t in near)}")
        if any(not t.hostile for t in near):
            reasons.append(f"rival_units:{sum(not t.hostile for t in near)}")
        if not reasons:
            continue
        inside, beside = count_defenders(city, units, map_width)
        found[city.city_id] = CityThreat(
            city.city_id, city.name, city.x, city.y, tuple(reasons), len(near), inside, beside
        )
    return found


class DefencePolicy:
    """A turn handler that sets the threat state. Run it before the military policy."""

    name = "defence"

    def __init__(self, state: DefenceState, map_width: int | None = STANDARD_MAP_WIDTH) -> None:
        self.state = state
        self._map_width = map_width

    async def run(self, game: Any) -> None:
        turn = (await game.get_game_overview()).turn
        civs = await game.get_diplomacy()
        threats = await _read_threats(game)
        at_war = any(c.has_met and c.is_at_war for c in civs)
        if not at_war and not threats:
            self.state.reset(turn, {})
            return
        cities = await _read_cities(game, self._map_width)
        units = await _read_units(game, self._map_width)
        self.state.reset(
            turn, assess(list(cities), list(units), threats, civs, map_width=self._map_width)
        )


class DefenceReport:
    """A turn handler that logs one line per threatened city. Run it last, before the resolver."""

    name = "defence_report"

    def __init__(self, state: DefenceState) -> None:
        self.state = state

    async def run(self, game: Any) -> None:
        for city in sorted(self.state.cities.values(), key=lambda c: c.city_id):
            actions = "; ".join(self.state.notes.get(city.city_id, [])) or "none"
            log.info(
                "defence turn %d: %s (%d,%d) threat=%s defenders=%d (%d in city, %d beside)%s "
                "actions: %s",
                self.state.turn,
                city.name,
                city.x,
                city.y,
                ",".join(city.reasons),
                city.defenders,
                city.defenders_in,
                city.defenders_beside,
                " SHORT" if city.short else "",
                actions,
            )
