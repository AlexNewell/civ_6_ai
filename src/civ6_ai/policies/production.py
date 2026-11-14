"""Choose what each idle city builds, with no model calls.

The choice is a pure ranking: `rank_production` drops dominated options, scores what is left and
returns it best first. The same city, options, context and weights always give the same order.
`CityProductionPolicy` is a turn handler that applies it to idle cities, and `resolve_production`
is the same logic as a blocker resolver, so it can take over `BlockerType.PRODUCTION` while the
default `build_cheapest` stays behind it as a fallback.

`ProductionWeights` is the plain input the Phase 2 strategist will set. The defaults play a
balanced game.
"""

import logging
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields, replace
from typing import Any, Protocol

from civ6_ai.blockers import Blocker, BlockerGame, BlockerType
from civ6_ai.construction import ProductionRefused, ProductionSet
from civ6_ai.monitors import Event, EventBus
from civ6_ai.placement import (
    ELSEWHERE,
    MAX_UNCHANGED_REFUSALS,
    OTHER,
    SAFETY_RECHECK_TURNS,
    UNKNOWN,
    UNKNOWN_WAIT_TURNS,
    OwnedTile,
    PlacementReads,
    Planner,
    classify,
)
from civ6_ai.policies.districts import BASE_VALUE, PLANNED_VALUE, is_planned, wants_district
from civ6_ai.policies.expansion import ExpansionState, expected_cities
from civ6_ai.policies.game_data import COMBAT_UNITS, REPLACES, UPGRADES, WONDERS
from civ6_ai.state import City, Empire, ProductionChoice, Unit
from civ6_ai.state import read_cities as _read_cities
from civ6_ai.state import read_empire as _read_empire
from civ6_ai.state import read_production as _read_production
from civ6_ai.state import read_units as _read_units

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6  # Per city per turn. Every refusal costs a call, and a unit always works.
# civ6-mcp turns "ERR:" replies into "Error: " before returning them, so a live wonder reply reads
# "Error: MISSING_COORDS|BUILDING_X is a wonder and requires target_x/target_y". Run 4 matched only
# the "ERR:" form, so no wonder was ever placed.
_NO_COORDS_WONDER = ("ERR:MISSING_COORDS", "Error: MISSING_COORDS")
# Replies that mean the game will not build the item here. Anything else that fails (a read
# failure, a timeout) may pass next turn, so it is never remembered.
_REFUSAL_CODES = ("NO_PLACEMENT", "CANNOT_START", "SILENT_FAILURE", "CANNOT_PRODUCE")
STARVING_FOCUS_RECOVERED = 2.0  # Food per turn at which a food-focused city goes back to default.
_TURN_SCALE = 20.0  # A build this many turns away scores half as much as an instant one.
_URGENT_SCALE = 10.0
_FINISHING_TURNS = 2  # A build this close to done is let finish.
_EARLY_TURNS = 60  # Turns in which a city with no garrison should get one first.


@dataclass(frozen=True)
class ProductionWeights:
    """How much each kind of build is worth to the empire. Zero turns a kind off.

    Yields scale the buildings and districts that produce them. The rest scale a whole kind of
    unit or project. Threat handling is not weighted, because losing a city beats any plan.
    """

    food: float = 1.0
    production: float = 1.0
    gold: float = 1.0
    science: float = 1.0
    culture: float = 1.0
    faith: float = 0.5
    military: float = 1.0  # Land units beyond the garrison a city needs.
    expansion: float = 1.0  # Settlers.
    builders: float = 1.0  # Builders, which improve tiles.
    trade: float = 1.0  # Traders, while the empire has route capacity no trader fills.
    wonders: float = 0.5
    target_cities: int = 6  # Stop building settlers at this many cities.
    victory: str = "any"  # The policy document's victory target. Decides the special districts.
    army_per_city: float = 1.5  # Combat units wanted per city before military stops scoring.

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, str):
                continue
            if value < 0 or not math.isfinite(value):
                raise ValueError(f"Weight {item.name} must be a finite number of at least 0")


@dataclass(frozen=True)
class ProductionContext:
    """What the empire has, which no single city shows."""

    turn: int
    num_cities: int
    builders: int = 0
    settlers: int = 0
    recon: int = 0
    army: int = 0
    traders: int = 0  # Traders that exist, on a route or not, plus those being built.
    trade_capacity: int = 0  # Routes the empire can run at once.
    districts: frozenset[str] = frozenset()  # Short names of districts any city has built.
    threatened: frozenset[int] = frozenset()  # Ids of cities the threat state marks (1.35).
    short: frozenset[int] = frozenset()  # Of those, the ones with fewer than two defenders.
    sites_ok: bool = True  # False when settling found no safe site to send a settler to.
    idle_builders: int = 0  # Builders that found no job on offer the last time they ran (1.45).
    spare_jobs: int | None = None  # Jobs no builder holds; None when not known this turn.

    def after_choosing(
        self, item: str, unknown_combat: bool = False, orders: bool = True
    ) -> "ProductionContext":
        """Count a pick, so two cities don't both build the one builder the empire needs.

        A builder ordered (`orders`) also uses up one unclaimed job. Builders that already exist
        don't: the job count was taken after they chose.
        """
        kinds = _counted_as(item) or (("army",) if unknown_combat else ())
        counted = replace(self, **{kind: getattr(self, kind) + 1 for kind in kinds})
        if orders and "builders" in kinds and counted.spare_jobs is not None:
            counted = replace(counted, spare_jobs=max(0, counted.spare_jobs - 1))
        if item.startswith("DISTRICT_"):  # A district being built counts, so two cities don't.
            counted = replace(counted, districts=counted.districts | {_short(item)})
        return counted


