"""Real inputs for the boost judgement, from reads the bot already makes (#344).

`build_boost_state` and `build_plan_signals` are pure functions on the typed views (cities, units,
threats, research, builder jobs, counters the builder and military policies keep). `BoostInputs`
is the async provider `ResearchPolicy(boost_inputs=...)` calls, and only when the judgement is
switched on. Every game read in it is one the other policies make the same turn, so under
`SharedReads` it costs nothing extra: cities, units, the overview, diplomacy, city-states, the
pantheon and the research status. The builder's jobs come from what the builder policy saw last
turn, not from a new `get_builder_tasks` call.

What is not read, and why:

- Improvements already on the map. No existing read lists them (the diary snapshot has a total,
  but it shows every player's state and the fog rules ban it). The count is what the builder
  policy has built, which is all of them until another source of improvements exists.
- Whether a city is coastal, and where the next settler goes: no read gives it cheaply, so the
  coastal-city boost is never predicted.
"""

import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from civ6_ai.policies.game_data import (
    COMBAT_UNITS,
    REPLACES,
    UNIQUE_BUILDINGS,
    UNIQUE_DISTRICTS,
    UNIQUE_IMPROVEMENTS,
    WONDERS,
)
from civ6_ai.state import (
    BuilderJob,
    City,
    Research,
    Threat,
    Unit,
    read_cities,
    read_research,
    read_threats,
    read_units,
)
from civ6_ai.state.fog import BARBARIAN_ID
from civ6_ai.state.reader import STANDARD_MAP_WIDTH
from civ6_ai.strategy.boost_judgement import BoostState, PlanSignals
from civ6_ai.strategy.boosts import (
    ANY,
    BARBARIAN,
    LAND_COMBAT,
    RESOURCE_MINES,
    SEA_RESOURCE,
    name_key,
)

log = logging.getLogger(__name__)

# Faith for a pantheon in the standard game. Only sizes how soon the "found a pantheon" boost
# lands, and the judgement's own horizon bounds the error.
PANTHEON_FAITH = 25.0
# A city that has not grown for this long is treated as not growing: a "turns to grow" that big
# is a stall (no food, or housing full), and counting it would promise a pop that is not coming.
MAX_GROWTH_TURNS = 40
SCOUT = "UNIT_SCOUT"
# Improvements that count as "a sea resource" for the boost, and the builder cannot place today.
SEA_IMPROVEMENTS = frozenset({"FISHING_BOATS", "OFFSHORE_PLATFORM"})
# Jobs older than this many turns are not trusted: the builder policy reads its jobs only on
# turns it has a builder free, so a long gap means nobody knows what is on offer.
MAX_JOB_AGE = 3

_Key = tuple[int, int, str]


# ----- builder jobs and sites -----


def improvement_key(improvement: str) -> str:
    return base_kind(improvement)


def job_key(job: BuilderJob) -> str:
    """What a job counts as: its improvement, or the resource mine for a mine on that resource.

    A job counts under one key only, so the "improve N tiles" total never counts it twice.
    """
    key = improvement_key(job.improvement)
    if key == "MINE" and job.resource:
        return RESOURCE_MINES.get(job.resource.removeprefix("RESOURCE_"), key)
    return key


def job_site(job: BuilderJob) -> str:
    """How a job reads in a nudge: `the stone near Rome`, or `a tile near Rome`."""
    what = f"the {job.resource.lower().replace('_', ' ')}" if job.resource else "a tile"
    return f"{what} near {job.city}" if job.city else f"{what} nearby"


def job_matches(job: BuilderJob, target: str, site: str) -> bool:
    """True when `job` is what an improvement nudge (its `target` and `site`) means."""
    if job.is_repair or job_site(job) != site:
        return False
    key = job_key(job)
    if target == ANY:
        return True
    if target == SEA_RESOURCE:
        return key in SEA_IMPROVEMENTS
    if target == "MINE":
        return key == "MINE" or key in RESOURCE_MINES.values()
    return key == target


def _one_per_tile(jobs: Iterable[BuilderJob], claimed: frozenset[_Key]) -> list[BuilderJob]:
    """The builder does one job per tile: the claimed one, else the first by name."""
    best: dict[tuple[int, int], BuilderJob] = {}
    ranked = sorted(
        (j for j in jobs if not j.is_repair),
        key=lambda j: ((j.x, j.y, j.improvement) not in claimed, j.improvement),
    )
    for job in ranked:
        best.setdefault((job.x, job.y), job)
    return [best[tile] for tile in sorted(best)]


