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
from civ6_ai.policies.districts import BASE_VALUE, PLANNED_VALUE, is_planned, wants_district
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
REFUSAL_BACKOFF_TURNS = 5  # Turns to skip an item after its first refusal for a city.
REFUSAL_BACKOFF_MAX_TURNS = 40  # Each repeat refusal doubles the wait, up to this.
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

    def after_choosing(self, item: str, unknown_combat: bool = False) -> "ProductionContext":
        """Count a pick, so two cities don't both build the one builder the empire needs."""
        kinds = _counted_as(item) or (("army",) if unknown_combat else ())
        counted = replace(self, **{kind: getattr(self, kind) + 1 for kind in kinds})
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
) -> ProductionContext:
    """Count what exists plus what cities are already building, so picks don't double up."""
    ctx = ProductionContext(
        turn=empire.turn,
        num_cities=empire.num_cities,
        trade_capacity=empire.trade_capacity,
        districts=frozenset(_short(d.district_type) for c in cities for d in c.districts),
    )
    for unit in units:
        ctx = ctx.after_choosing(
            unit.unit_type, unknown_combat=unit.is_combat and unit.unit_type not in COMBAT_UNITS
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
            if option.item not in COMBAT_UNITS and (
                canonical(option.item) not in _CIVILIAN_UNITS
                and any(word in option.item for word in _UNPLANNED_WORDS)
            ):
                continue
        keep.append(option)
    return keep, repairs


def is_defence(option: ProductionChoice) -> bool:
    """A unit that holds a city, or the walls that help it."""
    if option.category == "UNIT":
        return defence_strength(option.item) > 0
    return option.category == "BUILDING" and _short(option.item) in _DEFENSIVE_BUILDINGS


_MAX_DEFENDERS_PER_CITY = 3


def is_pressed(city: City, ctx: ProductionContext) -> bool:
    """Threatened and short of defenders, so defence outranks expansion.

    A threat that sits next to a garrisoned city for turns (barbarians camping live on turn 9 to
    25) must not stop settlers and builders forever. Once there are enough defenders, the
    threat only keeps the usual military scoring.
    """
    if not city.is_threatened:
        return False
    wanted = min(len(city.threats) + 1, _MAX_DEFENDERS_PER_CITY) * max(1, ctx.num_cities)
    return ctx.army < wanted


def _unit_score(
    option: ProductionChoice, city: City, ctx: ProductionContext, w: ProductionWeights
) -> float:
    item = option.item
    kind = canonical(item)
    threatened = is_pressed(city, ctx)
    if kind == "UNIT_SETTLER":
        if (
            threatened
            or city.pop < _SETTLER_MIN_POP
            or ctx.num_cities + ctx.settlers >= w.target_cities
            or ctx.settlers >= 1
        ):
            return 0.0
        return 14.0 * w.expansion
    if kind == "UNIT_BUILDER":
        wanted = max(1, ctx.num_cities) - ctx.builders
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
        return 14.0 + strength / 4.0 + 2.0 * len(city.threats)
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
        return 0.0 if city.is_threatened else _WONDER_VALUE * w.wonders
    if name in _DEFENSIVE_BUILDINGS:
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
    return value


def _district_score(option: ProductionChoice, city: City, w: ProductionWeights) -> float:
    name = _short(option.item)
    if name in BASE_VALUE:  # It passed `wants_district`, so it is worth building.
        return PLANNED_VALUE if is_planned(name, w.victory) else BASE_VALUE[name]
    profile = _DISTRICT_YIELDS.get(name)
    if profile is None:
        return _UNKNOWN_BUILDING * 2.0
    value = _yield_value(profile, w)
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
    if city.is_threatened and is_defence(option):
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


def is_refusal(result: str) -> bool:
    """The game said it won't build this item, not a call that failed for another reason."""
    return _failed(result) and any(code in result for code in _REFUSAL_CODES)


def _circumstances(city: City) -> tuple[int, int]:
    """What changes whether an item can be built: a new district or building."""
    return len(city.districts), len(city.buildings)


class RefusalMemory:
    """Items the game refused for a city, skipped until a back-off passes.

    The wait is `REFUSAL_BACKOFF_TURNS`, doubled for each repeat refusal of the same city and item,
    up to `REFUSAL_BACKOFF_MAX_TURNS`. It ends early when the city gains a district or building.
    Kept in memory only: a restart forgets it, which costs one retry per item (a few calls).
    """

    def __init__(self) -> None:
        # (city_id, item) -> (skip until this turn, refusals so far, city circumstances)
        self._entries: dict[tuple[int, str], tuple[int, int, tuple[int, int]]] = {}

    def blocked(self, city: City, item: str, turn: int) -> bool:
        entry = self._entries.get((city.city_id, item))
        if entry is None:
            return False
        until, _, seen = entry
        # Once expired or changed it is free again, but the count stays so a repeat waits longer.
        return turn < until and _circumstances(city) == seen

    def refused(self, city: City, item: str, turn: int) -> int:
        """Record a refusal and return how many turns the item is now skipped for."""
        key = (city.city_id, item)
        count = self._entries[key][1] + 1 if key in self._entries else 1
        wait = min(REFUSAL_BACKOFF_TURNS * 2 ** (count - 1), REFUSAL_BACKOFF_MAX_TURNS)
        self._entries[key] = (turn + wait, count, _circumstances(city))
        return wait

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
    game: ProductionGame, city: City, option: ProductionChoice, trace: StartTrace | None = None
) -> str:
    """Set production, finding a tile first where the item needs one."""
    trace = trace if trace is not None else StartTrace()
    if option.is_repair:
        trace.tile = (option.repair_x, option.repair_y)
        return await game.set_city_production(
            city.city_id, option.category, option.item, option.repair_x, option.repair_y
        )
    if option.category == "DISTRICT":
        placements = await game.get_district_advisor(city.city_id, option.item)
        if not isinstance(placements, list):
            return _advisor_failed(option.item, placements)
        trace.placements = len(placements)
        if not placements:
            return f"ERR:NO_PLACEMENT|{option.item}: no valid tile"
        best = min(placements, key=lambda p: (-p.total_adjacency, p.x, p.y))
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
        trace.placements = len(placements)
        if not placements:
            return f"ERR:NO_PLACEMENT|{option.item}: no valid tile"
        best = min(placements, key=lambda p: (p.displacement_score, p.x, p.y))
        trace.tile = (best.x, best.y)
        return await game.set_city_production(
            city.city_id, option.category, option.item, best.x, best.y
        )
    return result


