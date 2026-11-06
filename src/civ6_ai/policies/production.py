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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any, Protocol

from civ6_ai.blockers import Blocker, BlockerGame, BlockerType
from civ6_ai.state import City, Empire, ProductionChoice, Unit
from civ6_ai.state import read_cities as _read_cities
from civ6_ai.state import read_empire as _read_empire
from civ6_ai.state import read_production as _read_production
from civ6_ai.state import read_units as _read_units

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6  # Per city per turn. Every refusal costs a call, and a unit always works.
_NO_COORDS_WONDER = "ERR:MISSING_COORDS"
_TURN_SCALE = 20.0  # A build this many turns away scores half as much as an instant one.
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
    wonders: float = 0.5
    target_cities: int = 6  # Stop building settlers at this many cities.
    army_per_city: float = 1.5  # Combat units wanted per city before military stops scoring.

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
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

    def after_choosing(self, item: str) -> "ProductionContext":
        """Count a pick, so two cities don't both build the one builder the empire needs."""
        if item == "UNIT_BUILDER":
            return ProductionContext(**{**vars(self), "builders": self.builders + 1})
        if item == "UNIT_SETTLER":
            return ProductionContext(**{**vars(self), "settlers": self.settlers + 1})
        if item == "UNIT_SCOUT":
            return ProductionContext(**{**vars(self), "recon": self.recon + 1})
        if item in _UNIT_LINE:
            return ProductionContext(**{**vars(self), "army": self.army + 1})
        return self


def build_context(empire: Empire, units: Iterable[Unit]) -> ProductionContext:
    counts: dict[str, int] = {}
    army = 0
    for unit in units:
        counts[unit.unit_type] = counts.get(unit.unit_type, 0) + 1
        army += unit.is_combat
    return ProductionContext(
        turn=empire.turn,
        num_cities=empire.num_cities,
        builders=counts.get("UNIT_BUILDER", 0),
        settlers=counts.get("UNIT_SETTLER", 0),
        recon=counts.get("UNIT_SCOUT", 0),
        army=army,
    )


# Each line is ordered weakest first. Offering a stronger unit drops the weaker ones in its line,
# because it costs a little more for far more strength, and the weaker one can't be upgraded to.
_MILITARY_LINES = (
    ("UNIT_WARRIOR", "UNIT_SWORDSMAN", "UNIT_MUSKETMAN", "UNIT_RIFLEMAN", "UNIT_INFANTRY"),
    ("UNIT_SLINGER", "UNIT_ARCHER", "UNIT_CROSSBOWMAN", "UNIT_FIELD_CANNON", "UNIT_MACHINE_GUN"),
    ("UNIT_SPEARMAN", "UNIT_PIKEMAN", "UNIT_PIKE_AND_SHOT", "UNIT_ANTITANK_GUN"),
    ("UNIT_HORSEMAN", "UNIT_KNIGHT", "UNIT_CAVALRY", "UNIT_HELICOPTER"),
    ("UNIT_CATAPULT", "UNIT_TREBUCHET", "UNIT_BOMBARD", "UNIT_ARTILLERY"),
)
_UNIT_LINE: dict[str, tuple[str, ...]] = {u: line for line in _MILITARY_LINES for u in line}
_RANGED_DEFENDERS = frozenset({"UNIT_ARCHER", "UNIT_CROSSBOWMAN", "UNIT_SLINGER"})
_CIVILIAN_UNITS = frozenset({"UNIT_BUILDER", "UNIT_SETTLER", "UNIT_SCOUT"})

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
_UNWANTED_DISTRICTS = frozenset({"SPACEPORT", "AERODROME", "DAM", "CANAL", "STADIUM"})
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
    options: Iterable[ProductionChoice], city: City
) -> tuple[list[ProductionChoice], list[ProductionChoice]]:
    """Split options into those worth ranking and those that are never worth building.

    Dominated means another offered option does the same job better, or the city already has it.
    Repairs are kept apart because they have their own coordinates. Returns (choices, repairs).
    """
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
        if option.category == "DISTRICT" and (name in districts or name in _UNWANTED_DISTRICTS):
            continue
        if option.category == "UNIT":
            line = _UNIT_LINE.get(option.item)
            if line and any(
                stronger in offered for stronger in line[line.index(option.item) + 1 :]
            ):
                continue
            if not line and option.item not in _CIVILIAN_UNITS:
                continue  # Naval, religious, trade and support units need a plan this lacks.
        keep.append(option)
    return keep, repairs