def canonical(item: str) -> str:
    """The standard unit a unique unit replaces, so it is treated as that unit."""
    return REPLACES.get(item, item)


def _counted_as(item: str) -> tuple[str, ...]:
    kind = canonical(item)
    if kind == "UNIT_BUILDER":
        return ("builders",)
    if kind == "UNIT_SETTLER":
        return ("settlers",)
    if kind == "UNIT_SCOUT":
        return ("recon",)
    if kind == "UNIT_TRADER":
        return ("traders",)
    stats = COMBAT_UNITS.get(item)
    if stats is not None and stats[4] != "RECON":
        return ("army",)
    return ()


def build_context(
    empire: Empire,
    units: Iterable[Unit],
    in_progress: Iterable[str] = (),
    cities: Iterable[City] = (),
    defence: Any = None,
    idle_builders: int = 0,
    spare_jobs: int | None = None,
) -> ProductionContext:
    """Count what exists plus what cities are already building, so picks don't double up.

    `defence` is the threat state (`defence.DefenceState`), or None.
    """
    ctx = ProductionContext(
        turn=empire.turn,
        num_cities=empire.num_cities,
        trade_capacity=empire.trade_capacity,
        threatened=defence.threatened_ids if defence is not None else frozenset(),
        short=defence.short_ids if defence is not None else frozenset(),
        districts=frozenset(_short(d.district_type) for c in cities for d in c.districts),
        idle_builders=idle_builders,
        spare_jobs=spare_jobs,
    )
    for unit in units:
        ctx = ctx.after_choosing(
            unit.unit_type,
            unknown_combat=unit.is_combat and unit.unit_type not in COMBAT_UNITS,
            orders=False,
        )
    for item in in_progress:
        ctx = ctx.after_choosing(item)
    return ctx


_DEFENSIVE_UNIT_CLASSES = frozenset({"SIEGE", "RECON"})
# Units the policy has no plan for. Anything else the game offers and the table lacks is kept
# and scored low, so a new or modded land unit is a last resort, never invisible.
_UNPLANNED_WORDS = (
    "TRADER",
    "MISSIONARY",
    "APOSTLE",
    "INQUISITOR",
    "SPY",
    "GREAT_",
    "GURU",
    "ARCHAEOLOGIST",
    "NATURALIST",
    "ROCK_BAND",
    "SUPPORT",
    "MEDIC",
    "BATTERING",
    "OBSERVATION",
    "SUPPLY",
    "SIEGE_TOWER",
)
_UNKNOWN_UNIT_SCORE = 0.3
_CIVILIAN_UNITS = frozenset({"UNIT_BUILDER", "UNIT_SETTLER", "UNIT_SCOUT", "UNIT_TRADER"})


def _upgrades_of(item: str) -> set[str]:
    """Every unit this one eventually upgrades to."""
    found: set[str] = set()
    step = UPGRADES.get(item)
    while step is not None and step not in found:
        found.add(step)
        step = UPGRADES.get(step)
    return found


def defence_strength(item: str) -> float:
    """How well a unit holds a city, from the game's own numbers. Siege and scouts hold nothing."""
    stats = COMBAT_UNITS.get(item)
    if stats is None or stats[4] in _DEFENSIVE_UNIT_CLASSES:
        return 0.0
    return max(float(stats[0]), 0.9 * stats[1])


# Value of a building to each yield it grows: (food, production, gold, science, culture, faith).
_BUILDING_YIELDS: dict[str, tuple[float, float, float, float, float, float]] = {
    "MONUMENT": (0, 0, 0, 0, 2.0, 0),
    "GRANARY": (2.0, 0, 0, 0, 0, 0),
    "LIBRARY": (0, 0, 0, 3.0, 0, 0),
    "WORKSHOP": (0, 3.0, 0, 0, 0, 0),
    "MARKET": (0, 0, 3.0, 0, 0, 0),
    "SHRINE": (0, 0, 0, 0, 0, 2.0),
    "TEMPLE": (0, 0, 0, 0, 0, 3.0),
    "LIGHTHOUSE": (2.0, 0, 0, 0, 0, 0),
    "HARBOR": (2.0, 0, 2.0, 0, 0, 0),
    "WATER_MILL": (2.0, 0, 0, 0, 0, 0),
    "AMPHITHEATER": (0, 0, 0, 0, 3.0, 0),
    "UNIVERSITY": (0, 0, 0, 4.0, 0, 0),
    "BANK": (0, 0, 4.0, 0, 0, 0),
    "FACTORY": (0, 4.0, 0, 0, 0, 0),
    "MADRASA": (0, 0, 0, 4.0, 0, 0),
    "MUSEUM_ART": (0, 0, 0, 0, 4.0, 0),
    "MUSEUM_ARTIFACT": (0, 0, 0, 0, 4.0, 0),
    "SEWER": (2.0, 0, 0, 0, 0, 0),
    "STOCK_EXCHANGE": (0, 0, 5.0, 0, 0, 0),
    "RESEARCH_LAB": (0, 0, 0, 5.0, 0, 0),
    "CATHEDRAL": (0, 0, 0, 0, 0, 4.0),
    "ARENA": (0, 0, 0, 0, 3.0, 0),
    "FERRIS_WHEEL": (0, 0, 0, 0, 3.0, 0),
    "POWER_PLANT": (0, 4.0, 0, 0, 0, 0),
}
_DEFENSIVE_BUILDINGS = frozenset({"WALLS", "CASTLE", "STAR_FORT"})
_TRAINING_BUILDINGS = frozenset({"BARRACKS", "STABLE", "ARMORY", "MILITARY_ACADEMY"})
_WONDER_VALUE = 8.0
_UNKNOWN_BUILDING = 1.0  # Anything not listed still helps a little, so it ranks last, not never.

