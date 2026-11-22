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
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from civ6_ai.policies.military import is_land_military
from civ6_ai.state import City, Threat, Unit
from civ6_ai.state import read_cities as _read_cities
from civ6_ai.state import read_threats as _read_threats
from civ6_ai.state import read_units as _read_units
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance

log = logging.getLogger(__name__)

THREAT_RADIUS = 3  # Tiles. A land unit moves about 2 a turn, so this is one turn of warning.
WANTED_DEFENDERS = 2  # In the city or on a tile beside it, with no hostile unit about.
MAX_WANTED_DEFENDERS = 5  # However many hostile units are near: no city is stacked past this.
# How a rival that means us harm shows up in civ6-mcp's `diplomatic_state`.
HOSTILE_STATES = ("DENOUNCED", "UNFRIENDLY", "WAR")
# A rival not at war with us that could take a city if it chose to. Run 13: the Mapuche (60 techs
# to our 77, 16 cities to our 5) declared on turn 241 and again on 294, and Coba fell six turns
# into the second war with no wall or garrison made ready. civ6-mcp's own "MILITARY THREAT" flag is
# 1.5x with a hostile state; a rival twice our strength counts whatever its state, short of a pact.
WARY_RATIO_HOSTILE = 1.5
WARY_RATIO_ANY = 2.0
FRIENDLY_STATES = ("ALLIED", "DECLARED_FRIEND")


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
    # The threat map's view of the city (`threat_map.CityReading`), or None with no map (or when
    # the policies are run without it). When set, it decides `short` where hostile units are near.
    reading: Any = None
    # Hit points a turn over healing the map may show before the city counts as short.
    margin: float = 0.0
    # Where the near hostile units (the map's own sources) stood, to drop killed ones. Units of a
    # rival at peace are in `hostile_units` but not here: the map does not hold them, so a city
    # with only those near falls back to the head count.
    hostile_tiles: tuple[tuple[int, int], ...] = ()

    @property
    def defenders(self) -> int:
        return self.defenders_in + self.defenders_beside

    @property
    def wanted(self) -> int:
        """Defenders to hold the city: one more than the units near it, between 2 and 5.

        Run 10: five Mapuche units stood beside a city held by two and it fell a turn after the
        declaration, while production built an Amphitheater and gold bought nothing, because the
        old count asked for two defenders however many enemies came. A rival's unit near the
        city counts like a hostile one: it is the same size of force.
        """
        return max(WANTED_DEFENDERS, min(self.hostile_units + 1, MAX_WANTED_DEFENDERS))

    @property
    def short(self) -> bool:
        """Short of defenders.

        By the threat map when it has a reading and hostile units are near: the attackers that can
        reach the city hit its actual defender, and break its walls or out-damage its healing
        within a few turns (issue 264: the head count called Constantinople short at net -60 and
        a Campus gave way to a Slinger; issue 267: the first reading took our cover as the
        defender's strength and no city was ever short).
        With no reading, or no unit near (a war alone), the head count stays: a city at war
        wants two.
        """
        if self.reading is not None and self.hostile_tiles:
            return self.reading.pressed(self.margin)
        return self.defenders < self.wanted