def _unit_score(
    option: ProductionChoice, city: City, ctx: ProductionContext, w: ProductionWeights
) -> float:
    item = option.item
    threatened = city.is_threatened
    if item == "UNIT_SETTLER":
        if (
            threatened
            or city.pop < _SETTLER_MIN_POP
            or ctx.num_cities + ctx.settlers >= w.target_cities
            or ctx.settlers >= 1
        ):
            return 0.0
        return 14.0 * w.expansion
    if item == "UNIT_BUILDER":
        wanted = max(1, ctx.num_cities) - ctx.builders
        if wanted <= 0 or threatened:
            return 0.0
        return (6.0 + 2.0 * min(wanted, 3) + len(city.unimproved_resources)) * w.builders
    if item == "UNIT_SCOUT":
        return 5.0 if ctx.recon == 0 and ctx.turn <= _EARLY_TURNS else 0.0
    # Military.
    ungarrisoned = city.garrison_unit is None
    if threatened:
        # Several enemies near need several defenders. Ranged units hit them without being hit.
        base = 18.0 + 2.0 * len(city.threats)
        if item in _RANGED_DEFENDERS:
            base += 2.0
        return base
    if ungarrisoned:
        return 16.0 if ctx.turn <= _EARLY_TURNS else 12.0
    wanted = w.army_per_city * ctx.num_cities - ctx.army
    if wanted <= 0:
        return 0.0
    return min(wanted, 4.0) * 1.5 * w.military


def _building_score(
    option: ProductionChoice, city: City, ctx: ProductionContext, w: ProductionWeights
) -> float:
    name = _short(option.item)
    if name in _DEFENSIVE_BUILDINGS:
        if city.is_threatened and not city.has_walls:
            return 17.0 + len(city.threats)
        return 0.0  # Unthreatened, walls are an expense with no return.
    if name in _TRAINING_BUILDINGS:
        return 0.5 * w.military
    profile = _BUILDING_YIELDS.get(name)
    if profile is None:
        # An unlisted building can be a wonder, which needs a tile this policy finds on demand.
        return _UNKNOWN_BUILDING * (w.wonders if "WONDER" in name else 1.0)
    value = _yield_value(profile, w)
    if name == "GRANARY" and city.food_surplus > 3:
        value *= 0.5
    return value


def _district_score(option: ProductionChoice, city: City, w: ProductionWeights) -> float:
    profile = _DISTRICT_YIELDS.get(_short(option.item))
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
    urgent = city.is_threatened and option.category == "UNIT" and option.item in _UNIT_LINE
    if urgent:
        return value  # Time matters less than having it, and slow units are already dominated.
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
    keep, repairs = drop_dominated(list(options), city)
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


def _failed(result: str) -> bool:
    return result.startswith(("Error", "ERR:"))


async def _start(game: ProductionGame, city: City, option: ProductionChoice) -> str:
    """Set production, finding a tile first where the item needs one."""
    if option.is_repair:
        return await game.set_city_production(
            city.city_id, option.category, option.item, option.repair_x, option.repair_y
        )
    if option.category == "DISTRICT":
        placements = await game.get_district_advisor(city.city_id, option.item)
        if not isinstance(placements, list) or not placements:
            return f"ERR:NO_PLACEMENT|{option.item}: {placements or 'no valid tile'}"
        best = min(placements, key=lambda p: (-p.total_adjacency, p.x, p.y))
        return await game.set_city_production(
            city.city_id, option.category, option.item, best.x, best.y
        )
    result = await game.set_city_production(city.city_id, option.category, option.item)
    if option.category == "BUILDING" and result.startswith(_NO_COORDS_WONDER):
        # A wonder. The advisor ranks tiles by what building there would displace.
        placements = await game.get_wonder_advisor(city.city_id, option.item)
        if not isinstance(placements, list) or not placements:
            return f"ERR:NO_PLACEMENT|{option.item}: {placements or 'no valid tile'}"
        best = min(placements, key=lambda p: (p.displacement_score, p.x, p.y))
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
) -> ProductionChoice | None:
    """Start the best option that the game accepts. Returns it, or None if all were refused."""
    ranked = rank_production(city, options, context, weights)
    for option in ranked[:MAX_ATTEMPTS]:
        result = await _start(game, city, option)
        if not _failed(result):
            log.info("%s builds %s", city.name, option.item)
            return option
        log.info("%s can't build %s: %s", city.name, option.item, result[:120])
    return None


class CityProductionPolicy:
    """A turn handler that gives every idle city something to build."""

    name = "city_production"

    def __init__(self, weights: ProductionWeights | None = None) -> None:
        self.weights = weights or ProductionWeights()  # The strategist replaces this later.

    async def run(self, game: ProductionGame) -> None:
        unfinished = await self.assign(game)
        if unfinished:
            # Not an error: the blocker resolver's cheapest-item default runs after this.
            log.warning("No production chosen for %s", ", ".join(unfinished))

    async def assign(self, game: ProductionGame) -> list[str]:
        """Choose for each idle city and return the names of any left without a choice."""
        cities = [city for city in await _read_cities(game) if city.is_idle]
        if not cities:
            return []
        context = build_context(await _read_empire(game), await _read_units(game))
        unfinished = []
        for city in sorted(cities, key=lambda c: (not c.is_threatened, c.city_id)):
            options = await _read_production(game, city.city_id)
            picked = await choose_for_city(game, city, options, context, self.weights)
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