_DISTRICT_YIELDS: dict[str, tuple[float, float, float, float, float, float]] = {
    "CAMPUS": (0, 0, 0, 5.0, 0, 0),
    "COMMERCIAL_HUB": (0, 0, 5.0, 0, 0, 0),
    "HOLY_SITE": (0, 0, 0, 0, 0, 4.0),
    "THEATER": (0, 0, 0, 0, 5.0, 0),
    "INDUSTRIAL_ZONE": (0, 5.0, 0, 0, 0, 0),
    "HARBOR": (2.0, 0, 3.0, 0, 0, 0),
    "AQUEDUCT": (4.0, 0, 0, 0, 0, 0),
    "NEIGHBORHOOD": (3.0, 0, 0, 0, 0, 0),
    "ENTERTAINMENT_COMPLEX": (2.0, 0, 0, 0, 2.0, 0),
    "WATER_PARK": (2.0, 0, 0, 0, 2.0, 0),
    "ENCAMPMENT": (0, 0, 0, 0, 0, 0),
    "PRESERVE": (2.0, 0, 0, 0, 0, 0),
    "GOVERNMENT_PLAZA": (0, 0, 3.0, 3.0, 3.0, 0),
    "DIPLOMATIC_QUARTER": (0, 0, 3.0, 3.0, 3.0, 0),
}
_WEIGHT_ORDER = ("food", "production", "gold", "science", "culture", "faith")

_SETTLER_MIN_POP = 2
_BEHIND_SETTLER_SCORE = 19.0  # Above a builder or trader, below defending a threatened city.


def _short(item: str) -> str:
    for prefix in ("BUILDING_", "DISTRICT_", "UNIT_"):
        if item.startswith(prefix):
            return item.removeprefix(prefix)
    return item


def _yield_value(profile: tuple[float, ...], weights: ProductionWeights) -> float:
    return sum(
        amount * getattr(weights, name) for amount, name in zip(profile, _WEIGHT_ORDER, strict=True)
    )


def drop_dominated(
    options: Iterable[ProductionChoice],
    city: City,
    context: ProductionContext | None = None,
    weights: ProductionWeights | None = None,
) -> tuple[list[ProductionChoice], list[ProductionChoice]]:
    """Split options into those worth ranking and those that are never worth building.

    Dominated means another offered option does the same job better, or the city already has it.
    Special districts are kept only when `wants_district` says so. Repairs are kept apart because
    they have their own coordinates. Returns (choices, repairs).
    """
    context = context or ProductionContext(turn=0, num_cities=1)
    victory = (weights or ProductionWeights()).victory
    offered = {o.item for o in options if not o.is_repair}
    built = {_short(b) for b in city.buildings}
    districts = {_short(d.district_type) for d in city.districts}
    keep: list[ProductionChoice] = []
    repairs: list[ProductionChoice] = []
    seen: set[tuple[str, str]] = set()
    for option in options:
        if option.is_repair:
            if option.category == "DISTRICT" and None not in (option.repair_x, option.repair_y):
                repairs.append(option)
            continue
        key = (option.category, option.item)
        if key in seen or option.category == "PROJECT":
            continue
        seen.add(key)
        name = _short(option.item)
        if option.category == "BUILDING" and name in built:
            continue
        if option.category == "DISTRICT" and (
            name in districts
            or not wants_district(
                name,
                victory=victory,
                turn=context.turn,
                pop=city.pop,
                housing=city.housing,
                empire_districts=context.districts,
            )
        ):
            continue
        if option.category == "UNIT":
            if any(better in offered for better in _upgrades_of(option.item)):
                continue  # The game offers its upgrade, which does the same job better.
            if is_obsolete_melee(option.item, offered):
                continue  # A clearly stronger melee unit is on offer.
            if option.item not in COMBAT_UNITS and (
                canonical(option.item) not in _CIVILIAN_UNITS
                and any(word in option.item for word in _UNPLANNED_WORDS)
            ):
                continue
        keep.append(option)
    return keep, repairs


_MELEE_CLASSES = frozenset({"MELEE", "ANTI_CAVALRY"})
_OBSOLETE_RATIO = 1.2  # A melee unit is obsolete when another offered one is this much stronger.


def is_obsolete_melee(item: str, offered: Iterable[str]) -> bool:
    """True for a melee unit (a Warrior, say) when the game offers a clearly stronger one.

    The upgrade rule in `drop_dominated` only catches the unit's own line, and a city without Iron
    is not offered a Swordsman, so a Warrior stayed on offer beside a Spearman or Man-at-Arms.
    """
    stats = COMBAT_UNITS.get(item)
    if stats is None or stats[4] not in _MELEE_CLASSES:
        return False
    for other in offered:
        better = COMBAT_UNITS.get(other)
        if (
            other != item
            and better is not None
            and better[4] in _MELEE_CLASSES
            and better[0] >= _OBSOLETE_RATIO * stats[0]
        ):
            return True
    return False


def is_defence(option: ProductionChoice) -> bool:
    """A unit that holds a city, or the walls that help it."""
    if option.category == "UNIT":
        return defence_strength(option.item) > 0
    return option.category == "BUILDING" and _short(option.item) in _DEFENSIVE_BUILDINGS


