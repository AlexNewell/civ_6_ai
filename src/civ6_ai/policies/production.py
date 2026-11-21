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
from dataclasses import dataclass, field, fields, replace
from typing import Any, Protocol

from civ6_ai.blockers import Blocker, BlockerGame, BlockerType
from civ6_ai.city_tiles import REFRESH_TURNS, CityTiles, read_city_tiles
from civ6_ai.construction import ProductionRefused, ProductionSet
from civ6_ai.era import EraSettings, district_nudge, outlook
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
from civ6_ai.policies.expansion import ExpansionState, expected_cities, settler_cap
from civ6_ai.policies.game_data import (
    COMBAT_UNITS,
    REPLACES,
    UNIQUE_DISTRICTS,
    UPGRADES,
    WONDERS,
)
from civ6_ai.policies.growth import (
    CityGrowth,
    GrowthPolicy,
    Stance,
    food_multiplier,
    housing_multiplier,
)
from civ6_ai.policies.growth_caps import (
    at_housing_cap,
    decide_cap_focus,
    hold_cap_focus,
    settler_delay_note,
    yield_needs,
)
from civ6_ai.policies.military import MilitaryWeights, camp_matchup
from civ6_ai.replacements import READ_TRIES, apply_replacements, read_replacements
from civ6_ai.state import City, Empire, ProductionChoice, Unit
from civ6_ai.state import read_cities as _read_cities
from civ6_ai.state import read_empire as _read_empire
from civ6_ai.state import read_production as _read_production
from civ6_ai.state import read_units as _read_units
from civ6_ai.state.reader import hex_distance
from civ6_ai.strategy.wonders import estimate_race

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
_FIRST_SETTLER_TURNS = 45  # No builders before the first settler until this turn.
_CAMP_UNIT_SCORE = 17.0  # A unit for a camp force, while one is short (issue 268).
_CAMP_ODDS_CAP = 3.0  # Odds above this against a camp's defender are no better a pick.
_CAMP_COST_BASE = 60.0  # An Archer's cost: the unit the matchup per cost is scaled by.
_ARMY_MATCHUP_SCORE = 3.0  # The most the matchup adds to an army-floor unit's score (279).
_CAMP_MATCHUP_SCORE = 8.0  # The most the matchup adds to a camp unit's score.
_MATCHUP_WEIGHTS = MilitaryWeights()
_CAMP_CITY_REACH = 14  # Matches `MilitarySettings.camp_reach`.
_EARLY_TURNS = 60  # Turns in which a city with no garrison should get one first.
# Issue 301: the newest run queued 4 Scouts by turn 60 (a lost scout reset `recon` to 0 and the
# 15.0 score came back). Two cover the map; past that the early turns go to growth.
_MAX_EARLY_SCOUTS = 2