@dataclass(frozen=True)
class ImprovementSignals:
    jobs: dict[str, int] = field(default_factory=dict)
    sites: dict[str, tuple[str, ...]] = field(default_factory=dict)
    resource_jobs: dict[str, int] = field(default_factory=dict)
    resource_sites: dict[str, tuple[str, ...]] = field(default_factory=dict)


def improvement_signals(jobs: Iterable[BuilderJob], claimed: frozenset[_Key]) -> ImprovementSignals:
    """Builder jobs as the judgement reads them.

    `jobs` are the ones a builder has claimed (the plan is doing them), `sites` the open ones no
    builder has (what a nudge could ask for). A sea job counts under `SEA_RESOURCE` only, so the
    "improve N tiles" total does not count it twice.
    """
    done: Counter[str] = Counter()
    done_res: Counter[str] = Counter()
    open_: dict[str, list[str]] = {}
    open_res: dict[str, list[str]] = {}
    for job in _one_per_tile(jobs, claimed):
        key = job_key(job)
        if key in SEA_IMPROVEMENTS:
            key = SEA_RESOURCE
        if (job.x, job.y, job.improvement) in claimed:
            done[key] += 1
            if job.resource:
                done_res[key] += 1
            continue
        site = job_site(job)
        if site not in open_.setdefault(key, []):
            open_[key].append(site)
        if job.resource and site not in open_res.setdefault(key, []):
            open_res[key].append(site)
    return ImprovementSignals(
        dict(done),
        {k: tuple(v) for k, v in open_.items()},
        dict(done_res),
        {k: tuple(v) for k, v in open_res.items()},
    )


# ----- state -----


_PREFIXES = ("UNIT_", "DISTRICT_", "BUILDING_", "IMPROVEMENT_")


def base_kind(item: str) -> str:
    """An item's name without its prefix, a unique replacement mapped to the base it replaces.

    Boosts name the base district, unit, building or improvement, so every count goes through
    here: a Seowon is a Campus, a Pitati an Archer, a Madrasa a University.
    """
    for prefix in _PREFIXES:
        if item.startswith(prefix):
            short = item.removeprefix(prefix)
            if prefix == "UNIT_":
                return REPLACES.get(item, item).removeprefix(prefix)
            table = {
                "DISTRICT_": UNIQUE_DISTRICTS,
                "BUILDING_": UNIQUE_BUILDINGS,
                "IMPROVEMENT_": UNIQUE_IMPROVEMENTS,
            }[prefix]
            return table.get(short, short)
    return item


def _unit_key(unit_type: str) -> str:
    return base_kind(unit_type)


def _land_combat_type(unit_type: str) -> bool:
    """Land combat by the game data's combat-unit table, after a unique unit is mapped to its base.

    Naval, air and civilian units are not in the table, so a Bireme (a Galley) is not land.
    """
    base = REPLACES.get(unit_type, unit_type)
    return base != SCOUT and base in COMBAT_UNITS


def _district_key(district_type: str) -> str:
    return base_kind(district_type)


def _is_land_combat(unit: Unit) -> bool:
    # Same test the military policy uses for the units it orders, without importing it.
    return unit.is_combat and _land_combat_type(unit.unit_type)


def build_boost_state(
    turn: int,
    cities: Sequence[City],
    units: Sequence[Unit],
    built: Mapping[str, int] | None,
    built_on_resource: Mapping[str, int] | None,
    kills: Mapping[str, int],
    civilizations_met: int,
    city_states_met: int,
    has_pantheon: bool,
) -> BoostState:
    """What the game holds now. `built*` are None when no builder policy counts them."""
    districts: Counter[str] = Counter()
    buildings: Counter[str] = Counter()
    for city in cities:
        # A district being built is already on the tile list. It is queued, not done, and a
        # city holds one of each kind, so the kind it is building is left out of the done count.
        building = (
            _district_key(city.building) if (city.building or "").startswith("DISTRICT_") else None
        )
        for district in city.districts:
            kind = _district_key(district.district_type)
            if kind != "CITY_CENTER" and kind != building:
                districts[kind] += 1
        for building in city.buildings:
            buildings[base_kind("BUILDING_" + building.removeprefix("BUILDING_"))] += (
                1  # Read unprefixed.
            )
    unit_counts = Counter(_unit_key(u.unit_type) for u in units)
    improvements = dict(built or {})
    return BoostState(
        turn=turn,
        population=sum(c.pop for c in cities),
        biggest_city_population=max((c.pop for c in cities), default=0),
        cities=len(cities),
        districts=dict(districts),
        improvements=improvements,
        sea_resources_improved=sum(improvements.get(k, 0) for k in SEA_IMPROVEMENTS),
        resource_improvements=None if built_on_resource is None else dict(built_on_resource),
        buildings=dict(buildings),
        units=dict(unit_counts),
        land_combat_units=sum(1 for u in units if _is_land_combat(u)),
        kills=dict(kills),
        civilizations_met=civilizations_met,
        city_states_met=city_states_met,
        has_pantheon=has_pantheon,
        has_coastal_city=False,  # Not read, see the module note.
    )