_MAX_DEFENDERS_PER_CITY = 3


def under_threat(city: City, ctx: ProductionContext) -> bool:
    """Hostile units in range, or the threat state marks the city (a war, a rival's units)."""
    return city.is_threatened or city.city_id in ctx.threatened


def is_pressed(city: City, ctx: ProductionContext) -> bool:
    """Threatened and short of defenders, so defence outranks expansion.

    A city the threat state marks with fewer than two defenders is pressed too.

    A threat that sits next to a garrisoned city for turns (barbarians camping live on turn 9 to
    25) must not stop settlers and builders forever. Once there are enough defenders, the
    threat only keeps the usual military scoring.
    """
    if city.city_id in ctx.short:
        return True
    if not city.is_threatened:
        return False
    wanted = min(len(city.threats) + 1, _MAX_DEFENDERS_PER_CITY) * max(1, ctx.num_cities)
    return ctx.army < wanted


def is_starving(city: City) -> bool:
    """The city is losing food each turn and has people to lose."""
    return city.food_surplus < 0 and city.pop > 1


# What a starving city builds to feed itself. Housing buildings (Aqueduct, Sewer) do not help.
_FOOD_BUILDINGS = frozenset({"GRANARY", "LIGHTHOUSE", "HARBOR", "WATER_MILL"})
_STARVING_FOOD_BOOST = 4.0  # Extra weight on the food yield of a food building in a starving city.


def _unit_score(
    option: ProductionChoice, city: City, ctx: ProductionContext, w: ProductionWeights
) -> float:
    item = option.item
    kind = canonical(item)
    threatened = is_pressed(city, ctx)
    if kind == "UNIT_SETTLER":
        # Behind the expansion schedule (1.41), with safe sites and a defender for every city
        # (the one in this city included), settlers outrank the rest and a second may be queued.
        behind = ctx.num_cities < expected_cities(ctx.turn, w.target_cities)
        boosted = (
            behind
            and ctx.sites_ok
            and city.garrison_unit is not None
            and ctx.army >= ctx.num_cities
        )
        in_flight_cap = 2 if boosted and ctx.num_cities + 2 <= w.target_cities else 1
        if (
            threatened
            or is_starving(city)
            or city.pop < _SETTLER_MIN_POP
            or ctx.num_cities + ctx.settlers >= w.target_cities
            or ctx.settlers >= in_flight_cap
            # Run 11: a settler with no site was built five times and disbanded four. Only the
            # boost used to read this; now no fresh survey site means no settler at all.
            or not ctx.sites_ok
        ):
            return 0.0
        return (_BEHIND_SETTLER_SCORE if boosted else 14.0) * w.expansion
    if kind == "UNIT_BUILDER":
        wanted = max(1, ctx.num_cities) - ctx.builders
        if ctx.builders > 0:
            # Run 8: builders sat idle on 74% of builder-turns, 98% late. Train another only while
            # a job is going unclaimed, and never while one already has nothing to do.
            if ctx.idle_builders > 0:
                return 0.0
            if ctx.spare_jobs is not None:
                wanted = min(wanted, ctx.spare_jobs)
        if wanted <= 0 or threatened:
            return 0.0
        return (6.0 + 2.0 * min(wanted, 3) + len(city.unimproved_resources)) * w.builders
    if kind == "UNIT_TRADER":
        # Every route slot needs a trader. Traders already on a route count, since they fill a slot.
        wanted = ctx.trade_capacity - ctx.traders
        if wanted <= 0 or threatened:
            return 0.0
        return (7.0 + min(wanted, 3)) * w.trade
    if kind == "UNIT_SCOUT":
        # With nothing exploring, the settling policy sees no sites (live: a settler sat idle from
        # turn 30 on a 30-tile map), so the first scout outranks a settler.
        return 15.0 if ctx.recon == 0 and ctx.turn <= _EARLY_TURNS else 0.0
    # Military. Strength comes from the game's own numbers, so the best unit offered wins.
    stats = COMBAT_UNITS.get(item)
    if stats is None:
        return _UNKNOWN_UNIT_SCORE  # A land unit the table lacks is a last resort, not hidden.
    strength = defence_strength(item)
    if strength == 0:
        return 0.0  # Siege and scouts hold no city.
    if threatened:
        # In a marked city a ranged unit, which shoots from behind the front one, gets a lift.
        ranged = 3.0 if stats[4] == "RANGED" and city.city_id in ctx.threatened else 0.0
        return 14.0 + strength / 2.0 + 2.0 * len(city.threats) + ranged
    if city.garrison_unit is None:
        return (16.0 if ctx.turn <= _EARLY_TURNS else 12.0) + strength / 10.0
    wanted = w.army_per_city * ctx.num_cities - ctx.army
    if wanted <= 0:
        return 0.0
    return min(wanted, 4.0) * 1.5 * w.military + strength / 20.0


def _building_score(
    option: ProductionChoice, city: City, ctx: ProductionContext, w: ProductionWeights
) -> float:
    name = _short(option.item)
    if option.item in WONDERS:
        # Needs a tile, which `_start` finds when the game asks for one.
        return 0.0 if under_threat(city, ctx) else _WONDER_VALUE * w.wonders
    if name in _DEFENSIVE_BUILDINGS:
        if city.city_id in ctx.threatened and not city.has_walls:
            return 26.0 + len(city.threats)  # Walls come right after the first defender.
        if is_pressed(city, ctx) and not city.has_walls:
            return 17.0 + len(city.threats)
        return 0.0  # Unthreatened, walls are an expense with no return.
    if name in _TRAINING_BUILDINGS:
        return 0.5 * w.military
    profile = _BUILDING_YIELDS.get(name)
    if profile is None:
        return _UNKNOWN_BUILDING
    value = _yield_value(profile, w)
    if name == "GRANARY" and city.food_surplus > 3:
        value *= 0.5
    if name in _FOOD_BUILDINGS and is_starving(city):
        value += _STARVING_FOOD_BOOST * profile[0] * w.food
    return value