@dataclass
class DefenceState:
    """The threatened cities this turn, and what was done for each. One instance per game."""

    turn: int = 0
    cities: dict[int, CityThreat] = field(default_factory=dict)
    notes: dict[int, list[str]] = field(default_factory=dict)
    wary: tuple[str, ...] = ()  # Rivals at peace with us that are much stronger. See `wary_of`.
    # The shared threat map, or None (`--no-threat-map`): `short` is then the head count.
    map: Any = field(default=None, repr=False)
    # The camp the military policy means to clear (`military.CampPlan`) or None. Issue 268:
    # production reads it to build the units the force lacks.
    camp_plan: Any = None
    # The strongest barbarian land unit near our cities this turn: (melee, ranged) strength, set
    # by the military policy. Production's army floor picks units by their match against it.
    hostile: tuple[float, float] = (0.0, 0.0)

    def reset(self, turn: int, cities: dict[int, CityThreat], wary: tuple[str, ...] = ()) -> None:
        self.turn = turn
        self.cities = cities
        self.notes = {}
        self.wary = wary
        self.hostile = (0.0, 0.0)
        self.camp_plan = None  # Made again by the military policy, which runs after this.

    @property
    def any(self) -> bool:
        return bool(self.cities)

    @property
    def at_war(self) -> bool:
        """True when we are at war with a major civ (every city then carries a `war:` reason)."""
        return any(r.startswith("war:") for c in self.cities.values() for r in c.reasons)

    @property
    def threatened_ids(self) -> frozenset[int]:
        return frozenset(self.cities)

    @property
    def short_ids(self) -> frozenset[int]:
        return frozenset(i for i, c in self.cities.items() if c.short)

    @property
    def mapped_ids(self) -> frozenset[int]:
        """Cities whose `short` the threat map decided (a reading, and a hostile unit near)."""
        return frozenset(
            i for i, c in self.cities.items() if c.reading is not None and bool(c.hostile_tiles)
        )

    @property
    def reach_aware(self) -> bool:
        """The near-unit counts are by the threat map's reach, not a hex radius."""
        return self.map is not None

    @property
    def reach_counts(self) -> dict[int, int]:
        """Hostile units that can attack each threatened city soon (see `assess`)."""
        return {i: len(c.hostile_tiles) for i, c in self.cities.items()}

    def is_threatened(self, city_id: int) -> bool:
        return city_id in self.cities

    def note(self, city_id: int, text: str) -> None:
        if city_id in self.cities:
            self.notes.setdefault(city_id, []).append(text)

    def set_defenders(self, city_id: int, inside: int, beside: int) -> None:
        """Replace the counts after the military policy has moved units."""
        found = self.cities.get(city_id)
        if found is not None:
            self.cities[city_id] = replace(found, defenders_in=inside, defenders_beside=beside)

    def forget_killed(self, tiles: Iterable[tuple[int, int]]) -> None:
        """Hostile units that died this turn no longer count (issue 264, cause 2).

        The military policy acts after the state is made, so a city's count of near units, and the
        map's reading of it, are from the start of the turn until this runs.
        """
        gone = set(tiles)
        if not gone:
            return
        for city_id, found in list(self.cities.items()):
            alive = tuple(t for t in found.hostile_tiles if t not in gone)
            dead = len(found.hostile_tiles) - len(alive)
            if not dead:
                continue
            left = found.hostile_units - dead
            reasons = tuple(r for r in found.reasons if not r.startswith("hostile_units:"))
            if alive:
                reasons = (*reasons, f"hostile_units:{len(alive)}")
            if not reasons:  # The last threat died and nothing else marks the city.
                del self.cities[city_id]
                continue
            updated = replace(found, hostile_units=left, hostile_tiles=alive, reasons=reasons)
            self.cities[city_id] = replace(updated, reading=self._read(updated))

    def refresh(self) -> None:
        """Read the map again for every city, after our units have moved (it holds cover)."""
        for city_id, found in list(self.cities.items()):
            if found.reading is not None and self.map is not None:
                self.cities[city_id] = replace(found, reading=self._read(found))

    def _read(self, city: CityThreat) -> Any:
        if self.map is None or city.reading is None:
            return None
        return self.map.city_reading(city.x, city.y, self.turn)


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