async def choose_for_city(
    game: ProductionGame,
    city: City,
    options: Iterable[ProductionChoice],
    context: ProductionContext,
    weights: ProductionWeights,
    only_defence: bool = False,
    memory: RefusalMemory | None = None,
    emit: Callable[[Event], None] | None = None,
) -> ProductionChoice | None:
    """Start the best option that the game accepts. Returns it, or None if all were refused.

    With `only_defence`, the city's current build is replaced only if the best option is a
    defender or walls, and only defence is tried. Items in `memory` for this city are skipped.
    `emit` receives a `ProductionSet` or `ProductionRefused` for each attempt.
    """
    ranked = rank_production(city, options, context, weights)
    if memory is not None:
        ranked = [o for o in ranked if not memory.blocked(city, o.item, context.turn)]
    if only_defence:
        if not ranked or not is_defence(ranked[0]) or score(ranked[0], city, context, weights) <= 0:
            return None
        ranked = [o for o in ranked if is_defence(o)]
    for option in ranked[:MAX_ATTEMPTS]:
        trace = StartTrace()
        result = await _start(game, city, option, trace)
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
            return option
        log.info("%s can't build %s: %s", city.name, option.item, result[:120])
        refusal = is_refusal(result)
        wait = 0
        if memory is not None and refusal:
            wait = memory.refused(city, option.item, context.turn)
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
                )
            )
    return None


def _should_switch(city: City) -> bool:
    """A threatened city is building something that isn't defence and won't finish this turn."""
    if not city.is_threatened or city.is_idle or city.build_turns_left <= _FINISHING_TURNS:
        return False
    current = city.building or ""
    return not (defence_strength(current) > 0 or _short(current) in _DEFENSIVE_BUILDINGS)


class CityProductionPolicy:
    """A turn handler that gives every idle city something to build."""

    name = "city_production"

    def __init__(
        self, weights: ProductionWeights | None = None, bus: EventBus | None = None
    ) -> None:
        self._emit = bus.publish if bus is not None else None
        self.weights = weights or ProductionWeights()  # The strategist replaces this later.
        self.refusals = RefusalMemory()

    async def run(self, game: ProductionGame) -> None:
        unfinished = await self.assign(game)
        if unfinished:
            # Not an error: the blocker resolver's cheapest-item default runs after this.
            log.warning("No production chosen for %s", ", ".join(unfinished))

    async def assign(self, game: ProductionGame) -> list[str]:
        """Choose for each idle city, and return the names of any left without a choice.

        A threatened city that is building something else switches to a defender or walls when
        that is the best thing on offer, unless the current item is about to finish. A city
        already building a defender or walls is left alone, so a switch never repeats.
        """
        cities = await _read_cities(game)
        switching = [c for c in cities if _should_switch(c)]
        idle = [c for c in cities if c.is_idle]
        if not switching and not idle:
            return []
        empire, units = await _read_empire(game), await _read_units(game)
        building = {c.city_id: c.building for c in cities if c.building is not None}
        # First the switches, which only ever choose defence. Each one replaces what the city was
        # building, and a refused switch leaves it building, so the count below stays true.
        for city in sorted(switching, key=lambda c: c.city_id):
            options = await _read_production(game, city.city_id)
            context = build_context(empire, units, building.values(), cities)
            picked = await choose_for_city(
                game,
                city,
                options,
                context,
                self.weights,
                only_defence=True,
                memory=self.refusals,
                emit=self._emit,
            )
            if picked is not None:
                building[city.city_id] = picked.item
        context = build_context(empire, units, building.values(), cities)
        unfinished = []
        for city in sorted(idle, key=lambda c: (not c.is_threatened, c.city_id)):
            options = await _read_production(game, city.city_id)
            picked = await choose_for_city(
                game, city, options, context, self.weights, memory=self.refusals, emit=self._emit
            )
            if picked is None:
                unfinished.append(city.name)
            else:
                context = context.after_choosing(picked.item)
        return unfinished

    def blocker_policies(self) -> Mapping[str, Any]:
        """Policies to pass to `BlockerResolver`, which takes over the production blocker."""
        return {BlockerType.PRODUCTION: self.resolve}

    async def resolve(self, game: ProductionGame, blocker: Blocker) -> bool:
        return not await self.assign(game)