def _district_score(option: ProductionChoice, city: City, w: ProductionWeights) -> float:
    name = _short(option.item)
    if name in BASE_VALUE:  # It passed `wants_district`, so it is worth building.
        return PLANNED_VALUE if is_planned(name, w.victory) else BASE_VALUE[name]
    profile = _DISTRICT_YIELDS.get(name)
    if profile is None:
        return _UNKNOWN_BUILDING * 2.0
    value = _yield_value(profile, w)
    if name in _FOOD_BUILDINGS and is_starving(city):
        value += _STARVING_FOOD_BOOST * profile[0] * w.food
    if _short(option.item) == "AQUEDUCT":
        # Worth building when growth is capped by housing, not before.
        value = value * 2.0 if city.housing - city.pop <= 1 else value * 0.3
    return value


def score(
    option: ProductionChoice, city: City, ctx: ProductionContext, weights: ProductionWeights
) -> float:
    """What the option is worth to this city, discounted by how long it takes."""
    if option.is_repair:
        value = 8.0
    elif option.category == "UNIT":
        value = _unit_score(option, city, ctx, weights)
    elif option.category == "DISTRICT":
        value = _district_score(option, city, weights)
    else:
        value = _building_score(option, city, ctx, weights)
    if value <= 0:
        return 0.0
    if under_threat(city, ctx) and is_defence(option):
        # A defender that arrives in time matters most, so speed counts for more than usual.
        return value * _URGENT_SCALE / (_URGENT_SCALE + max(option.turns, 1))
    return value * _TURN_SCALE / (_TURN_SCALE + max(option.turns, 1))


def rank_production(
    city: City,
    options: Iterable[ProductionChoice],
    context: ProductionContext,
    weights: ProductionWeights | None = None,
) -> list[ProductionChoice]:
    """Return the options worth building, best first. Zero-scored options come last.

    The rest of the order breaks ties by category and name, never by input order, so shuffled
    input gives the same list.
    """
    weights = weights or ProductionWeights()
    keep, repairs = drop_dominated(list(options), city, context, weights)
    ranked = sorted(
        [*repairs, *keep],
        key=lambda o: (-score(o, city, context, weights), o.category, o.item, o.repair_x or 0),
    )
    return ranked


class ProductionGame(BlockerGame, Protocol):
    """What the policy needs from civ6-mcp's `GameState`."""

    async def get_game_overview(self) -> Any: ...
    async def get_district_advisor(self, city_id: int, district_type: str) -> Any: ...
    async def get_wonder_advisor(self, city_id: int, wonder_name: str) -> Any: ...
    async def set_city_focus(self, city_id: int, focus: str) -> str: ...


def is_refusal(result: str) -> bool:
    """The game said it won't build this item, not a call that failed for another reason."""
    return _failed(result) and any(code in result for code in _REFUSAL_CODES)


@dataclass
class _Refusal:
    """One remembered refusal: why, under which placement state, and what ends it."""

    reason: str
    signature: str | None  # The city's placement signature when it was refused.
    count: int  # Refusals in a row under that same signature.
    until: int | None  # Turn at which it is asked again regardless. None: only a change ends it.
    needs: str | None = None  # A tech or civic whose arrival ends it.
    permanent: bool = False  # Dropped for the game.