def wary_of(civs: list[Any]) -> tuple[str, ...]:
    """Met majors, not at war with us, that are much stronger than we are.

    A rival at `WARY_RATIO_HOSTILE` times our military strength while denouncing or unfriendly to
    us, or at `WARY_RATIO_ANY` times it in any state but an alliance or declared friendship. Our
    own strength comes from the same `get_diplomacy` read; with none, nobody is judged. The
    production policy only asks for walls on the strength of this, so a false alarm costs one
    building per city, and no unit is built or bought for it.
    """
    found = []
    for civ in civs:
        ours = getattr(civ, "_our_military", 0)
        if not civ.has_met or civ.is_at_war or ours <= 0 or civ.military_strength <= 0:
            continue
        ratio = civ.military_strength / ours
        state = civ.diplomatic_state
        if (state in HOSTILE_STATES and ratio >= WARY_RATIO_HOSTILE) or (
            state not in FRIENDLY_STATES and ratio >= WARY_RATIO_ANY
        ):
            found.append(civ.civ_name)
    return tuple(sorted(found))


def _reaches(
    threat: Threat, city: City, threat_map: Any, radius: int, map_width: int | None
) -> bool:
    """Whether a unit is near a city: it can attack the city within the map's warning turns
    (terrain costed, its own moves and range: #399), or by hex `radius` with no map."""
    if threat_map is None:
        return hex_distance(city.x, city.y, threat.x, threat.y, map_width) <= radius
    return (city.x, city.y) in threat_map.hostile_reach(threat.unit_type, threat.x, threat.y)


def assess(
    cities: list[City],
    units: list[Unit],
    threats: list[Threat] | tuple[Threat, ...],
    civs: list[Any],
    radius: int = THREAT_RADIUS,
    map_width: int | None = STANDARD_MAP_WIDTH,
    threat_map: Any = None,
    turn: int | None = None,
) -> dict[int, CityThreat]:
    """The threatened cities, by id. `civs` are `get_diplomacy` entries.

    With a `threat_map` each city also carries the map's reading of it, which decides `short`.
    """
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
            if _reaches(t, city, threat_map, radius, map_width)
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
            city.city_id,
            city.name,
            city.x,
            city.y,
            tuple(reasons),
            len(near),
            inside,
            beside,
            reading=threat_map.city_reading(city.x, city.y, turn) if threat_map else None,
            margin=threat_map.settings.short_margin if threat_map else 0.0,
            hostile_tiles=tuple((t.x, t.y) for t in near if t.hostile),
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
        wary = wary_of(civs)
        if not at_war and not threats:
            self.state.reset(turn, {}, wary)
            return
        cities = await _read_cities(game, self._map_width)
        units = await _read_units(game, self._map_width)
        self.state.reset(
            turn,
            assess(
                list(cities),
                list(units),
                threats,
                civs,
                map_width=self._map_width,
                threat_map=self.state.map,
                turn=turn,
            ),
            wary,
        )


class DefenceReport:
    """A turn handler that logs one line per threatened city. Run it last, before the resolver."""

    name = "defence_report"

    def __init__(self, state: DefenceState) -> None:
        self.state = state

    async def run(self, game: Any) -> None:
        for city in sorted(self.state.cities.values(), key=lambda c: c.city_id):
            actions = "; ".join(self.state.notes.get(city.city_id, [])) or "none"
            seen = city.reading
            mapped = (
                f"map: threat={seen.threat:.1f} fog={seen.fog:.1f} cover={seen.cover:.1f} "
                f"defender={seen.defender:.1f} damage={seen.damage:.1f} "
                f"walls={seen.wall_damage:.1f} net={seen.net:.1f}/{city.margin:g} "
                f"turns={seen.turns:.1f}/{seen.horizon:g}"
                if seen is not None
                else "map: off"
            )
            log.info(
                "defence turn %d: %s (%d,%d) threat=%s defenders=%d (%d in city, %d beside)%s "
                "%s actions: %s",
                self.state.turn,
                city.name,
                city.x,
                city.y,
                ",".join(city.reasons),
                city.defenders,
                city.defenders_in,
                city.defenders_beside,
                " SHORT" if city.short else "",
                mapped,
                actions,
            )