# ----- plan signals -----


def growth_rates(cities: Sequence[City]) -> tuple[float | None, float | None]:
    """Turns per population gained: all cities together, and the biggest city alone.

    The biggest city's rate is 0.0 when it is stalled and None only when there are no cities.

    A city counts only when it is growing now (food surplus, a believable `turns_to_grow`).
    """
    growing = [c for c in cities if c.food_surplus > 0 and 0 < c.turns_to_grow <= MAX_GROWTH_TURNS]
    total = sum(1.0 / c.turns_to_grow for c in growing)
    overall = 1.0 / total if total > 0 else None
    if not cities:
        return overall, None  # Nothing to measure: the city rate is not given.
    top = max(c.pop for c in cities)
    leaders = [c for c in cities if c.pop == top]
    # A tied leader that is not growing does not hold the others back: take the fastest.
    fastest = [c.turns_to_grow for c in leaders if c in growing]
    # 0.0 is a known stall, distinct from None (not given), so it never borrows the overall rate.
    return overall, float(min(fastest)) if fastest else 0.0


def queued_by_kind(cities: Iterable[City]) -> tuple[Counter, Counter, Counter, bool]:
    """What the cities are building now: districts, units, buildings, and whether a wonder."""
    districts: Counter[str] = Counter()
    units: Counter[str] = Counter()
    buildings: Counter[str] = Counter()
    wonder = False
    for city in cities:
        item = city.building
        if item is None:
            continue
        if item.startswith("DISTRICT_"):
            districts[_district_key(item)] += 1
        elif item.startswith("UNIT_"):
            units[_unit_key(item)] += 1
            if _land_combat_type(item):
                units[LAND_COMBAT] += 1  # The aggregate a "have N land combat units" boost reads.
        elif item in WONDERS:
            wonder = True
        elif item.startswith("BUILDING_"):
            buildings[base_kind(item)] += 1
    return districts, units, buildings, wonder


def kill_offers(units: Iterable[Unit], threats: Iterable[Threat]) -> dict[str, int]:
    """Hostile units our combat units can attack now and are not weaker than.

    Counted once per tile for `BARBARIAN` and once per tile for each unit type that could take
    it, so "kill a unit with a Slinger" sees an offer only when a Slinger is in range. A unit
    below half health is left out. An optimistic bound: the odds are the military policy's call.
    """
    hostile = {(t.x, t.y): t for t in threats if t.hostile}
    by_type: dict[str, set[tuple[int, int]]] = {}
    barbarians: set[tuple[int, int]] = set()
    for unit in units:
        if not _is_land_combat(unit) or unit.hp * 2 < unit.max_hp:
            continue
        strength = max(unit.combat_strength, unit.ranged_strength)
        for target in unit.targets:
            tile = (target.x, target.y)
            enemy = hostile.get(tile)
            if enemy is None or strength < max(enemy.combat_strength, enemy.ranged_strength):
                continue
            by_type.setdefault(_unit_key(unit.unit_type), set()).add(tile)
            if enemy.owner_id == BARBARIAN_ID:
                barbarians.add(tile)
    offers = {kind: len(tiles) for kind, tiles in by_type.items()}
    if barbarians:
        offers[BARBARIAN] = len(barbarians)
    return offers