class RefusalMemory:
    """What the game refused for a city, and what would change the answer (issue 187).

    There are no long timers. A refusal is kept with the city's placement signature
    (`placement.build_signatures_query`) and is asked again as soon as that changes, or as soon as
    the tech or civic it waits for lands. By reason (`placement.classify`):

    - placement or district: ask again when the signature changes. A short
      `SAFETY_RECHECK_TURNS` re-ask covers what the signature cannot show; it only restores
      eligibility.
    - unlock: ask again when the tech or civic is held.
    - elsewhere (the wonder is built or under way elsewhere): dropped for the game.
    - unknown or other (a flaky set): a wait of `UNKNOWN_WAIT_TURNS`.
    - for every reason, after three refusals in a row under an unchanged signature only a change
      ends it.
    - no signature could be read: the short wait, whatever the reason.

    Kept in memory only: a restart forgets it, which costs one retry per item.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[int, str], _Refusal] = {}

    def watched(self) -> tuple[str, ...]:
        """The techs and civics some refusal is waiting for."""
        return tuple(sorted({e.needs for e in self._entries.values() if e.needs}))

    def has(self, city: City, item: str) -> bool:
        return (city.city_id, item) in self._entries

    async def blocked(
        self, city: City, item: str, turn: int, reads: PlacementReads | None = None
    ) -> bool:
        entry = self._entries.get((city.city_id, item))
        if entry is None:
            return False
        if entry.permanent:
            return True
        if reads is not None:
            signature = await reads.signature(city.city_id)
            if signature is not None and entry.signature not in (None, signature):
                return False
            if entry.needs and await reads.unlocked(entry.needs):
                return False
        return entry.until is None or turn < entry.until

    def refused(
        self,
        city: City,
        item: str,
        turn: int,
        reply: str = "",
        reason: str | None = None,
        signature: str | None = None,
        needs: str | None = None,
    ) -> int:
        """Record a refusal. Returns the turns until it is asked again, or -1 for only a change."""
        reason = reason or classify(reply, None)[0]
        key = (city.city_id, item)
        before = self._entries.get(key)
        same = before is not None and before.signature == signature and signature is not None
        count = before.count + 1 if same and before is not None else 1
        if reason == ELSEWHERE:
            entry = _Refusal(reason, signature, count, None, permanent=True)
        elif signature is None:
            entry = _Refusal(reason, None, count, turn + UNKNOWN_WAIT_TURNS, needs)
        elif count >= MAX_UNCHANGED_REFUSALS:
            entry = _Refusal(reason, signature, count, None, needs)  # Only a change ends it.
        elif reason in (UNKNOWN, OTHER):
            entry = _Refusal(reason, signature, count, turn + UNKNOWN_WAIT_TURNS)
        else:
            entry = _Refusal(reason, signature, count, turn + SAFETY_RECHECK_TURNS, needs)
        self._entries[key] = entry
        return -1 if entry.until is None else entry.until - turn

    def accepted(self, city: City, item: str) -> None:
        self._entries.pop((city.city_id, item), None)


def _failed(result: str) -> bool:
    return result.startswith(("Error", "ERR:"))


def _advisor_failed(item: str, reply: Any) -> str:
    """The advisor answered with something other than tiles (a read error). Not a refusal."""
    return f"ERR:ADVISOR_READ|{item}: {reply}"


@dataclass
class StartTrace:
    """What `_start` did besides the reply, for the construction events."""

    placements: int | None = None  # How many tiles the advisor offered.
    tile: tuple[int, int] | None = None  # The tile it chose.


async def _start(
    game: ProductionGame,
    city: City,
    option: ProductionChoice,
    trace: StartTrace | None = None,
    plan: "PlanContext | None" = None,
) -> str:
    """Set production, finding a tile first where the item needs one.

    With `plan`, an item with no valid tile (or, for a district, a clearly better tile the city
    could buy) goes through the planner, which may buy a tile; the advisor is then asked again.
    """
    trace = trace if trace is not None else StartTrace()
    bought: tuple[int, int] | None = None
    if option.is_repair:
        trace.tile = (option.repair_x, option.repair_y)
        return await game.set_city_production(
            city.city_id, option.category, option.item, option.repair_x, option.repair_y
        )
    if option.category == "DISTRICT":
        placements = await game.get_district_advisor(city.city_id, option.item)
        if not isinstance(placements, list):
            return _advisor_failed(option.item, placements)
        if plan is not None:
            offered = [OwnedTile(p.x, p.y, p.total_adjacency) for p in placements]
            if await plan.chain(game, city, option, offered):
                placements = await game.get_district_advisor(city.city_id, option.item)
                if not isinstance(placements, list):
                    return _advisor_failed(option.item, placements)
                await plan.verify(city, option, [OwnedTile(p.x, p.y, 0) for p in placements])
                bought = plan.bought_tile
        trace.placements = len(placements)
        if not placements:
            return f"ERR:NO_PLACEMENT|{option.item}: no valid tile"
        best = min(placements, key=lambda p: (-p.total_adjacency, p.x, p.y))
        if bought is not None:  # The tile was bought for its net value, so it is the one used.
            best = next((p for p in placements if (p.x, p.y) == bought), best)
        trace.tile = (best.x, best.y)
        return await game.set_city_production(
            city.city_id, option.category, option.item, best.x, best.y
        )
    result = await game.set_city_production(city.city_id, option.category, option.item)
    if option.category == "BUILDING" and result.startswith(_NO_COORDS_WONDER):
        # A wonder. The advisor ranks tiles by what building there would displace.
        placements = await game.get_wonder_advisor(city.city_id, option.item)
        if not isinstance(placements, list):
            return _advisor_failed(option.item, placements)
        if plan is not None:
            offered = [OwnedTile(p.x, p.y, 0) for p in placements]
            if await plan.chain(game, city, option, offered):
                placements = await game.get_wonder_advisor(city.city_id, option.item)
                if not isinstance(placements, list):
                    return _advisor_failed(option.item, placements)
                await plan.verify(city, option, [OwnedTile(p.x, p.y, 0) for p in placements])
                bought = plan.bought_tile
        trace.placements = len(placements)
        if not placements:
            return f"ERR:NO_PLACEMENT|{option.item}: no valid tile"
        best = min(placements, key=lambda p: (p.displacement_score, p.x, p.y))
        if bought is not None:
            best = next((p for p in placements if (p.x, p.y) == bought), best)
        trace.tile = (best.x, best.y)
        return await game.set_city_production(
            city.city_id, option.category, option.item, best.x, best.y
        )
    return result


@dataclass
class PlanContext:
    """What `_start` needs to chain a tile purchase: the planner and this turn's reads."""

    planner: Planner
    reads: PlacementReads

    async def chain(
        self, game: Any, city: City, option: ProductionChoice, offered: list[OwnedTile]
    ) -> bool:
        return await self.planner.consider(
            game, self.reads, city, option.category, option.item, offered
        )

    @property
    def bought_tile(self) -> tuple[int, int]:
        x, y, _ = self.planner._bought_at
        return x, y

    async def verify(self, city: City, option: ProductionChoice, offered: list[OwnedTile]) -> None:
        """After a purchase: log `buy_failed_place` if the game did not offer the bought tile."""
        await self.planner.placed(self.reads, city, option.item, offered)


