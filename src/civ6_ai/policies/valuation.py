"""Return on investment, shared by gold and faith spending.

Run 12 showed that nothing in the spending code weighed what an item gives against what it costs.
The gold policy took the item production ranked first and saved for it whatever its price (a
1,015 gold Food Market, valued as "unknown building 1.0", was the saving target on turns when a
240 gold Library was on offer). The faith policy refused every great person "not worth saving for"
when the wait passed a turn count, while faith had no other use. A price of this size was judged
against a count of turns, never against what it buys.

This module is the missing valuation. It is pure (no game calls), so both policies and the tests
use the same numbers.

Value of an item, in "yield points" (one point is one weighted yield for one turn):

    value = rate * horizon(turn, last_turn) + fixed

- `rate` is the yield per turn the item adds, weighted by `ProductionWeights` (so science counts
  more than faith, as the bot's priorities say). Buildings use production's own yield table
  (`_BUILDING_YIELDS`) and `_EXTRA_BUILDINGS` below for the late ones production lacks. Units and
  other non-yield items use production's undiscounted score times `UNIT_RATE_PER_SCORE`, so a
  Settler, a Builder or a garrison keep the order production gives them.
- `horizon` counts the turns of benefit left, each discounted by `DISCOUNT`, from `HORIZON_CAP`
  turns at most and fewer as the game's last turn nears. An item bought on turn 320 of 330 is
  worth about a third of one bought on turn 200.
- `fixed` is the time a purchase saves: for the item the city is building now, the turns left
  times its yield plus the production freed (its output goes to the next item). An item the city
  is not building is bought as an addition, so it has no `fixed`. A build one turn from done
  therefore has almost no value to buy.

    ROI = value / cost

Idle gold or faith has no return (it is not in the score), so the bar for buying falls as the
stock grows, `floor`: `ROI_FLOOR` with nothing spare, halving when the spare amount reaches
`HOARD_TURNS` turns of income, never below `ROI_FLOOR_MIN`.

Saving. Buying A now delays a better item B that is out of reach by `cost_A / income` turns, and
waiting for B first delays A by about as long. Comparing the value lost either way, A first is
better exactly when

    value_A * (gap_B + cost_A) >= value_B * cost_A        (gap_B = cost_B - what is held)

(`buy_now_beats_saving`). When B is nearly affordable (a small gap), only an item at least as
good per gold as B goes first; when B is far off, anything above the floor does. B is the
unaffordable item with the best ROI, valued at the turn it will be reached, so a late game
removes distant targets by itself.

Great people are one-shot: value is `GP_UNIT_VALUE` times the class weight (the bot's own
`gp_*` weights) times an era factor, cut as the game's end nears (`GP_PAYBACK_TURNS`).
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from civ6_ai.policies.game_data import WONDERS
from civ6_ai.policies.production import (
    _BUILDING_YIELDS,
    _DEFENSIVE_BUILDINGS,
    _TRAINING_BUILDINGS,
    ProductionContext,
    ProductionWeights,
    _short,
    _yield_value,
    canonical,
    is_defence,
    score,
    under_threat,
)
from civ6_ai.policies.secondary import _GP_WEIGHTS, SecondaryWeights
from civ6_ai.state import City, ProductionChoice
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance

HORIZON_CAP = 60  # Turns of benefit counted at most.
DISCOUNT = 0.03  # Per turn: a yield later is worth less (compounding, risk, the game's end).
ROI_FLOOR = 0.10  # Value per gold needed to buy when nothing is spare.
ROI_FLOOR_MIN = 0.02  # Never below this: an item that returns almost nothing is not bought.
HOARD_TURNS = 10  # Spare gold of this many turns of income halves the floor.
HOARD_MIN = 100.0  # ... and at least this much, when income is small.
UNIT_RATE_PER_SCORE = 0.4  # Yield points per turn for each point of production's unit score.
SETTLER_RATE_PER_SCORE = 0.8  # A city is worth more than a score of 14 says.
DEFENCE_ROI = 100.0  # A defender or walls under threat outrank every return (kept from before).
GP_UNIT_VALUE = 40.0  # Value of a class-weight-1 great person in the ancient era.
GP_ERA_STEP = 0.5  # Each era adds this fraction of the ancient value.
GP_PAYBACK_TURNS = 20  # A great person bought with fewer turns left than this is cut pro rata.
MAX_WAIT_FRACTION = 0.6  # Do not save for more than this share of the turns left.

# Buildings production's table lacks (they were all scored "unknown, 1.0"): (food, production,
# gold, science, culture, faith) per turn, in the same rough scale as production's own table.
_EXTRA_BUILDINGS: dict[str, tuple[float, float, float, float, float, float]] = {
    "FOOD_MARKET": (2.5, 0, 0, 0, 0, 0),
    "SHOPPING_MALL": (0, 0, 5.0, 0, 0, 0),
    "BROADCAST_CENTER": (0, 0, 0, 0, 4.0, 0),
    "ZOO": (0, 0, 0, 0, 3.0, 0),
    "STADIUM": (0, 0, 0, 0, 3.0, 0),
    "COAL_POWER_PLANT": (0, 4.0, 0, 0, 0, 0),
    "FOSSIL_FUEL_POWER_PLANT": (0, 4.0, 0, 0, 0, 0),
    "NUCLEAR_POWER_PLANT": (0, 4.0, 0, 0, 0, 0),
    "HYDROELECTRIC_DAM": (0, 3.0, 0, 0, 0, 0),
    "ELECTRONICS_FACTORY": (0, 4.0, 0, 0, 0, 0),
    "AIRPORT": (0, 0, 0, 0, 2.0, 0),
    "GROVE": (2.0, 0, 0, 0, 0, 0),
    "MEETING_HOUSE": (0, 0, 0, 0, 0, 3.0),
    "MOSQUE": (0, 0, 0, 0, 0, 3.0),
    "GURDWARA": (0, 0, 0, 0, 0, 3.0),
    "PAGODA": (0, 0, 0, 0, 0, 3.0),
    "STUPA": (0, 0, 0, 0, 0, 3.0),
    "SYNAGOGUE": (0, 0, 0, 0, 0, 3.0),
    "WAT": (0, 0, 0, 0, 0, 3.0),
    "DAR_E_MEHR": (0, 0, 0, 0, 0, 3.0),
    "GOV_CONQUEST": (0, 2.0, 0, 0, 0, 0),
    "GOV_CITYSTATES": (0, 0, 2.0, 0, 0, 0),
    "GOV_SPIES": (0, 0, 0, 0, 2.0, 0),
    "GOV_FAITH": (0, 0, 0, 0, 0, 3.0),
    "GOV_CULTURE": (0, 0, 0, 0, 3.0, 0),
    "GOV_MILITARY": (0, 2.0, 0, 0, 0, 0),
    "GOV_TALL": (0, 0, 2.0, 0, 0, 0),
    "GOV_WIDE": (0, 2.0, 0, 0, 0, 0),
    "GOV_SCIENCE": (0, 0, 0, 3.0, 0, 0),
    "FLOOD_BARRIER": (0.5, 0, 0, 0, 0, 0),  # Protects tiles from floods: a little food.
}
_ERAS = (
    "ancient",
    "classical",
    "medieval",
    "renaissance",
    "industrial",
    "modern",
    "atomic",
    "information",
    "future",
)


@dataclass(frozen=True)
class Valuation:
    """What an item is worth: `rate` a turn over the horizon plus a one-off `fixed` part."""

    rate: float = 0.0
    fixed: float = 0.0
    basis: str = ""
    always: bool = False  # Defence under threat: bought before any return is compared.
    payback: float = 0.0  # One-shot items: `fixed` is cut when fewer turns than this remain.

    def at(self, turn: int, last_turn: int, wait: float = 0.0) -> float:
        """The value of having it `wait` turns from `turn` (a one-shot's payback shrinks too)."""
        fixed = self.fixed
        if self.payback > 0:
            fixed *= min(1.0, max(0.0, last_turn - turn - wait) / self.payback)
        return self.rate * horizon(turn, last_turn, wait) + fixed

    def value(self, horizon_now: float) -> float:
        return self.rate * horizon_now + self.fixed


@dataclass(frozen=True)
class Priced:
    """A valued item with its price, as the policies pass it around."""

    label: str
    cost: int
    valuation: Valuation
    value: float  # At the current horizon.

    @property
    def roi(self) -> float:
        if self.valuation.always:
            return DEFENCE_ROI
        return self.value / self.cost if self.cost > 0 else 0.0


def horizon(turn: int, last_turn: int, wait: float = 0.0) -> float:
    """Discounted turns of benefit for an item in place `wait` turns from now."""
    left = int(max(0.0, last_turn - turn - wait))
    n = min(left, HORIZON_CAP)
    return (1.0 - (1.0 - DISCOUNT) ** n) / DISCOUNT


def floor(spare: float, income: float) -> float:
    """The ROI an item needs to be bought, falling as spare currency piles up unused."""
    scale = max(HOARD_TURNS * max(income, 0.0), HOARD_MIN)
    return max(ROI_FLOOR_MIN, ROI_FLOOR / (1.0 + max(spare, 0.0) / scale))


def buy_now_beats_saving(value_a: float, cost_a: float, value_b: float, gap_b: float) -> bool:
    """True when buying A now is worth more than keeping the money for the out-of-reach B."""
    if cost_a <= 0:
        return True
    return value_a * (max(gap_b, 0.0) + cost_a) >= value_b * cost_a


def value_option(
    option: ProductionChoice,
    city: City,
    ctx: ProductionContext,
    weights: ProductionWeights,
) -> Valuation | None:
    """What buying this option is worth, or None when production scores it as worth nothing."""
    # Production's own value, undiscounted by build time (a purchase has no build time).
    # The early Monument bonus (issue 299) only ranks builds; as a per-turn rate it would make a
    # Monument look several times better than its +2 culture, so it is left out here.
    plain = replace(weights, early_monument=0.0)
    raw = score(replace(option, turns=0), city, ctx, plain)
    if raw <= 0:
        return None
    if under_threat(city, ctx) and is_defence(option):
        return Valuation(raw, basis="defence under threat", always=True)
    name = _short(option.item)
    if option.category == "UNIT":
        settler = canonical(option.item) == "UNIT_SETTLER"
        rate = raw * (SETTLER_RATE_PER_SCORE if settler else UNIT_RATE_PER_SCORE)
        basis = "unit score"
    elif (
        option.item in WONDERS
        or name in _BUILDING_YIELDS
        or name in _DEFENSIVE_BUILDINGS
        or name in _TRAINING_BUILDINGS
    ):
        rate, basis = raw, "production score"  # Includes the city adjustments production makes.
    elif name in _EXTRA_BUILDINGS:
        rate, basis = _yield_value(_EXTRA_BUILDINGS[name], weights), "extra yield table"
    else:
        rate, basis = raw, "unknown building"
    if city.building == option.item and city.build_turns_left > 0:
        # The city is building it: buying only brings it forward and frees the production.
        left = city.build_turns_left
        freed = max(city.production, 0.0) * left * weights.production
        return Valuation(0.0, rate * min(left, HORIZON_CAP) + freed, f"{basis}, saves {left} turns")
    return Valuation(rate, 0.0, basis)


BUILDER_RATE = 3.0  # Yield points per turn a Builder adds (production's score floor for one).


def catalogue_valuation(category: str, item: str, weights: ProductionWeights) -> Valuation | None:
    """A building or Builder valued from the tables alone, for a buyer with no city context.

    The faith policy has no `ProductionContext`, so it uses the yield tables directly. Anything
    the tables don't know gets the unknown-building value of 1.0, as production scores it.
    """
    if category == "UNIT":
        return Valuation(BUILDER_RATE, 0.0, "builder") if item == "UNIT_BUILDER" else None
    name = _short(item)
    profile = _BUILDING_YIELDS.get(name) or _EXTRA_BUILDINGS.get(name)
    if profile is None:
        return Valuation(1.0, 0.0, "unknown building")
    return Valuation(_yield_value(profile, weights), 0.0, "yield table")


def price(label: str, cost: int, valuation: Valuation, turn: int, last_turn: int) -> Priced:
    return Priced(label, cost, valuation, valuation.at(turn, last_turn))


def era_index(era_name: str) -> int:
    text = (era_name or "").lower()
    for index, era in enumerate(_ERAS):
        if era in text:
            return index
    return 0


def great_person_valuation(
    person: Any, weights: SecondaryWeights, turn: int, last_turn: int, wait: float = 0.0
) -> Valuation:
    """A great person: one-shot, so no horizon, but cut when the game is nearly over."""
    attr = _GP_WEIGHTS.get(person.class_name)
    class_weight = getattr(weights, attr) if attr else 0.5
    era = 1.0 + GP_ERA_STEP * era_index(person.era_name)
    return Valuation(
        0.0,
        GP_UNIT_VALUE * class_weight * era,
        f"great person: class weight {class_weight:g}, era x{era:g}",
        payback=GP_PAYBACK_TURNS,
    )


def pick_target(
    items: list[Priced],
    spare: float,
    income: float,
    turn: int,
    last_turn: int,
) -> tuple[Priced, float] | None:
    """The unaffordable item worth saving for, and the turns to reach it, or None.

    Best ROI at the turn it would be reached; its wait must be inside `MAX_WAIT_FRACTION` of the
    turns left, and its ROI must clear the floor. No income means no saving.
    """
    if income <= 0:
        return None
    left = last_turn - turn
    best: tuple[float, Priced, float] | None = None
    for item in items:
        gap = item.cost - spare
        if gap <= 0 or item.valuation.always:
            continue
        wait = gap / income
        if wait > MAX_WAIT_FRACTION * left:
            continue
        later = replace(item, value=item.valuation.at(turn, last_turn, wait))
        if later.value <= 0:
            continue
        if later.roi < floor(spare, income):
            continue
        key = (later.roi, -item.cost)
        if best is None or key > (best[0], -best[1].cost):
            best = (later.roi, later, wait)
    return None if best is None else (best[1], best[2])


# Tile purchases (issue 87). The owner's rules from the first observed human game: buy a tile that
# supplies a yield the city lacks (food for a city without a good food source, with or without a
# resource), buy a strategic or luxury resource tile that can be improved and yields well, buy a
# tile that completes a boost (a per-tile value handed in by the boost work, issue 322), and
# sometimes buy to secure valuable land before a rival or a city-state's borders take it.
# The purchasable-tile read gives terrain, resource and class but no yields, so the yields are an
# estimate from the terrain name (`_TERRAIN_YIELDS`) and the resource class.
TILE_FOOD_LOW = 2.0  # A city making less food surplus than this lacks a food source.
TILE_PRODUCTION_LOW = 3.0  # ... and one making less production lacks a production source.
TILE_LACK_MULT = 2.0  # A yield the city lacks counts this many times over.
TILE_PLAIN_SHARE = (
    0.25  # A tile giving only yields the city has is worth this share (it has others).
)
TILE_FOOD_STRONG = 2  # Food a tile needs to fix a lack (a 1 food tile does not).
TILE_RESOURCE_BONUS = {"bonus": (1, 0, 0), "strategic": (0, 1, 0), "luxury": (0, 0, 2)}
TILE_AMENITY_RATE = 1.5  # A luxury also adds an amenity to the empire, per turn.
TILE_STRATEGIC_RATE = 1.0  # A strategic's own value per turn once improved (units, upgrades).
TILE_CONTESTED_RATE = 0.4  # Per turn for land a rival or city-state border would take; lower
# than the yield rules on purpose: owner says this is worth it only sometimes.
TILE_CONTESTED_MIN = 1.0  # Weighted base yield a contested tile needs to count as valuable.

# (food, production, gold) by terrain name fragment; the first fragment found wins.
_TERRAIN_YIELDS = (
    ("MOUNTAIN", (0, 0, 0)),
    ("SNOW", (0, 0, 0)),
    ("DESERT", (0, 0, 0)),
    ("OCEAN", (1, 0, 0)),
    ("COAST", (1, 0, 1)),
    ("TUNDRA", (1, 0, 0)),
    ("GRASS_HILLS", (2, 1, 0)),
    ("PLAINS_HILLS", (1, 2, 0)),
    ("HILLS", (0, 2, 0)),
    ("GRASS", (2, 0, 0)),
    ("PLAINS", (1, 1, 0)),
)


# The tech each resource's improvement needs, for the common ancient and classical resources.
# APPROXIMATE: built by hand (the game's tables are readable only in Lua), so a resource missing
# here, or a tech name wrong, reads as not improvable, the safe way to be wrong.
_FARMS = ("WHEAT", "RICE", "MAIZE")  # Farm: Pottery.
_PLANTATIONS = ("BANANAS", "CITRUS", "COCOA", "COFFEE", "COTTON", "DYES", "INCENSE", "SILK")
_PLANTATIONS += ("SPICES", "SUGAR", "TEA", "TOBACCO", "WINE")  # Plantation: Irrigation.
_PASTURES = ("CATTLE", "SHEEP", "HORSES")  # Pasture: Animal Husbandry.
_MINES = ("IRON", "COPPER", "SALT", "JADE", "SILVER", "DIAMONDS", "AMBER", "MERCURY")  # Mine.
_QUARRIES = ("STONE", "MARBLE", "GYPSUM")  # Quarry: Mining.
# Left out as unsure of the improvement's tech (so never improvable): camp resources (deer, furs,
# truffles, ivory), niter, coal, oil, aluminum, uranium.
RESOURCE_TECH: dict[str, str] = {
    **dict.fromkeys(_FARMS, "POTTERY"),
    **dict.fromkeys(_PLANTATIONS, "IRRIGATION"),
    **dict.fromkeys(_PASTURES, "ANIMAL_HUSBANDRY"),
    **dict.fromkeys(_MINES + _QUARRIES, "MINING"),
    **dict.fromkeys(("FISH", "CRABS", "WHALES", "PEARLS"), "SAILING"),
}


def tech_name(item_type: str) -> str:
    """`TECH_MINING` or `TECHNOLOGY_MINING` as `MINING`."""
    return item_type.upper().removeprefix("TECHNOLOGY_").removeprefix("TECH_")


def resource_improvable(
    resource: str | None, open_techs: frozenset[str] | None, has_builder: bool
) -> bool:
    """Can the empire improve this resource now? Needs a Builder and the improvement's tech.

    `open_techs` (bare names, see `tech_name`) are the techs still to research (available or
    locked: the read lists techs two eras ahead, and every tech in `RESOURCE_TECH` is earlier
    than that), so a table tech missing from it is done. None (the research read failed) or an
    unknown resource gives False.
    """
    if not has_builder or open_techs is None or not resource:
        return False
    tech = RESOURCE_TECH.get(resource.upper().removeprefix("RESOURCE_"))
    return tech is not None and tech not in open_techs


def tile_improvable(
    resource: str | None,
    open_techs: frozenset[str] | None,
    has_builder: bool,
    occupied: bool = False,
    reserved: bool = False,
) -> bool:
    """`resource_improvable`, and the tile is also free to improve: nothing already stands on it
    (an improvement, pillaged or not, or a district; `occupied`) and no district or wonder of
    ours is planned there (`reserved`, a tile in the placement plan book), which a farm or mine
    would only be built over."""
    if occupied or reserved:
        return False
    return resource_improvable(resource, open_techs, has_builder)


# Contested land (issue 338). A purchasable tile near a city that is not ours is one the other
# border may take first. Borders grow with the city's culture, to about 5 tiles over a game, so
# the radius is a middle value: near enough that growth plausibly reaches the tile soon.
CONTESTED_RADIUS = 4  # Tiles from a foreign city (rival or city-state) that count as near.
CONTESTED_MIN_YIELD = 2  # Estimated food + production + gold a resource-less tile needs.


def is_contested(
    x: int,
    y: int,
    terrain: str,
    resource: str | None,
    foreign_cities: Iterable[tuple[int, int]],
    radius: int = CONTESTED_RADIUS,
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> bool:
    """Is this purchasable tile near a foreign city and worth taking before they do?

    Worth taking: it has a resource, or its estimated yields (from the terrain name) total at
    least `CONTESTED_MIN_YIELD`. A tile on a foreign city itself never appears (the game does
    not offer it), but distance 0 still counts as near.
    """
    if not resource and sum(estimate_tile_yields(terrain)) < CONTESTED_MIN_YIELD:
        return False
    return any(hex_distance(x, y, fx, fy, map_width) <= radius for fx, fy in foreign_cities)


@dataclass(frozen=True)
class TileFacts:
    """A purchasable tile as the valuation sees it. `improvable` is whether the resource on it
    can be improved now (a tech or builder gap makes it false); `contested` is set when a rival
    or a city-state border could take it; `boost_value` is the one-off value of a boost it
    completes (issue 322), zero until that work supplies it."""

    x: int
    y: int
    cost: int
    terrain: str = ""
    resource: str | None = None
    resource_class: str | None = None
    improvable: bool = False  # Conservative: only `resource_improvable` says otherwise.
    contested: bool = False
    boost_value: float = 0.0


@dataclass(frozen=True)
class TileValue:
    """What a tile is worth: a valuation comparable with `value_option`'s, a priority (lower is
    better: 0 boost, 1 lacking yield, 2 resource, 3 contested, 4 other) and the reason."""

    valuation: Valuation
    priority: int
    reason: str


def estimate_tile_yields(terrain: str) -> tuple[int, int, int]:
    """(food, production, gold) guessed from the terrain name; (0, 0, 0) when unknown."""
    text = (terrain or "").upper()
    for fragment, yields in _TERRAIN_YIELDS:
        if fragment in text:
            return yields
    return (0, 0, 0)


def tile_facts(
    tile: Any,
    open_techs: frozenset[str] | None,
    has_builder: bool,
    occupied: bool = False,
    reserved: bool = False,
    foreign_cities: Iterable[tuple[int, int]] = (),
    contested: bool = False,
    boost_value: float = 0.0,
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> TileFacts:
    """The valuation's view of a purchasable tile (civ6-mcp's `PurchasableTile`). The policy and
    `scripts/tile_report.py` both build facts here, so the report shows what the policy sees.
    `contested` forces the flag on (a hand-marked tile); otherwise it is read off the cities."""
    return TileFacts(
        tile.x,
        tile.y,
        tile.cost,
        tile.terrain,
        tile.resource,
        tile.resource_class,
        improvable=tile_improvable(tile.resource, open_techs, has_builder, occupied, reserved),
        contested=contested
        or is_contested(
            tile.x, tile.y, tile.terrain, tile.resource, foreign_cities, map_width=map_width
        ),
        boost_value=boost_value,
    )


def value_tile_purchase(
    tile: TileFacts, city: City, weights: ProductionWeights
) -> TileValue | None:
    """What buying this tile for `city` is worth per turn (see the module's tile rules), or None."""
    food, prod, gold = estimate_tile_yields(tile.terrain)
    reasons: list[str] = []
    priority = 4  # No rule applies yet.
    weighted = food * weights.food + prod * weights.production + gold * weights.gold
    lacks_food = city.food_surplus < TILE_FOOD_LOW and food >= TILE_FOOD_STRONG
    lacks_prod = city.production < TILE_PRODUCTION_LOW and prod >= 1
    rate = weighted * TILE_PLAIN_SHARE
    if lacks_food:
        rate += food * weights.food * (TILE_LACK_MULT - TILE_PLAIN_SHARE)
        reasons.append(f"city lacks food (surplus {city.food_surplus:.1f}), tile gives {food}")
        priority = 1
    if lacks_prod:
        rate += prod * weights.production * (TILE_LACK_MULT - TILE_PLAIN_SHARE)
        reasons.append(f"city lacks production ({city.production:.1f}), tile gives {prod}")
        priority = min(priority, 1)
    cls = tile.resource_class
    if tile.resource and cls in TILE_RESOURCE_BONUS and tile.improvable:
        bf, bp, bg = TILE_RESOURCE_BONUS[cls]
        gain = bf * weights.food + bp * weights.production + bg * weights.gold
        gain += {"luxury": TILE_AMENITY_RATE, "strategic": TILE_STRATEGIC_RATE}.get(cls, 0.0)
        # A resource tile is worked at once, so it counts in full, not at the plain share.
        rate += weighted * (1.0 - TILE_PLAIN_SHARE) + gain
        reasons.append(f"{cls} resource {tile.resource} can be improved")
        priority = min(priority, 2)
    elif tile.resource:
        reasons.append(f"{cls or 'resource'} {tile.resource} cannot be improved yet")
    if tile.contested and (tile.resource or weighted >= TILE_CONTESTED_MIN):
        rate += TILE_CONTESTED_RATE
        reasons.append("contested: a rival or city-state border could take it")
        priority = min(priority, 3)
    fixed = max(tile.boost_value, 0.0)
    if fixed > 0:
        reasons.append(f"completes a boost worth {fixed:.0f}")
        priority = 0
    if priority == 4 or (rate <= 0 and fixed <= 0):
        return None  # None of the owner's rules applies: a plain tile is not worth gold.
    reason = "; ".join(reasons)
    return TileValue(Valuation(rate, fixed, f"tile: {reason}"), priority, reason)