@dataclass(frozen=True)
class ProductionWeights:
    """How much each kind of build is worth to the empire. Zero turns a kind off.

    Yields scale the buildings and districts that produce them. The rest scale a whole kind of
    unit or project. Threat handling is not weighted, because losing a city beats any plan.
    """

    food: float = 1.0
    production: float = 1.0
    gold: float = 1.0
    science: float = 1.25  # Above 1: science compounds, and a science race is lost late.
    culture: float = 1.0
    faith: float = 0.5
    military: float = 1.0  # Land units beyond the garrison a city needs.
    expansion: float = 1.0  # Settlers.
    builders: float = 1.0  # Builders, which improve tiles.
    trade: float = 1.0  # Traders, while the empire has route capacity no trader fills.
    wonders: float = 0.5
    # The ceiling on cities: settlers stop here. Below it the cap follows the schedule and the safe
    # sites (see `settler_cap`), so it is no longer a fixed small number (issue 316).
    target_cities: int = 12
    victory: str = "any"  # The policy document's victory target. Decides the special districts.
    # The army floor: combat units kept per city. Below it, military units score above an ordinary
    # building even when no city is short; at it, military stops scoring.
    army_per_city: float = 1.5
    # Issue 275: while a camp's force is short, at most this share of the cities (rounded down, at
    # least one) build units for it, so districts and settlers keep running in the rest.
    camp_share: float = 0.5
    # Turns a one-city empire holds back builders to put the first settler first. 0 turns it off.
    builder_hold_turns: int = _FIRST_SETTLER_TURNS
    # Issue 299: score of a Monument in a city with no culture building and no threat, in place of
    # its yield value. The bot's first Monument came on turn 74 to 80 (3 civics at turn 50 against
    # a human's 9), because a second Warrior or Scout always outscored it. The default sits above
    # an army-floor unit (8 to 11) and below an ordinary settler (14), so a ready settler still
    # comes first. 0 turns it off.
    early_monument: float = 12.0
    # Issue 323: a wonder is scored only when the race estimator (`strategy.wonders`) says go, and
    # only in a safe, tall-leaning city. Off: the old flat wonder score, unchanged.
    wonder_races: bool = False
    # Issue 357: a threat elsewhere stops a city's wonder race only within this many tiles of it.
    # A far city's raid does not need this city's production, and an empire-wide gate meant the
    # barbarians or a settler somewhere stopped every wonder (none started live over 30 turns).
    wonder_threat_reach: int = 10
    # Issue 369: a camp force's units no longer outbid a settler this city can build while the camp
    # does not block expansion. Live (STD_T1_04, T24 to T29) four Slingers and two Warriors beat a
    # settler worth about 10 after the build-time discount, and the 3rd city came 11 turns late.
    # The camp unit is held just below the settler; a threatened city and a camp that blocks
    # settling (issue 284) are unchanged. False restores the old order, for a posture that wants
    # the camp cleared first.
    settler_before_camp: bool = True
    # Issue 311: while the era outlook says push (about 10 turns before the era ends, the score
    # within reach of a Dark or Golden breakpoint), the first district of a kind earns a Historic
    # Moment. This adds to its score, about one ordinary building. The district would be built
    # sooner or later; the nudge only moves it up. 0 turns it off.
    era_district_bonus: float = 3.0

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
    # Issue 301: Scouts this policy has queued over the game so far, kept by the policy because
    # `recon` forgets a scout the moment it dies. Zero when not known (a test, a fresh process).
    scouts_built: int = 0
    army: int = 0
    traders: int = 0  # Traders that exist, on a route or not, plus those being built.
    trade_capacity: int = 0  # Routes the empire can run at once.
    trade_safe: bool = True  # False while every route is too dangerous (issue 269): no Trader.
    districts: frozenset[str] = frozenset()  # Short names of districts any city has built.
    science_districts: int = 0  # Campuses (or the unique district in their place) built or begun.
    threatened: frozenset[int] = frozenset()  # Ids of cities the threat state marks (1.35).
    # Issue 357: where the threatened cities are, so a far threat can be told from a near one. An
    # id missing here has an unknown position and counts as near.
    threatened_at: Mapping[int, tuple[int, int]] = field(default_factory=dict, compare=False)
    short: frozenset[int] = frozenset()  # Of those, the ones with fewer than two defenders.
    mapped: frozenset[int] = frozenset()  # Of those, the ones the threat map judged (issue 264).
    wary: bool = False  # A much stronger rival at peace with us (`defence.wary_of`): walls.
    # Issue 268: combat units still to build to clear the camp the military policy has chosen,
    # and the kind most missing ("melee", "ranged" or "any"). Each army pick counts one off.
    camp_units: int = 0
    camp_wants: str = ""
    camp_target: tuple[int, int] | None = None  # The camp, so only a city in reach builds.
    # The strongest defender seen at the camp (melee and ranged strength), for the matchup; and
    # how many cities are already building a camp unit, for the share cap (issue 275).
    camp_defender: float = 0.0
    camp_defender_ranged: float = 0.0
    camp_building: int = 0
    # Issue 279: the strongest known hostile land unit near us (a camp's defender or a roaming
    # barbarian): melee and ranged strength. The army floor picks units by their match against it.
    hostile: float = 0.0
    hostile_ranged: float = 0.0
    sites_ok: bool = True  # False when settling found no safe site to send a settler to.
    # Issue 283: settlers on the map that settling held this turn (no site, or no safe route and
    # no escort). While one waits, another settler could do no more than it.
    held_settlers: int = 0
    idle_builders: int = 0  # Builders that found no job on offer the last time they ran (1.45).
    spare_jobs: int | None = None  # Jobs no builder holds; None when not known this turn.
    # Each city's growth plan (`growth.py`). Empty: food and housing items score the old way.
    growth: Mapping[int, CityGrowth] = field(default_factory=dict, compare=False)
    # Issue 323, read only with `wonder_races`: the difficulty name ("" when unread, which the
    # estimator treats with care), the rivals who could build a wonder, the progress fraction of
    # each rival seen building a given wonder, and what a district is worth here instead.
    difficulty: str = ""
    rivals: int = 0
    rival_progress: Mapping[str, tuple[float, ...]] = field(default_factory=dict, compare=False)
    wonder_alternative: float = 5.0
    era_push: bool = False  # Issue 311: the era outlook says to favour cheap Historic Moments.
    # Where `rivals` came from, for the log: "rankings" (living majors but us, as last read) or
    # "default" (no rankings read yet). Not a decision input.
    rivals_source: str = field(default="", compare=False)

    @property
    def camp_blocks(self) -> bool:
        """A camp force is short and expansion is stuck: a settler is held, or no site is safe.

        Issue 284: iteration 3 queued 12 settlers in 50 turns while the camp's barbarians held
        every settler back and the camp force was never built. Clearing the camp is then the
        expansion move, so the force comes before the settler and the share cap is lifted.
        """
        return self.camp_units > 0 and (self.held_settlers > 0 or not self.sites_ok)

    def after_choosing(
        self, item: str, unknown_combat: bool = False, orders: bool = True
    ) -> "ProductionContext":
        """Count a pick, so two cities don't both build the one builder the empire needs.

        A builder ordered (`orders`) also uses up one unclaimed job. Builders that already exist
        don't: the job count was taken after they chose.
        """
        kinds = _counted_as(item) or (("army",) if unknown_combat else ())
        counted = replace(self, **{kind: getattr(self, kind) + 1 for kind in kinds})
        stats = COMBAT_UNITS.get(item)
        # A ranged unit cannot take the camp tile, so it does not meet a demand for a melee one.
        fills = not (self.camp_wants == "melee" and stats is not None and stats[4] == "RANGED")
        if "army" in kinds and counted.camp_units > 0 and fills:
            counted = replace(
                counted,
                camp_units=counted.camp_units - 1,
                camp_building=counted.camp_building + 1,
            )
        if orders and "builders" in kinds and counted.spare_jobs is not None:
            counted = replace(counted, spare_jobs=max(0, counted.spare_jobs - 1))
        if item.startswith("DISTRICT_"):  # A district being built counts, so two cities don't.
            counted = replace(counted, districts=counted.districts | {_short(item)})
            if _base_district(item) == "CAMPUS":
                counted = replace(counted, science_districts=counted.science_districts + 1)
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
    trade_safe: bool = True,
) -> ProductionContext:
    """Count what exists plus what cities are already building, so picks don't double up.

    `defence` is the threat state (`defence.DefenceState`), or None.
    """
    plan = getattr(defence, "camp_plan", None)
    ctx = ProductionContext(
        turn=empire.turn,
        num_cities=empire.num_cities,
        trade_capacity=empire.trade_capacity,
        trade_safe=trade_safe,
        threatened=defence.threatened_ids if defence is not None else frozenset(),
        threatened_at={c.city_id: (c.x, c.y) for c in cities},
        short=defence.short_ids if defence is not None else frozenset(),
        mapped=defence.mapped_ids if defence is not None else frozenset(),
        wary=bool(defence is not None and defence.wary),
        districts=frozenset(_short(d.district_type) for c in cities for d in c.districts),
        science_districts=sum(
            _base_district(d.district_type) == "CAMPUS" for c in cities for d in c.districts
        ),
        idle_builders=idle_builders,
        spare_jobs=spare_jobs,
    )
    for unit in units:
        ctx = ctx.after_choosing(
            unit.unit_type,
            unknown_combat=unit.is_combat and unit.unit_type not in COMBAT_UNITS,
            orders=False,
        )
    # The plan's count is of units to add to a force it already sized from the units that exist,
    # so it is set after those are counted; only builds under way count off.
    if plan is not None:
        ctx = replace(
            ctx,
            camp_units=plan.units_wanted,
            camp_wants=plan.wants,
            camp_target=plan.target,
            camp_defender=getattr(plan, "defender", 0.0),
            camp_defender_ranged=getattr(plan, "defender_ranged", 0.0),
        )
    near = getattr(defence, "hostile", (0.0, 0.0))
    ctx = replace(
        ctx,
        hostile=max(near[0], ctx.camp_defender),
        hostile_ranged=max(near[1], ctx.camp_defender_ranged),
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
    "DRONE",  # Run 13: Coba built two while at war; they cannot fortify or hold a city.
    "BALLOON",
    "SUPPLY",
    "SIEGE_TOWER",
)
_ARMY_FLOOR_SCORE = 8.0  # Below the army floor: above a typical building (7 to 8), under defence.
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
_WARY_WALLS_SCORE = 12.0
_TRAINING_BUILDINGS = frozenset({"BARRACKS", "STABLE", "ARMORY", "MILITARY_ACADEMY"})
_WONDER_VALUE = 8.0
_MONUMENT_COST = 30.0  # Production the city still owes its first culture building (issue 323).
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


def _base_district(item: str) -> str:
    """The district without its prefix, a unique district as the base district it replaces."""
    short = _short(item)
    return UNIQUE_DISTRICTS.get(short, short)


# A city without a science district is worth more the further the empire is from having one in
# every city, up to this much on top of the district's own value. It only raises the score, so
# a threatened city, a defender or a nearly finished build still wins, and wonders and the other
# districts still get their turn once most cities have one.
_SCIENCE_CATCHUP = 2.5


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
    a repair is a restored item, not a new one. Returns (choices, repairs).
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
            # A building repair has no tile (the game offers it with no coordinates, issue 280);
            # a district repair names its own.
            if option.category == "BUILDING" or None not in (option.repair_x, option.repair_y):
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
    if city.city_id in ctx.mapped:
        return False  # The map weighed what is near against what holds the city, and it holds.
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


_HOUSING_ITEMS = frozenset({"SEWER", "NEIGHBORHOOD"})  # Their food profile stands for housing.


def _deficit_tolerated(city: City, ctx: ProductionContext) -> bool:
    plan = ctx.growth.get(city.city_id)
    return plan is not None and plan.stance is Stance.SHRINK_OK