async def _explain(
    reads: PlacementReads | None, city: City, option: ProductionChoice, reply: str
) -> tuple[str, str | None, str | None]:
    """Why the game refused: the reason class, the unlock it waits for, and the city's signature."""
    diagnosis = None
    if reads is not None and ("CANNOT_START" in reply or "CANNOT_PRODUCE" in reply):
        diagnosis = await reads.diagnose(city.city_id, option.category, option.item)
    reason, needs = classify(reply, diagnosis)
    signature = await reads.signature(city.city_id) if reads is not None else None
    return reason, needs, signature


async def choose_for_city(
    game: ProductionGame,
    city: City,
    options: Iterable[ProductionChoice],
    context: ProductionContext,
    weights: ProductionWeights,
    only_defence: bool = False,
    memory: RefusalMemory | None = None,
    emit: Callable[[Event], None] | None = None,
    reads: PlacementReads | None = None,
    planner: Planner | None = None,
) -> ProductionChoice | None:
    """Start the best option that the game accepts. Returns it, or None if all were refused.

    With `only_defence`, the city's current build is replaced only if the best option is a
    defender or walls, and only defence is tried. Items in `memory` for this city are skipped.
    `emit` receives a `ProductionSet` or `ProductionRefused` for each attempt. `reads` supplies the
    placement signatures and diagnoses (see `placement`), and `planner` the chained tile buys.
    """
    ranked = rank_production(city, options, context, weights)
    chain = PlanContext(planner, reads) if planner is not None and reads is not None else None
    if only_defence:
        if not ranked or not is_defence(ranked[0]) or score(ranked[0], city, context, weights) <= 0:
            return None
        ranked = [o for o in ranked if is_defence(o)]
    tried = 0
    for option in ranked:
        if tried >= MAX_ATTEMPTS:
            break
        # A remembered refusal is checked only when the item has won its place in the ranking, so
        # a changed signature by itself costs no call and gives the item no priority.
        if (
            memory is not None
            and memory.has(city, option.item)
            and await memory.blocked(city, option.item, context.turn, reads)
        ):
            continue
        # The game will not start a new district while the city is at its population limit, and
        # no tile changes that, so it is not tried. The cap is read once a turn for every city,
        # so the district competes again as soon as the city may hold one more.
        if option.category == "DISTRICT" and not option.is_repair and reads is not None:
            cap = await reads.district_cap(city.city_id, option.item)
            if cap is not None:
                log.debug(
                    "%s skips %s: %s of %s districts for its population",
                    city.name,
                    option.item,
                    cap.zoned,
                    cap.allowed,
                )
                continue
            # The game's own verdict (`CanStartOperation`, read with the signatures) names the
            # failure; one that tiles cannot change here costs no advisor call and no refusal. It
            # is read every turn, so the district competes again once the game allows it.
            verdict = await reads.district_denied(city.city_id, option.item)
            if verdict is not None:
                log.debug("%s skips %s: the game says %s", city.name, option.item, verdict)
                continue
        tried += 1
        trace = StartTrace()
        result = await _start(game, city, option, trace, chain)
        where = (context.turn, city.city_id, city.name, city.x, city.y)
        if not _failed(result):
            log.info("%s builds %s", city.name, option.item)
            if emit is not None:
                tile = trace.tile or (None, None)
                emit(
                    ProductionSet(
                        *where,
                        item=option.item,
                        category=option.category,
                        tile_x=tile[0],
                        tile_y=tile[1],
                        placements_offered=trace.placements,
                    )
                )
            if memory is not None:
                memory.accepted(city, option.item)
            placed = option.category == "DISTRICT" or trace.placements is not None
            if planner is not None and trace.tile is not None and placed and not option.is_repair:
                planner.reserve(city.city_id, option.item, *trace.tile)
            if planner is not None:
                planner.book.drop(city.city_id, option.item)  # Started: no tile to save for.
            return option
        log.info("%s can't build %s: %s", city.name, option.item, result[:120])
        refusal = is_refusal(result)
        wait, reason = 0, ""
        if refusal:
            reason, needs, signature = await _explain(reads, city, option, result)
            if memory is not None:
                wait = memory.refused(
                    city, option.item, context.turn, result, reason, signature, needs
                )
        if emit is not None:
            emit(
                ProductionRefused(
                    *where,
                    item=option.item,
                    category=option.category,
                    reply=result[:200],
                    is_refusal=refusal,
                    placements_offered=trace.placements,
                    backoff_turns=wait,
                    reason_class=reason,
                )
            )
    return None


def _should_switch(city: City, marked: frozenset[int] = frozenset()) -> bool:
    """A threatened city is building something that isn't defence and won't finish this turn."""
    threatened = city.is_threatened or city.city_id in marked
    if not threatened or city.is_idle or city.build_turns_left <= _FINISHING_TURNS:
        return False
    current = city.building or ""
    return not (defence_strength(current) > 0 or _short(current) in _DEFENSIVE_BUILDINGS)