def nearest_unmet(
    threats: Iterable[Threat], civs: Iterable[Any], met_city_states: Iterable[int]
) -> tuple[int | None, int | None]:
    """Distance to the nearest visible unit of an unmet major, and of an unmet city-state.

    Evidence of an imminent meeting, from the threat scan and the diplomacy list the bot reads
    each turn. The scan may not report an unmet owner's units at all, in which case this is
    (None, None) and no meeting is waited for.
    """
    unmet_majors = {c.player_id for c in civs if not c.has_met}
    met_cs = set(met_city_states)
    civ: int | None = None
    city_state: int | None = None
    for t in threats:
        if t.owner_id == BARBARIAN_ID:
            continue
        if t.is_city_state:
            if t.owner_id not in met_cs and (city_state is None or t.distance < city_state):
                city_state = t.distance
        elif t.owner_id in unmet_majors and (civ is None or t.distance < civ):
            civ = t.distance
    return civ, city_state


def pantheon_turns(has_pantheon: bool, balance: float, per_turn: float) -> int | None:
    """Turns until a pantheon can be bought, or None when none is being earned."""
    if has_pantheon or per_turn <= 0:
        return None
    return max(1, math.ceil((PANTHEON_FAITH - balance) / per_turn))


def research_turns(research: Research) -> dict[str, int]:
    """Turns left on the tech and civic in progress, by the name key boost targets use."""
    out: dict[str, int] = {}
    for name, turns in (
        (research.current_tech, research.current_tech_turns),
        (research.current_civic, research.current_civic_turns),
    ):
        if name and turns is not None and turns > 0:
            out[name_key(name)] = turns
    return out


def build_plan_signals(
    cities: Sequence[City],
    units: Sequence[Unit],
    threats: Sequence[Threat],
    research: Research,
    jobs: Iterable[BuilderJob],
    claimed: frozenset[_Key],
    has_pantheon: bool,
    pantheon_balance: float,
    unmet: tuple[int | None, int | None] = (None, None),
) -> PlanSignals:
    overall, biggest = growth_rates(cities)
    districts, queued_units, buildings, wonder = queued_by_kind(cities)
    sites = improvement_signals(jobs, claimed)
    return PlanSignals(
        growth_turns_per_pop=overall,
        city_growth_turns_per_pop=biggest,
        districts_queued=dict(districts),
        improvement_jobs=sites.jobs,
        improvement_sites=sites.sites,
        resource_improvement_jobs=sites.resource_jobs,
        resource_improvement_sites=sites.resource_sites,
        units_queued=dict(queued_units),
        buildings_queued=dict(buildings),
        kill_offers=kill_offers(units, threats),
        exploring=any(u.unit_type == SCOUT for u in units),
        nearest_unmet_civ=unmet[0],
        nearest_unmet_city_state=unmet[1],
        pantheon_turns=pantheon_turns(has_pantheon, pantheon_balance, sum(c.faith for c in cities)),
        wonder_queued=wonder,
        research_turns=research_turns(research),
    )


# ----- the provider -----


@dataclass
class BoostInputs:
    """The async `boost_inputs` provider for `ResearchPolicy`.

    `game` returns the reads-sharing game the policies get (a callable, since the bot builds that
    game after its handlers). `builder` and `military` are the policies whose counters it reads,
    and either may be None. `map_width` is the map's.
    """

    game: Callable[[], Any]
    builder: Any = None
    military: Any = None
    map_width: int | None = STANDARD_MAP_WIDTH

    async def __call__(self) -> tuple[BoostState, PlanSignals]:
        game = self.game()
        overview = await game.get_game_overview()
        cities = await read_cities(game, self.map_width)
        units = await read_units(game, self.map_width)
        threats = await read_threats(game)
        research = await read_research(game)
        civs = await game.get_diplomacy()
        envoys = await game.get_city_states()
        pantheon = await game.get_pantheon_status()
        met = sum(1 for c in civs if c.has_met and c.player_id != overview.player_id)
        turn = overview.turn
        builder = self.builder
        jobs: tuple[BuilderJob, ...] = ()
        claimed: frozenset[_Key] = frozenset()
        fresh = builder is not None and builder.jobs_turn is not None
        if fresh and 0 <= turn - builder.jobs_turn <= MAX_JOB_AGE:
            jobs, claimed = tuple(builder.open_jobs), frozenset(builder.claimed)
        state = build_boost_state(
            turn, cities, units,
            None if builder is None else builder.built,
            None if builder is None else builder.built_on_resource,
            {} if self.military is None else self.military.kills,
            met, len(envoys.city_states), pantheon.has_pantheon,
        )  # fmt: skip
        signals = build_plan_signals(
            cities, units, threats, research, jobs, claimed, pantheon.has_pantheon,
            pantheon.faith_balance,
            nearest_unmet(threats, civs, (c.player_id for c in envoys.city_states)),
        )  # fmt: skip
        return state, signals