def _growth_adjusted(
    value: float,
    name: str,
    profile: tuple[float, ...],
    city: City,
    ctx: ProductionContext,
    w: ProductionWeights,
) -> float:
    """Rescale the food part of a building by the city's growth plan. Other yields are untouched.

    A food building is worth what the gap to the city's target makes it, a housing building what
    the housing cap costs, and neither once the city has reached its target size.
    """
    plan = ctx.growth.get(city.city_id)
    if plan is None or profile[0] <= 0:
        return value
    if name in _HOUSING_ITEMS:
        factor = housing_multiplier(plan)
    elif name in _FOOD_BUILDINGS or name == "GRANARY":
        factor = food_multiplier(plan)
    else:
        return value
    food_part = profile[0] * w.food
    return value + (factor - 1.0) * food_part


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
        cap = settler_cap(ctx.turn, ctx.num_cities, ctx.sites_ok, w.target_cities)
        in_flight_cap = 2 if boosted and ctx.num_cities + 2 <= cap else 1
        if (
            threatened
            or is_starving(city)
            or city.pop < _SETTLER_MIN_POP
            or ctx.num_cities + ctx.settlers >= cap
            or ctx.settlers >= in_flight_cap
            # Issue 283: a settler already waits with nowhere to go, so another adds nothing.
            or ctx.held_settlers > 0
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
        # Run 15 (one start, one seed): a builder before the first settler delayed the second city.
        # A default, not a rule: it holds only while a settler is really wanted (a site is known,
        # expansion is weighted, more than one city is the goal), and `builder_hold_turns` = 0 in
        # a policy document turns it off, for a tall start or a map with nowhere to settle.
        if (
            ctx.num_cities <= 1
            and ctx.turn <= w.builder_hold_turns
            and ctx.sites_ok
            and w.expansion > 0
            and w.target_cities > 1
        ):
            return 0.0
        return (6.0 + 2.0 * min(wanted, 3) + len(city.unimproved_resources)) * w.builders
    if kind == "UNIT_TRADER":
        # Every route slot needs a trader. Traders already on a route count, since they fill a slot.
        wanted = ctx.trade_capacity - ctx.traders
        if wanted <= 0 or threatened or not ctx.trade_safe:
            return 0.0
        return (7.0 + min(wanted, 3)) * w.trade
    if kind == "UNIT_SCOUT":
        # With nothing exploring, the settling policy sees no sites (live: a settler sat idle from
        # turn 30 on a 30-tile map), so the first scout outranks a settler.
        # Issue 301: a lost scout is replaced once, not forever.
        if ctx.recon == 0 and ctx.turn <= _EARLY_TURNS and ctx.scouts_built < _MAX_EARLY_SCOUTS:
            return 15.0
        return 0.0
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
    floor = 0.0
    if wanted > 0:
        # Below the floor (issue 267) a unit outranks an ordinary building, so the floor is reached
        # even when no city is short: a threat that arrives finds something to fight with.
        # Issue 279: by the unit's match against the strongest hostile unit known, per cost,
        # once one is known (iteration 2 kept picking Heavy Chariots, which lose to a
        # Man-at-Arms); by strength before.
        match = hostile_unit_value(item, ctx)
        pick = strength / 20.0 if match is None else _ARMY_MATCHUP_SCORE * match
        floor = (_ARMY_FLOOR_SCORE + min(wanted, 4.0)) * w.military + pick
    return max(floor, _camp_score(city, item, stats[4], strength, ctx, w))


def camp_share_cap(num_cities: int, share: float) -> int:
    """Cities that may build camp units at once: the share of them, rounded down, at least one."""
    return max(1, int(num_cities * share))


def hostile_unit_value(item: str, ctx: ProductionContext) -> float | None:
    """A unit's worth against the strongest known hostile unit, per production cost, or None
    when none is known or the unit cannot fight (issue 279).

    The odds are continuous (a melee unit that loses scores low, not 0), capped at 3 to 1 and set
    against a unit costing 60, so a cheap unit that does as well beats a dear one.
    """
    if ctx.hostile <= 0:
        return None
    odds = camp_matchup(item, ctx.hostile, ctx.hostile_ranged, _MATCHUP_WEIGHTS, "melee")
    if odds is None:
        return None
    cost = float(COMBAT_UNITS[item][3])
    return min(odds, _CAMP_ODDS_CAP) / _CAMP_ODDS_CAP * min(1.5, _CAMP_COST_BASE / max(cost, 1.0))


def camp_unit_value(
    item: str, unit_class: str, cost: float, ctx: ProductionContext
) -> float | None:
    """What a unit is worth to the camp force: its odds against the strongest known defender, per
    production cost (issue 275), or None when no defender has been seen (then strength decides).

    The odds are `expected_damage` both ways (`military.camp_matchup`), capped at 3 to 1 and set
    against a unit costing 60. A melee unit that loses one-on-one is worth 0, unless the force
    lacks one to take the tile.
    """
    if ctx.camp_defender <= 0:
        return None
    odds = camp_matchup(
        item, ctx.camp_defender, ctx.camp_defender_ranged, _MATCHUP_WEIGHTS, ctx.camp_wants
    )
    if odds is None:
        return None
    if ctx.camp_wants == "melee" and COMBAT_UNITS[item][1] == 0:
        odds = _CAMP_ODDS_CAP  # The tile needs a unit standing on it, not one that wins the duel.
    if odds <= 0:
        return 0.0
    return min(odds, _CAMP_ODDS_CAP) / _CAMP_ODDS_CAP * min(1.5, _CAMP_COST_BASE / max(cost, 1.0))


def _camp_score(
    city: City,
    item: str,
    unit_class: str,
    strength: float,
    ctx: ProductionContext,
    w: ProductionWeights,
) -> float:
    """A unit for the force that clears a nearby camp (issue 268), while one is still short.

    Above an ordinary building (the camp keeps spawning units and blocks settling), below a
    defender for a threatened city. The kind the force lacks gets a little more, and a kind it has
    enough of a little less, so the build is a melee unit to take the tile or a ranged one to
    soften a strong defender, whichever is missing.
    """
    if ctx.camp_units <= 0 or w.military <= 0:
        return 0.0
    # A city further than the force is called from (`camp_reach`) builds no unit for it: the unit
    # would never join the force.
    if ctx.camp_target is not None and (
        hex_distance(city.x, city.y, *ctx.camp_target) > _CAMP_CITY_REACH
    ):
        return 0.0
    # Only a share of the cities builds for the camp, so the rest keep their districts and
    # settlers; a city already building a camp unit is counted in `camp_building`.
    # Issue 284: not while the camp is what holds the settlers back; then every city in reach
    # builds for it, and the settlers wait for the force.
    cap = ctx.num_cities if ctx.camp_blocks else camp_share_cap(ctx.num_cities, w.camp_share)
    if ctx.camp_building >= cap:
        return 0.0
    ranged = unit_class == "RANGED"
    lift = 0.0
    if ctx.camp_wants == "melee":
        if ranged:
            return 0.0  # Only a melee unit takes the camp tile; the force's missing piece.
        lift = 2.0
    elif ctx.camp_wants == "ranged":
        lift = 2.0 if ranged else -1.0
    worth = camp_unit_value(item, unit_class, float(COMBAT_UNITS[item][3]), ctx)
    if worth is not None:
        if worth <= 0:
            return 0.0  # It loses to the defender one-on-one, and the force does not need it.
        return (_CAMP_UNIT_SCORE + lift + _CAMP_MATCHUP_SCORE * worth) * w.military
    return (_CAMP_UNIT_SCORE + lift + strength / 10.0) * w.military


def wants_early_monument(city: City, ctx: ProductionContext, w: ProductionWeights) -> bool:
    """No Monument yet and no threat: the city builds its culture first (issue 299)."""
    if w.early_monument <= 0 or "MONUMENT" in city.buildings:
        return False
    return not under_threat(city, ctx)


_LOGGED_RACES: set[tuple[str, str, int, float]] = set()


def not_tall_and_safe_reason(city: City, ctx: ProductionContext, w: ProductionWeights) -> str:
    """Why this city does not race a wonder (issue 323, per city since issue 357), or "".

    The city itself must be safe and not building a settler. A threat elsewhere counts only
    within `wonder_threat_reach` tiles (a threatened city of unknown position counts as near).
    Camp units block only a city the camp force is called from (`_CAMP_CITY_REACH`), the same
    cities `_camp_score` asks for units; a camp far away leaves this city's production free. A
    settler being walked or built in another city does not block: only this city's own settler
    build takes its production.
    """
    if under_threat(city, ctx):
        return "this city is threatened"
    if city.building == "UNIT_SETTLER":
        return "this city is building a settler"
    for other in ctx.threatened:
        at = ctx.threatened_at.get(other)
        if at is None or hex_distance(city.x, city.y, *at) <= w.wonder_threat_reach:
            return "a threatened city is near"
    if ctx.camp_units > 0 and (
        ctx.camp_target is None
        or hex_distance(city.x, city.y, *ctx.camp_target) <= _CAMP_CITY_REACH
    ):
        return "a camp force is short nearby"
    return ""


def tall_and_safe(city: City, ctx: ProductionContext, w: ProductionWeights | None = None) -> bool:
    """Only a safe city races a wonder: see `not_tall_and_safe_reason`."""
    return not not_tall_and_safe_reason(city, ctx, w or ProductionWeights())


# Rivals assumed when the world rankings have not been read yet (the first turns, or a failed
# read). A standard game has about five other majors; the guess leans low but not zero, since
# zero rivals would make every race a certain win.
DEFAULT_RIVALS = 3
RIVALS_FROM_RANKINGS = "rankings"
RIVALS_DEFAULT = "default"


def rivals_from_rankings(rankings: Any) -> tuple[int, str]:
    """The rivals who could build a wonder, and where the count came from.

    Every living major civ but us can, met or not (the AI builds wonders on its own), so this is
    the rankings' living majors minus us. `rankings` is a `RankingsHolder` or None; its newest
    reading is used even from an earlier turn, because the count changes rarely and the policy
    runs before the rankings handler each turn. An empty or missing reading gives the default.
    """
    last = getattr(rankings, "last", None)
    if last is None or not getattr(last, "civs", ()):
        return DEFAULT_RIVALS, RIVALS_DEFAULT
    return len(last.rivals), RIVALS_FROM_RANKINGS


def wonder_alternative_value(
    city: City,
    options: Iterable[ProductionChoice],
    ctx: ProductionContext,
    w: ProductionWeights,
    fallback: float,
    unavailable: frozenset[str] = frozenset(),
) -> float:
    """What the best non-wonder district or building is worth in this city, before the discount
    for turns (the estimator's wonder value is undiscounted too). `fallback` when none is on
    offer or none scores above zero."""
    best = 0.0
    for o in options:
        if o.category not in ("DISTRICT", "BUILDING") or o.item in WONDERS or o.is_repair:
            continue
        if o.item in unavailable:
            continue  # Ruled out by the game or a refusal: it is no alternative.
        best = max(best, _raw_value(o, city, ctx, w))
    return best if best > 0 else fallback


def race_context(
    city: City,
    keep: Iterable[ProductionChoice],
    context: ProductionContext,
    weights: ProductionWeights,
    unavailable: frozenset[str] = frozenset(),
) -> ProductionContext:
    """The context with this city's wonder alternative filled in (issue 328), or the same one.

    Ranking, the log line and the wonder guard in `choose_for_city` all score with this one, so
    a wonder the ranking approved is not rejected by a guard using the constant. Off, or with no
    wonder on offer, nothing is computed and the context is returned unchanged.
    """
    keep = list(keep)
    if not weights.wonder_races or not any(o.item in WONDERS for o in keep):
        return context
    alt = wonder_alternative_value(
        city, keep, context, weights, context.wonder_alternative, unavailable
    )
    return replace(context, wonder_alternative=alt)


def _wonder_race_score(
    option: ProductionChoice, city: City, ctx: ProductionContext, w: ProductionWeights
) -> float:
    """The flat wonder score when the race estimator says go in a tall, safe city, else 0."""
    est = estimate_race(
        option.item,
        cost=option.cost,
        production=city.production,
        turn=ctx.turn,
        difficulty=ctx.difficulty,
        rivals=ctx.rivals,
        alternative=ctx.wonder_alternative,
        other_work=0.0 if "MONUMENT" in city.buildings else _MONUMENT_COST,
        progress=tuple(ctx.rival_progress.get(option.item, ())),
        already_started=city.building == option.item,
    )
    blocked = not_tall_and_safe_reason(city, ctx, w)
    go = est.go and not blocked
    # The alternative is in the key: a reranked verdict (an item ruled out) is a new line.
    key = (city.name, option.item, ctx.turn, round(ctx.wonder_alternative, 2))
    if key not in _LOGGED_RACES:
        _LOGGED_RACES.add(key)
        reason = f" (not tall and safe: {blocked})" if blocked else ""
        shown = ",".join(f"{p:.2f}" for p in ctx.rival_progress.get(option.item, ())) or "none"
        inputs = (
            f" [inputs: difficulty={ctx.difficulty or 'unread'}, rivals={ctx.rivals}"
            f" ({ctx.rivals_source or 'set'}), rival progress={shown},"
            f" alt={ctx.wonder_alternative:.1f}]"
        )
        log.info("%s%s%s", est.log_line(city.name), reason, inputs)
    return _WONDER_VALUE * w.wonders if go else 0.0


def _building_score(
    option: ProductionChoice, city: City, ctx: ProductionContext, w: ProductionWeights
) -> float:
    name = _short(option.item)
    if name == "MONUMENT" and not option.is_repair and wants_early_monument(city, ctx, w):
        return max(w.early_monument, _yield_value(_BUILDING_YIELDS[name], w))
    if option.item in WONDERS:
        # Needs a tile, which `_start` finds when the game asks for one.
        if under_threat(city, ctx):
            return 0.0
        if w.wonder_races:
            return _wonder_race_score(option, city, ctx, w)
        return _WONDER_VALUE * w.wonders
    if name in _DEFENSIVE_BUILDINGS:
        if city.city_id in ctx.threatened and not city.has_walls:
            return 26.0 + len(city.threats)  # Walls come right after the first defender.
        if is_pressed(city, ctx) and not city.has_walls:
            return 17.0 + len(city.threats)
        if ctx.wary and name == "WALLS" and not city.has_walls:
            # A stronger neighbour can declare any turn (Run 13: Coba fell six turns after the
            # second declaration with no wall). Above a typical building, below a marked city's.
            return _WARY_WALLS_SCORE
        return 0.0  # Unthreatened, walls are an expense with no return.
    if name in _TRAINING_BUILDINGS:
        return 0.5 * w.military
    profile = _BUILDING_YIELDS.get(name)
    if profile is None:
        return _UNKNOWN_BUILDING
    value = _yield_value(profile, w)
    if name == "GRANARY" and city.food_surplus > 3 and city.city_id not in ctx.growth:
        value *= 0.5
    value = _growth_adjusted(value, name, profile, city, ctx, w)
    if name in _FOOD_BUILDINGS and is_starving(city) and not _deficit_tolerated(city, ctx):
        value += _STARVING_FOOD_BOOST * profile[0] * w.food
    return value


def _district_score(
    option: ProductionChoice, city: City, w: ProductionWeights, ctx: ProductionContext
) -> float:
    name = _base_district(option.item)
    if name in BASE_VALUE:  # It passed `wants_district`, so it is worth building.
        return PLANNED_VALUE if is_planned(name, w.victory) else BASE_VALUE[name]
    profile = _DISTRICT_YIELDS.get(name)
    if profile is None:
        return _UNKNOWN_BUILDING * 2.0
    value = _yield_value(profile, w)
    if name == "CAMPUS" and ctx.num_cities > 0:
        behind = max(0.0, 1.0 - ctx.science_districts / ctx.num_cities)
        value += _SCIENCE_CATCHUP * behind * w.science
    plan = ctx.growth.get(city.city_id)
    if name == "AQUEDUCT":
        # Worth building when growth is capped by housing, not before. With a growth plan, by
        # how many citizens the cap costs toward the target.
        if plan is not None:
            value *= housing_multiplier(plan)
        else:
            value = value * 2.0 if city.housing - city.pop <= 1 else value * 0.3
    else:
        value = _growth_adjusted(value, name, profile, city, ctx, w)
    if name in _FOOD_BUILDINGS and is_starving(city) and not _deficit_tolerated(city, ctx):
        value += _STARVING_FOOD_BOOST * profile[0] * w.food
    return value


_REPAIR_GAIN = 2.0  # A restored building pays at once and costs only a turn or two.


def _repair_value(
    option: ProductionChoice, city: City, ctx: ProductionContext, w: ProductionWeights
) -> float:
    """What a repair restores: the yields of the building (or district) it brings back."""
    name = _short(option.item)
    if option.category == "DISTRICT":
        name = _base_district(option.item)
        profile = _DISTRICT_YIELDS.get(name)
        if profile is None:
            return BASE_VALUE.get(name, _UNKNOWN_BUILDING) * _REPAIR_GAIN
    elif name in _DEFENSIVE_BUILDINGS:
        # Walls back are worth what new walls would be, and little to a city nobody threatens.
        return _building_score(option, city, ctx, w) or _UNKNOWN_BUILDING * _REPAIR_GAIN
    else:
        profile = _BUILDING_YIELDS.get(name)
        if profile is None:
            return _UNKNOWN_BUILDING * _REPAIR_GAIN
    return max(_yield_value(profile, w), _UNKNOWN_BUILDING) * _REPAIR_GAIN


def score(
    option: ProductionChoice, city: City, ctx: ProductionContext, weights: ProductionWeights
) -> float:
    """What the option is worth to this city, discounted by how long it takes."""
    value = _raw_value(option, city, ctx, weights)
    if value <= 0:
        return 0.0
    if under_threat(city, ctx) and is_defence(option):
        # A defender that arrives in time matters most, so speed counts for more than usual.
        return value * _URGENT_SCALE / (_URGENT_SCALE + max(option.turns, 1))
    return value * _TURN_SCALE / (_TURN_SCALE + max(option.turns, 1))


def _raw_value(
    option: ProductionChoice, city: City, ctx: ProductionContext, weights: ProductionWeights
) -> float:
    """What the option is worth before the discount for how long it takes."""
    if option.is_repair:
        value = _repair_value(option, city, ctx, weights)
    elif option.category == "UNIT":
        value = _unit_score(option, city, ctx, weights)
    elif option.category == "DISTRICT":
        value = _district_score(option, city, weights, ctx) + district_nudge(
            _short(option.item), ctx.districts, ctx.era_push, weights.era_district_bonus
        )
    else:
        value = _building_score(option, city, ctx, weights)
    return value


def _is_floor_unit(option: ProductionChoice) -> bool:
    """A land combat unit: the kind the army floor scores. Civilian and recon units are not."""
    stats = COMBAT_UNITS.get(option.item)
    return stats is not None and stats[4] != "RECON" and defence_strength(option.item) > 0


def _growth_before_garrison(city: City, ctx: ProductionContext) -> bool:
    """Early, garrisoned, and nothing hostile near: the army floor may yield to growth.

    Left out on purpose, so their rules stay as they are: a city without a garrison (its first
    defender comes first), a threatened or pressed city, any known hostile unit, and a camp force
    that is short (issues 268 and 284).
    """
    return (
        ctx.turn <= _EARLY_TURNS
        and city.garrison_unit is not None
        and not under_threat(city, ctx)
        and not is_pressed(city, ctx)
        and ctx.camp_units <= 0
        and ctx.hostile <= 0
        and ctx.hostile_ranged <= 0
    )


def _settler_before_camp(
    city: City,
    keep: list[ProductionChoice],
    scores: dict[int, float],
    ctx: ProductionContext,
    w: ProductionWeights,
) -> None:
    """Hold a camp unit just below a settler this city can build (issue 369), in place in `scores`.

    Compared on the discounted scores, the ones the ranking uses. Only a unit the camp demand
    itself lifted is held: an army-floor unit keeps its score.
    """
    if (
        not w.settler_before_camp
        or ctx.camp_units <= 0
        or ctx.camp_blocks
        or under_threat(city, ctx)
        or is_pressed(city, ctx)
    ):
        return
    settler = max(
        (
            scores[id(o)]
            for o in keep
            if o.category == "UNIT" and canonical(o.item) == "UNIT_SETTLER"
        ),
        default=0.0,
    )
    if settler <= 0:
        return
    # Only a combat unit in a garrisoned city reaches the camp score in `_unit_score`; a first
    # defender and a Scout keep their own priorities.
    if city.garrison_unit is None:
        return
    held = []
    for o in keep:
        if o.category != "UNIT" or not _is_floor_unit(o) or scores[id(o)] < settler:
            continue
        stats = COMBAT_UNITS[o.item]
        camp = _camp_score(city, o.item, stats[4], defence_strength(o.item), ctx, w)
        if camp > 0 and camp >= _raw_value(o, city, ctx, w):
            held.append(o)
    if held:
        # One common factor, so the camp units keep their order if the settler is refused.
        factor = (settler - 0.01) / max(scores[id(o)] for o in held)
        for o in held:
            scores[id(o)] *= factor


def rank_production(
    city: City,
    options: Iterable[ProductionChoice],
    context: ProductionContext,
    weights: ProductionWeights | None = None,
    unavailable: frozenset[str] = frozenset(),
) -> list[ProductionChoice]:
    """Return the options worth building, best first. Zero-scored options come last.

    The rest of the order breaks ties by category and name, never by input order, so shuffled
    input gives the same list.
    """
    weights = weights or ProductionWeights()
    options = [o for o in options if o.item != "UNIT_TRADER" or context.trade_safe]  # Issue 269.
    keep, repairs = drop_dominated(list(options), city, context, weights)
    context = race_context(city, keep, context, weights, unavailable)
    scores = {id(o): score(o, city, context, weights) for o in (*repairs, *keep)}
    if not under_threat(city, context):
        # Restoring what is already ours comes before a new district (issue 280). Under threat
        # defence still wins on its own score.
        top = max((scores[id(o)] for o in repairs), default=0.0)
        if top > 0:
            for o in keep:
                if o.category == "DISTRICT" and scores[id(o)] >= top:
                    scores[id(o)] = top - 0.01
    _settler_before_camp(city, keep, scores, context, weights)
    if _growth_before_garrison(city, context):
        # Issue 301: a garrisoned, quiet city early on put the army floor (8 + up to 4 + the
        # matchup) over a Campus or a Builder, and the bot reached turn 60 with one district to
        # the human's five. The floor unit now waits for a district or Builder that scores.
        growth = max(
            (
                scores[id(o)]
                for o in keep
                if (o.category == "DISTRICT" or canonical(o.item) == "UNIT_BUILDER")
                and o.item not in unavailable  # The game or a refusal rules it out (issue 301).
            ),
            default=0.0,
        )
        affected = [
            o
            for o in keep
            if o.category == "UNIT" and _is_floor_unit(o) and scores[id(o)] >= growth
        ]
        if growth > 0 and affected:
            top = max(scores[id(o)] for o in affected)
            # The units yield only to growth that really outranks what the best of them was
            # above: a weaker district must not drop it under an ordinary building (a capped
            # city's Monument), which the floor was meant to beat.
            rivals = max(
                (
                    scores[id(r)]
                    for r in keep
                    if scores[id(r)] < top
                    and not (r.category == "DISTRICT" or canonical(r.item) == "UNIT_BUILDER")
                    and not (r.category == "UNIT" and _is_floor_unit(r))
                ),
                default=0.0,
            )
            if growth - 0.01 > rivals:
                # One common shift, so the units keep their order and gaps (a Warrior still ranks
                # over an Archer if the district then fails to start) and the best sits just
                # under the growth option.
                delta = top - (growth - 0.01)
                for o in affected:
                    scores[id(o)] -= delta
    return sorted(
        [*repairs, *keep],
        key=lambda o: (-scores[id(o)], o.category, o.item, o.repair_x or 0),
    )


def _early_monument_note(
    city: City, ranked: list[ProductionChoice], ctx: ProductionContext, w: ProductionWeights
) -> str:
    """For the options line (issue 299): says the Monument carries the early bonus, so a pick of
    it over a unit can be read from the log."""
    if not wants_early_monument(city, ctx, w):
        return ""
    if not any(o.item == "BUILDING_MONUMENT" for o in ranked):
        return ""
    return f"; early Monument (no culture building, no threat) {w.early_monument:.1f}"


def _camp_demand_note(
    city: City, ranked: list[ProductionChoice], ctx: ProductionContext, w: ProductionWeights
) -> str:
    """For the options line (issue 284): the best a camp unit scores here while the force is
    short, so a settler or building that wins over it can be read against it."""
    if ctx.camp_units <= 0:
        return ""
    best: tuple[float, str] | None = None
    for o in ranked:
        stats = COMBAT_UNITS.get(o.item) if o.category == "UNIT" else None
        if stats is None:
            continue
        value = _camp_score(city, o.item, stats[4], defence_strength(o.item), ctx, w)
        if best is None or value > best[0]:
            best = (value, o.item)
    shown = "none offered" if best is None else f"{best[1]} {best[0]:.1f}"
    return (
        f" | camp demand: {shown}, {ctx.camp_units} wanted, {ctx.camp_building} building, "
        f"blocks expansion={'yes' if ctx.camp_blocks else 'no'}"
    )


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
    options = list(options)
    base_context = context

    def with_race(gone: frozenset[str] = frozenset()) -> ProductionContext:
        """The context with the race alternative, over the same filter and dominance pass as
        `rank_production` and without the items in `gone`, so ranking, log and guard agree."""
        if not weights.wonder_races:
            return base_context
        usable = [o for o in options if o.item != "UNIT_TRADER" or base_context.trade_safe]
        kept, _ = drop_dominated(list(usable), city, base_context, weights)
        return race_context(city, kept, base_context, weights, gone)

    context = with_race()
    ranked = rank_production(city, options, context, weights)
    if ranked:
        log.info(
            "%s options%s: %s%s%s",
            city.name,
            " (defence only)" if only_defence else "",
            ", ".join(f"{o.item} {score(o, city, context, weights):.1f}" for o in ranked[:3]),
            _early_monument_note(city, ranked, context, weights),
            _camp_demand_note(city, ranked, context, weights),
        )
    chain = PlanContext(planner, reads) if planner is not None and reads is not None else None
    if only_defence:
        if not ranked or not is_defence(ranked[0]) or score(ranked[0], city, context, weights) <= 0:
            return None
        ranked = [o for o in ranked if is_defence(o)]
    tried = 0
    unavailable: set[str] = set()

    def skip(option: ProductionChoice, rest: list[ProductionChoice]) -> list[ProductionChoice]:
        """The options still to try, ranked again without a growth option that cannot start.

        Issue 301: a district the game will not start must not keep demoting the army floor.
        """
        # With wonder races on, a refused building is no alternative either (issue 328).
        races = (
            weights.wonder_races and option.category == "BUILDING" and option.item not in WONDERS
        )
        if option.is_repair or not (
            option.category == "DISTRICT" or canonical(option.item) == "UNIT_BUILDER" or races
        ):
            return rest
        nonlocal context
        unavailable.add(option.item)
        context = with_race(frozenset(unavailable))  # The guard below scores with this one.
        again = rank_production(city, options, context, weights, frozenset(unavailable))
        left = {o.item for o in rest}
        return [o for o in again if o.item in left]

    while ranked:
        option = ranked.pop(0)
        if tried >= MAX_ATTEMPTS:
            break
        # Issue 283: a settler scored 0 (one is held, no safe site, enough cities) is never built
        # as the last item left; the city takes the next option or stays idle.
        if option.item == "UNIT_SETTLER" and score(option, city, context, weights) <= 0:
            continue
        # Issue 323: a wonder the race estimator rejected is not started, even when everything
        # above it is refused. Off, a zero-scored wonder is still tried, as before.
        if (
            weights.wonder_races
            and option.item in WONDERS
            and score(option, city, context, weights) <= 0
        ):
            continue
        # A remembered refusal is checked only when the item has won its place in the ranking, so
        # a changed signature by itself costs no call and gives the item no priority.
        if (
            memory is not None
            and memory.has(city, option.item)
            and await memory.blocked(city, option.item, context.turn, reads)
        ):
            ranked = skip(option, ranked)
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
                ranked = skip(option, ranked)
                continue
            # The game's own verdict (`CanStartOperation`, read with the signatures) names the
            # failure; one that tiles cannot change here costs no advisor call and no refusal. It
            # is read every turn, so the district competes again once the game allows it.
            verdict = await reads.district_denied(city.city_id, option.item)
            if verdict is not None:
                log.debug("%s skips %s: the game says %s", city.name, option.item, verdict)
                ranked = skip(option, ranked)
                continue
        tried += 1
        trace = StartTrace()
        result = await _start(game, city, option, trace, chain)
        where = (context.turn, city.city_id, city.name, city.x, city.y)
        if not _failed(result):
            log.info(
                "%s %s %s", city.name, "repairs" if option.is_repair else "builds", option.item
            )
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
        if weights.wonder_races:  # Off: a refusal leaves the ranking as it was (issue 328).
            ranked = skip(option, ranked)  # A refused alternative must not veto a wonder.
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


def _should_switch(
    city: City, marked: frozenset[int] = frozenset(), short: frozenset[int] | None = None
) -> bool:
    """A threatened city is building something that isn't defence and won't finish this turn.

    With `short` (the threat state's verdict), only a city that is short switches: a covered city
    keeps its build (issue 264: a Campus gave way to a Slinger at net threat -60).
    """
    threatened = (
        city.city_id in short if short is not None else city.is_threatened or city.city_id in marked
    )
    if not threatened or city.is_idle or city.build_turns_left <= _FINISHING_TURNS:
        return False
    current = city.building or ""
    return not (defence_strength(current) > 0 or _short(current) in _DEFENSIVE_BUILDINGS)


def _pillaged_items(city: City) -> list[str]:
    """The city's pillaged buildings and districts, by short name."""
    return [
        *(f"district {_short(d.district_type)}" for d in city.districts if d.pillaged),
        *(f"building {_short(b)}" for b in city.pillaged_buildings),
    ]


def is_repair_build(city: City) -> bool:
    """The city is building something it has pillaged: that build is the repair."""
    current = _short(city.building or "")
    return any(item.endswith(" " + current) for item in _pillaged_items(city))


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
        trade_blocked: Any = None,
        rankings: Any = None,
    ) -> None:
        self.rankings = rankings  # A `RankingsHolder`, or None: the rivals count for wonder races.
        self.trade_blocked = (
            trade_blocked  # `SecondaryPolicy.trade_blocked` (turn -> bool), or None.
        )
        self.builder_demand = builder_demand  # `builder.BuilderDemand`, or None.
        self.scouts_built = 0  # Scouts queued so far; caps the early rebuilds (issue 301).
        self._food_focus: set[int] = set()  # Cities this policy set to a food focus.
        self._reset: set[int] = set()  # Cities returned to default, which may starve again.
        self._kept: set[int] = set()  # Cities that starved again after a reset: focus stays.
        self._emit = bus.publish if bus is not None else None
        self.defence = defence  # The threat state (`defence.DefenceState`), or None.
        # Per city, the item a defence switch replaced, to put first again once the city is no
        # longer short (issue 264: four Campuses were abandoned and none resumed).
        self._replaced: dict[int, str] = {}
        # Per city, what was pillaged at the last read, to log new damage and each repair.
        self._pillaged: dict[int, frozenset[str]] = {}
        self.weights = weights or ProductionWeights()  # The strategist replaces this later.
        self.refusals = RefusalMemory()
        self.growth = GrowthPolicy()  # Target size and stance per city (1.47).
        self._cap_focus: dict[int, str] = {}  # City -> yield favored at a cap (issue 215).
        self._cap_since: dict[int, int] = {}  # City -> turn that focus was set (issue 356).
        self._cap_tried: dict[int, int] = {}  # City -> last turn a focus call was made for it.
        self._tiles: dict[int, CityTiles] = {}  # Food of each city's tiles (`city_tiles.py`).
        self._tiles_age = REFRESH_TURNS  # Turns since the last read.
        self._replace_tries = 0  # Reads of the game's replacement tables tried.
        self._replace_done = False
        self._tiles_seen: dict[int, tuple[int, bool]] = {}  # (pop, starving) at the last read.
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
            trade_safe=not (self.trade_blocked is not None and self.trade_blocked(empire.turn)),
        )
        self.expansion.target_cities = self.weights.target_cities
        growth = self.growth.update(cities, empire, self.weights.target_cities, tiles=self._tiles)
        context = replace(
            context,
            sites_ok=self.expansion.sites_ok(empire.turn),
            held_settlers=self.expansion.held_settlers,
            scouts_built=self.scouts_built,
            growth=growth,
            era_push=self._era_push(empire),
        )
        if self.weights.wonder_races:
            # Issue 328: the race inputs are read only when the setting is on. The difficulty
            # comes from the overview `build_empire` already holds. Rival progress per wonder is
            # not exposed by the game's reads, so it stays empty (a wonder built elsewhere is
            # already gone from the game's production list).
            rivals, source = rivals_from_rankings(self.rankings)
            context = replace(
                context, difficulty=empire.difficulty, rivals=rivals, rivals_source=source
            )
        return context

    def _era_push(self, empire) -> bool:
        """Issue 311: whether the era outlook says to favour cheap Historic Moments this turn."""
        if self.weights.era_district_bonus <= 0:
            return False
        return outlook(
            empire.turn,
            empire.era_score,
            empire.dark_threshold,
            empire.golden_threshold,
            empire.game_speed,
            EraSettings(),
        ).push

    async def assign(self, game: ProductionGame) -> list[str]:
        """Choose for each idle city, and return the names of any left without a choice.

        A threatened city that is building something else switches to a defender or walls when
        that is the best thing on offer, unless the current item is about to finish. A city
        already building a defender or walls is left alone, so a switch never repeats.
        """
        cities = await _read_cities(game)
        await self._read_replacements(game)
        await self._read_tiles(game, cities)
        await self._feed_starving(game, cities)
        await self._focus_at_caps(game, cities)
        marked = self.defence.threatened_ids if self.defence is not None else frozenset()
        short = self.defence.short_ids if self.defence is not None else None
        self._log_pillaged(cities)
        switching = [c for c in cities if _should_switch(c, marked, short)]
        repairing = [
            c
            for c in cities
            if self._should_repair(c, marked, short, {s.city_id for s in switching})
        ]
        resuming = [c for c in cities if self._can_resume(c, marked, short or frozenset())]
        idle = [c for c in cities if c.is_idle and c not in resuming]
        if not switching and not idle and not resuming and not repairing:
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
                if city.building:
                    self._replaced[city.city_id] = city.building
                building[city.city_id] = picked.item
                self._note(city, f"production: switched to {picked.item}")
        context = self._context(empire, units, building.values(), cities)
        # Then the cities with something pillaged, which put a repair before the long build they
        # were on (issue 280). The build is kept to resume, as after a defence switch.
        for city in sorted(repairing, key=lambda c: c.city_id):
            options = [o for o in await _read_production(game, city.city_id) if o.is_repair]
            if not options:
                continue
            picked = await choose_for_city(
                game,
                city,
                options,
                context,
                self.weights,
                memory=self.refusals,
                emit=self._emit,
                reads=reads,
            )
            if picked is not None:
                if city.building and city.city_id not in self._replaced:
                    self._replaced[city.city_id] = city.building
                building[city.city_id] = picked.item
                self._note(city, f"production: repairing {picked.item}")
        context = self._context(empire, units, building.values(), cities)
        # Then the cities with an interrupted build, once they are covered again.
        for city in sorted(resuming, key=lambda c: c.city_id):
            options = await _read_production(game, city.city_id)
            wanted = self._replaced[city.city_id]
            offered = [o for o in options if o.item == wanted]
            if offered and score(offered[0], city, context, self.weights) <= 0:
                # Issue 283: the build was interrupted for defence and is no longer wanted (a
                # settler while another waits, or while the camp holds settling back). It is not
                # resumed at a score of 0; the city chooses afresh when it is idle.
                log.info("%s does not resume %s: it scores 0 now", city.name, wanted)
                offered = []
            if not offered:
                del self._replaced[city.city_id]  # The game no longer offers it.
                if city.is_idle:
                    idle.append(city)  # No longer on offer: choose afresh.
                continue
            picked = await choose_for_city(
                game,
                city,
                offered,
                context,
                self.weights,
                memory=self.refusals,
                emit=self._emit,
                reads=reads,
                planner=self.planner,
            )
            if picked is not None:
                del self._replaced[city.city_id]  # Kept on a refusal, to try again next turn.
                building[city.city_id] = picked.item
                self._note(city, f"production: resumed {picked.item}")
                # Counted again from what each city builds, so the cancelled defence unit drops out.
                context = self._context(empire, units, building.values(), cities)
            elif city.is_idle:
                idle.append(city)
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
                if canonical(picked.item) == "UNIT_SCOUT":
                    self.scouts_built += 1
                    context = replace(context, scouts_built=self.scouts_built)
                self._note(city, f"production: chose {picked.item}")
        return unfinished

    def _log_pillaged(self, cities: list[City]) -> None:
        """Log what is pillaged in each city when it changes, and each item repaired."""
        for city in cities:
            now = frozenset(_pillaged_items(city))
            before = self._pillaged.get(city.city_id, frozenset())
            if now != before:
                if now - before:
                    log.info("%s pillaged: %s", city.name, ", ".join(sorted(now)))
                if before - now:
                    log.info("%s repaired: %s", city.name, ", ".join(sorted(before - now)))
            self._pillaged[city.city_id] = now

    @staticmethod
    def _should_repair(
        city: City,
        marked: frozenset[int],
        short: frozenset[int] | None,
        switching: set[int],
    ) -> bool:
        """A busy city with a pillaged building or district, covered, and not about to finish."""
        if city.is_idle or city.city_id in switching or not _pillaged_items(city):
            return False
        if city.build_turns_left <= _FINISHING_TURNS:
            return False
        threatened = (
            city.city_id in short
            if short is not None
            else city.is_threatened or city.city_id in marked
        )
        if threatened:
            return False
        current = city.building or ""
        return not (
            defence_strength(current) > 0
            or _short(current) in _DEFENSIVE_BUILDINGS
            or is_repair_build(city)
        )

    def _can_resume(self, city: City, marked: frozenset[int], short: frozenset[int]) -> bool:
        """A city with an interrupted build that is covered again, and so may go back to it.

        An idle city is covered when it is not short. One still building the defence item that
        replaced the build goes back only when the threat has gone altogether, and not in the
        turns that item needs to finish, so a flickering threat does not make it change twice.
        """
        if city.city_id not in self._replaced or city.city_id in short:
            return False
        if city.is_idle:
            return True
        current = city.building or ""
        emergency = defence_strength(current) > 0 or _short(current) in _DEFENSIVE_BUILDINGS
        return emergency and city.city_id not in marked and city.build_turns_left > _FINISHING_TURNS

    def _plan_budget(self, empire: Empire) -> int:
        """Gold the planner may spend on a tile this turn: none while a city is threatened."""
        from civ6_ai.policies.gold import reserve_for  # Gold imports this module.

        if self.defence is not None and self.defence.any:
            return 0
        return int(empire.gold) - reserve_for(empire.num_cities, empire.gold_per_turn)

    async def _read_replacements(self, game: ProductionGame) -> None:
        """Read the game's unique replacements once per game and merge them over the hand tables
        (issue 354). A failed read is retried up to `READ_TRIES` times, then the hand tables
        stand."""
        execute = getattr(game, "execute_lua", None)
        if self._replace_done or execute is None or self._replace_tries >= READ_TRIES:
            return
        self._replace_tries += 1
        try:
            read = await read_replacements(game)
        except ConnectionError:
            raise
        except Exception as err:
            log.warning("Replacement read failed, keeping the hand tables: %s", err)
            return
        apply_replacements(read)
        self._replace_done = True
        log.info(
            "Replacements read from the game: %d buildings, %d districts, %d units",
            len(read.buildings),
            len(read.districts),
            len(read.units),
        )

    async def _read_tiles(self, game: ProductionGame, cities: list[City]) -> None:
        """Refresh the per-city tile foods: every `REFRESH_TURNS`, or when a city's size changed or
        it started or stopped starving. One batched call for all cities. Never raises on a bad
        read (a partial read is dropped and the last good one kept)."""
        execute = getattr(game, "execute_lua", None)
        if execute is None or not cities:
            return
        self._tiles_age += 1  # One call per turn: no extra read just to learn the turn.
        seen = {c.city_id: (c.pop, is_starving(c)) for c in cities}
        if self._tiles_age < REFRESH_TURNS and seen == self._tiles_seen:
            return
        try:
            tiles = await read_city_tiles(game)
        except ConnectionError:
            raise
        except Exception as err:
            log.warning("City tile read failed, keeping the last one: %s", err)
            return
        self._tiles, self._tiles_age, self._tiles_seen = tiles, 0, seen

    async def _feed_starving(self, game: ProductionGame, cities: list[City]) -> None:
        """Set a food focus on a starving city, and clear it once the city has recovered.

        One `set_city_focus` call per change, never per turn: only a city that starts starving, or
        one this policy focused that now gains `STARVING_FOCUS_RECOVERED` food a turn, costs a call.
        A city that starves again after its reset keeps the focus for good, since the focus's own
        food was what lifted it. A failed call is tried again next turn.
        """
        for city in cities:
            if is_starving(city) and city.city_id not in self._food_focus:
                tiles = self._tiles.get(city.city_id)
                if tiles is not None and tiles.focus_gain() <= 0:
                    # Run 13: the capital and Quirigua had no free food tile, so the focus
                    # (a call and a log line) changed nothing. Asked again when the tiles do.
                    continue
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
            log.info(
                "%s: focus %s (food %+.1f) %s",
                city.name,
                focus,
                city.food_surplus,
                str(result)[:200],
            )
            if add:
                self._food_focus.add(city.city_id)
                if city.city_id in self._reset:  # The deficit came back: the focus was the cure.
                    self._kept.add(city.city_id)
            else:
                self._food_focus.discard(city.city_id)
                self._reset.add(city.city_id)

    async def _focus_at_caps(self, game: ProductionGame, cities: list[City]) -> None:
        """Issue 215: at a housing or amenity cap, favor a non-food yield; undo it below the cap.

        Off unless `GrowthWeights.cap_focus`. One `set_city_focus` call per change. A city the
        starvation rule focused is left to it. A failed call is tried again next turn.
        """
        w = self.growth.weights
        if w.settler_note:
            for city in cities:
                note = settler_delay_note(city)
                if note:
                    log.info("growth %s", note)
        if not w.cap_focus:
            return
        empire = None
        weights = {
            "science": self.weights.science,
            "culture": self.weights.culture,
            "gold": self.weights.gold,
        }
        for city in cities:
            if city.city_id in self._food_focus or is_starving(city):
                self._cap_focus.pop(city.city_id, None)  # The food rule owns the focus now.
                self._cap_since.pop(city.city_id, None)
                continue
            held = self._cap_focus.get(city.city_id)
            if held is None and not at_housing_cap(city):
                continue  # Nothing held and not capped: the usual turn costs no read.
            if empire is None:
                empire = await _read_empire(game)  # One read a turn, only while a city is capped.
            if self._cap_tried.get(city.city_id) == empire.turn:
                continue  # One focus call per city per turn.
            verdict = decide_cap_focus(city, empire, enabled=True, weights=weights)
            keep = verdict.focus
            if keep is not None:
                keep = hold_cap_focus(
                    held,
                    empire.turn - self._cap_since.get(city.city_id, empire.turn),
                    yield_needs(city, empire, weights),
                    margin=w.cap_margin,
                    min_hold=w.cap_min_hold,
                )
            if keep is not None and keep != held:
                focus = keep  # New cap, or a clearly more needed yield after the hold.
            elif verdict.focus is None and held is not None:
                focus = "default"
            else:
                continue
            self._cap_tried[city.city_id] = empire.turn
            try:
                result = await game.set_city_focus(city.city_id, focus)
            except Exception as err:  # A focus is a nicety. It must never stop production.
                log.warning("%s: set_city_focus %s failed: %s", city.name, focus, err)
                continue
            if _failed(str(result)):
                log.info("%s: set_city_focus %s refused: %s", city.name, focus, str(result)[:120])
                continue
            log.info(
                "growth %s",
                verdict.reason
                if focus != "default"
                else f"{city.name}: below its caps again ({verdict.reason}): focus default",
            )
            if focus == "default":
                self._cap_focus.pop(city.city_id, None)
                self._cap_since.pop(city.city_id, None)
            else:
                self._cap_focus[city.city_id] = focus
                self._cap_since[city.city_id] = empire.turn

    def _note(self, city: City, text: str) -> None:
        if self.defence is not None:
            self.defence.note(city.city_id, text)

    def blocker_policies(self) -> Mapping[str, Any]:
        """Policies to pass to `BlockerResolver`, which takes over the production blocker."""
        return {BlockerType.PRODUCTION: self.resolve}

    async def resolve(self, game: ProductionGame, blocker: Blocker) -> bool:
        return not await self.assign(game)