class CityProductionPolicy:
    """A turn handler that gives every idle city something to build."""

    name = "city_production"

    def __init__(
        self,
        weights: ProductionWeights | None = None,
        bus: EventBus | None = None,
        expansion: ExpansionState | None = None,
        defence: Any = None,
        builder_demand: Any = None,
    ) -> None:
        self.builder_demand = builder_demand  # `builder.BuilderDemand`, or None.
        self._food_focus: set[int] = set()  # Cities this policy set to a food focus.
        self._reset: set[int] = set()  # Cities returned to default, which may starve again.
        self._kept: set[int] = set()  # Cities that starved again after a reset: focus stays.
        self._emit = bus.publish if bus is not None else None
        self.defence = defence  # The threat state (`defence.DefenceState`), or None.
        self.weights = weights or ProductionWeights()  # The strategist replaces this later.
        self.refusals = RefusalMemory()
        self.planner = Planner(
            emit=self._emit
        )  # Chained tile buys. Its `book` is the gold policy's saving list.
        self.expansion = expansion if expansion is not None else ExpansionState()

    async def run(self, game: ProductionGame) -> None:
        unfinished = await self.assign(game)
        if unfinished:
            # Not an error: the blocker resolver's cheapest-item default runs after this.
            log.warning("No production chosen for %s", ", ".join(unfinished))

    def _context(self, empire, units, in_progress, cities) -> ProductionContext:
        demand = self.builder_demand
        context = build_context(
            empire,
            units,
            in_progress,
            cities,
            self.defence,
            idle_builders=demand.idle if demand is not None else 0,
            spare_jobs=demand.spare_jobs if demand is not None else None,
        )
        self.expansion.target_cities = self.weights.target_cities
        return replace(context, sites_ok=self.expansion.sites_ok(empire.turn))

    async def assign(self, game: ProductionGame) -> list[str]:
        """Choose for each idle city, and return the names of any left without a choice.

        A threatened city that is building something else switches to a defender or walls when
        that is the best thing on offer, unless the current item is about to finish. A city
        already building a defender or walls is left alone, so a switch never repeats.
        """
        cities = await _read_cities(game)
        await self._feed_starving(game, cities)
        marked = self.defence.threatened_ids if self.defence is not None else frozenset()
        switching = [c for c in cities if _should_switch(c, marked)]
        idle = [c for c in cities if c.is_idle]
        if not switching and not idle:
            return []
        empire, units = await _read_empire(game), await _read_units(game)
        reads = PlacementReads(game, self.refusals.watched())
        self.planner.begin_turn(
            empire.turn,
            self._plan_budget(empire),
            empire.gold_per_turn,
            empire.game_speed,
            empire.max_turns,
        )
        building = {c.city_id: c.building for c in cities if c.building is not None}
        # First the switches, which only ever choose defence. Each one replaces what the city was
        # building, and a refused switch leaves it building, so the count below stays true.
        for city in sorted(switching, key=lambda c: c.city_id):
            options = await _read_production(game, city.city_id)
            context = self._context(empire, units, building.values(), cities)
            picked = await choose_for_city(
                game,
                city,
                options,
                context,
                self.weights,
                only_defence=True,
                memory=self.refusals,
                emit=self._emit,
                reads=reads,
            )
            if picked is not None:
                building[city.city_id] = picked.item
                self._note(city, f"production: switched to {picked.item}")
        context = self._context(empire, units, building.values(), cities)
        unfinished = []
        for city in sorted(idle, key=lambda c: (not under_threat(c, context), c.city_id)):
            options = await _read_production(game, city.city_id)
            picked = await choose_for_city(
                game,
                city,
                options,
                context,
                self.weights,
                memory=self.refusals,
                emit=self._emit,
                reads=reads,
                planner=self.planner,
            )
            if picked is None:
                unfinished.append(city.name)
            else:
                context = context.after_choosing(picked.item)
                self._note(city, f"production: chose {picked.item}")
        return unfinished

    def _plan_budget(self, empire: Empire) -> int:
        """Gold the planner may spend on a tile this turn: none while a city is threatened."""
        from civ6_ai.policies.gold import reserve_for  # Gold imports this module.

        if self.defence is not None and self.defence.any:
            return 0
        return int(empire.gold) - reserve_for(empire.num_cities, empire.gold_per_turn)

    async def _feed_starving(self, game: ProductionGame, cities: list[City]) -> None:
        """Set a food focus on a starving city, and clear it once the city has recovered.

        One `set_city_focus` call per change, never per turn: only a city that starts starving, or
        one this policy focused that now gains `STARVING_FOCUS_RECOVERED` food a turn, costs a call.
        A city that starves again after its reset keeps the focus for good, since the focus's own
        food was what lifted it. A failed call is tried again next turn.
        """
        for city in cities:
            if is_starving(city) and city.city_id not in self._food_focus:
                focus, add = "food", True
            elif (
                city.city_id in self._food_focus
                and city.city_id not in self._kept
                and city.food_surplus >= STARVING_FOCUS_RECOVERED
            ):
                focus, add = "default", False
            else:
                continue
            try:
                result = await game.set_city_focus(city.city_id, focus)
            except Exception as err:  # A focus is a nicety. It must never stop production.
                log.warning("%s: set_city_focus %s failed: %s", city.name, focus, err)
                continue
            if _failed(str(result)):
                log.info("%s: set_city_focus %s refused: %s", city.name, focus, str(result)[:120])
                continue
            log.info("%s: focus %s (food %+.1f)", city.name, focus, city.food_surplus)
            if add:
                self._food_focus.add(city.city_id)
                if city.city_id in self._reset:  # The deficit came back: the focus was the cure.
                    self._kept.add(city.city_id)
            else:
                self._food_focus.discard(city.city_id)
                self._reset.add(city.city_id)

    def _note(self, city: City, text: str) -> None:
        if self.defence is not None:
            self.defence.note(city.city_id, text)

    def blocker_policies(self) -> Mapping[str, Any]:
        """Policies to pass to `BlockerResolver`, which takes over the production blocker."""
        return {BlockerType.PRODUCTION: self.resolve}

    async def resolve(self, game: ProductionGame, blocker: Blocker) -> bool:
        return not await self.assign(game)
