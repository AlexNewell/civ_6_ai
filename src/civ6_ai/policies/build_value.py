"""Value a production option in yield-turns (issue 400, slice 2).

Production scores each build with its own hand-tuned constant (a Monument 12, a settler 14 or 19, a
Scout 15, a camp unit 17 plus strength), so two kinds of build cannot be compared except by tuning
one against the other. This module values every option in the unit `valuation.py` already uses for
gold and faith: a yield-turn, one weighted yield for one turn. It is pure (no game calls) and is
not used by the live picks yet: `scripts/replay_builds.py` re-ranks logged decisions with it.

    value = (yields added per turn x discounted turns of benefit) - production the build costs

- The yields are weighted by `ProductionWeights` (science counts more than faith, as the bot's
  priorities say), the same weights `valuation.py` and `yield_report.py` use.
- The benefit starts when the build finishes (`option.turns` away) and runs for at most
  `horizon_cap` turns, fewer as the game's last turn nears, each turn discounted by `discount`
  (both are `valuation.py`'s own constants, so the two cannot drift).
- The cost is the production the build takes, one weighted production point each (production is a
  yield too). It is the opportunity cost: those hammers could have gone to anything else.
- The figure that ranks options (`BuildValue.value`) is the net per build turn. A city has a fixed
  production per turn, so it is the rate of return on its time that decides, not the total: a
  Monument worth 20 net in 4 turns beats a Library worth 30 in 10. `per_build_turn=False` ranks by
  the net total instead.

Where no value can be computed from what production has, `value_option` returns a `BuildValue`
whose `value` is None and whose `reason` says why. It never makes up a number. Left unvalued:
wonders (no yield table and the race estimator's inputs are not logged), walls (the wall hit points
do not exist before the walls are built), military units in a city with no threat-map reading
(the risk of an attack is not measured), camp units (their worth is the camp mission's, PR 411),
exploration beyond unblocking settlers, projects and every building with no yield table.

Settings carry the assumptions. Each has a comment naming the measurement or the rule behind it;
those that are neither are marked "assumption" and are the first to tune.
"""

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field, fields, replace
from typing import Any

from civ6_ai.policies.expansion import settler_cap
from civ6_ai.policies.game_data import WONDERS
from civ6_ai.policies.growth import CityGrowth, GrowthWeights, Stance, food_multiplier
from civ6_ai.policies.production import (
    _BUILDING_YIELDS,
    _CAMP_CITY_REACH,
    _DEFENSIVE_BUILDINGS,
    _DISTRICT_YIELDS,
    _EARLY_TURNS,
    _MAX_EARLY_SCOUTS,
    _SETTLER_MIN_POP,
    ProductionWeights,
    _base_district,
    _short,
    _yield_value,
    canonical,
    defence_strength,
    hostiles_near,
    is_pressed,
    under_threat,
)
from civ6_ai.policies.valuation import _EXTRA_BUILDINGS, DISCOUNT, HORIZON_CAP
from civ6_ai.state.reader import hex_distance

_YIELD_NAMES = ("food", "production", "gold", "science", "culture", "faith")
# What the production policy's `food` profile stands for in these items: citizens' room, not food.
_HOUSING_ITEMS = frozenset({"AQUEDUCT", "SEWER", "NEIGHBORHOOD"})
_PRODUCTION_PROFILE = 1  # Index of production in a yield profile.


@dataclass(frozen=True)
class BuildValueSettings:
    """Knobs for `value_option`. Each default says where it comes from."""

    # From `valuation.py` (PR 219): the same horizon and discount as gold and faith spending.
    horizon_cap: int = HORIZON_CAP
    discount: float = DISCOUNT
    # One production point spent costs this many yield-turns. 1.0: production is a yield, valued at
    # its `ProductionWeights.production`, so a build that returns less than it cost is a loss.
    cost_per_production: float = 1.0
    # Rank by net value per build turn (see the module doc). False: by the net total.
    per_build_turn: bool = True
    # Housing items (Aqueduct, Sewer, Neighbourhood): citizens of room one item adds. Assumption:
    # the base game's Aqueduct gives 2; the others differ. The growth plan's `housing_gap` (how
    # many citizens the cap costs toward the target) bounds the benefit from above.
    housing_per_item: int = 2
    # Turns for the city to fill the room once it exists. Assumption (a new citizen takes 10 to 25
    # turns at a surplus of 2 to 3 in the logged runs' growth lines).
    housing_fill_turns: int = 15
    # Builders: improvement charges per Builder (a game rule: 3 at standard), the yield one
    # improvement adds (assumption, a lower bound: a Farm's +1 food or a Mine's +1 production),
    # and turns before the first one is worked (assumption: walk and build).
    builder_charges: int = 3
    improvement_yield: float = 1.0
    builder_setup_turns: int = 5
    # Traders: the yield of one running route (assumption: about +1 food and +1 production for a
    # short domestic route in the base game; international gold is not counted) and the turns
    # before it runs (assumption: build, walk, set up).
    route_yield: float = 2.0
    trader_setup_turns: int = 8
    # Settlers: the expected city, measured from per-city yields (STD_T1_04, STD_T1_03, STD_T1_01,
    # turns 1 to 60, 7 non-capital cities). Weighted yield of a non-capital city as a share of the
    # capital's at the same turn: 0.28 at ages 0 to 4 (sd 0.07, 34 city-turns), 0.35 at 5 to 9,
    # 0.45 at 10 to 19 (sd 0.10), 0.51 at 20 to 29 (sd 0.19, 5 cities), 0.54 at 30 and over
    # (sd 0.20, 3 cities). A city is not worth nothing at founding (its centre tile gives about
    # 10.7 yield-turns a turn). The curve is linear from `new_city_start_share` to
    # `new_city_mature_share` over `new_city_ramp_turns` (0.45 at age 15 falls on the line) and is
    # flat after. Past age 25 the data is 3 cities with a wide spread, so no figure is claimed
    # there. The shares are of the building city's yield; they were measured against the capital,
    # so they hold for a capital-built settler and are an assumption for any other city.
    new_city_start_share: float = 0.28
    new_city_mature_share: float = 0.5
    new_city_ramp_turns: int = 20
    # Turns from the build to the city working (assumption: walk and found).
    settler_travel_turns: int = 8
    # The settler also costs a citizen, regrown over this many turns (assumption).
    settler_pop_regrow_turns: int = 10
    min_settler_pop: int = _SETTLER_MIN_POP  # Production's own floor.
    # Defence: the threat map's pressure is net hit points lost a turn plus the unseen threat, a
    # city under `full_pressure` of it counts as certain to fall without more cover. All three are
    # production's own Walls settings (issue 388), so the two read the map the same way.
    full_pressure: float = ProductionWeights.walls_full_pressure
    fog_weight: float = ProductionWeights.walls_fog_weight
    # Share of the city's future yield a fall loses. 1.0: a city that falls is gone, and a retaken
    # one is not counted.
    fall_loss_share: float = 1.0
    # Scouts: those production allows (issue 301), the early turns they matter in, and the share
    # of a settler's expected city a Scout is worth while no safe site is known (assumption: a
    # scout is one of several ways to find one, so far under the settler it unblocks).
    max_scouts: int = _MAX_EARLY_SCOUTS
    scout_turns: int = _EARLY_TURNS
    scout_unblock_share: float = 0.25
    # Districts: when the adjacency is read (a Holy Site's, today), the faith it adds counts one
    # per point, on top of this share of the production table's rate for the district's building
    # slots and Great Person points (assumption: the table's figure mixes both and is not split).
    district_unlock_share: float = 0.5

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool):
                continue
            if value < 0 or not math.isfinite(value):
                raise ValueError(f"Setting {item.name} must be a finite number of at least 0")


# The settings that are assumptions, not measurements or game rules: `replay_builds.py` lists them
# and shows a changed pick's value at half and double each.
ASSUMED_SETTINGS = (
    "new_city_start_share",
    "new_city_mature_share",
    "new_city_ramp_turns",
    "settler_travel_turns",
    "settler_pop_regrow_turns",
    "improvement_yield",
    "builder_setup_turns",
    "route_yield",
    "trader_setup_turns",
    "housing_per_item",
    "housing_fill_turns",
    "scout_unblock_share",
    "district_unlock_share",
)


def scaled(settings: BuildValueSettings, name: str, factor: float) -> BuildValueSettings:
    """The settings with one assumed setting multiplied (a whole-number setting stays whole)."""
    value = getattr(settings, name)
    new = max(1, round(value * factor)) if isinstance(value, int) else value * factor
    return replace(settings, **{name: new})


@dataclass(frozen=True)
class BuildValue:
    """One option's value. `value` is None when it cannot be computed; `reason` says why or how."""

    value: float | None
    reason: str
    net: float | None = None  # Benefit less cost, in yield-turns, before the per-turn division.
    benefit: float = 0.0
    cost: float = 0.0


@dataclass(frozen=True)
class OptionInput:
    """One offered option as logged: its place, price and the current score."""

    item: str
    category: str
    cost: float
    turns: int
    score: float | None  # None: the option was dropped before scoring (dominated or filtered).
    is_repair: bool = False


@dataclass(frozen=True)
class DecisionInputs:
    """What `value_option` reads about the city and the empire, all of it logged per decision."""

    turn: int = 0
    last_turn: int | None = None
    pop: int = 1
    housing: float = 0.0
    food_surplus: float = 0.0
    yields: tuple[float, ...] = (0.0,) * 6  # food, production, gold, science, culture, faith.
    has_walls: bool = False
    garrisoned: bool = True
    unimproved: int = 0  # Unimproved resources around the city (builder jobs seen).
    num_cities: int = 1
    builders: int = 0
    settlers: int = 0
    recon: int = 0
    scouts_built: int = 0
    army: int = 0
    traders: int = 0
    trade_capacity: int = 0
    trade_safe: bool = True
    sites_ok: bool = True
    held_settlers: int = 0
    idle_builders: int = 0
    spare_jobs: int | None = None
    settler_cap: int = 0  # Production's own cap on cities plus settlers in flight.
    hostiles_near: int = 0
    under_threat: bool = False
    pressed: bool = False
    holy_site_adjacency: int = 0  # 0: not read, or none fits.
    camp_in_reach: bool = False  # A camp force is short and this city is in reach of the camp.
    growth: dict[str, Any] | None = None  # stance, housing_gap, food_gap, gap.
    reading: dict[str, Any] | None = None  # Threat map: threat, fog, cover, damage, net, turns.

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            out[item.name] = list(value) if isinstance(value, tuple) else value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionInputs":
        known = {item.name for item in fields(cls)}
        picked = {k: v for k, v in data.items() if k in known}
        if "yields" in picked:
            picked["yields"] = tuple(float(v) for v in picked["yields"])
        return cls(**picked)


def discounted_turns(delay: float, last_turn_left: float | None, s: BuildValueSettings) -> float:
    """Discounted turns of benefit that start `delay` turns from now.

    `last_turn_left` is the turns to the game's last turn from now (None: not known, only the cap
    applies). The delay itself is discounted: a yield later is worth less.
    """
    left = s.horizon_cap if last_turn_left is None else last_turn_left - delay
    n = int(max(0.0, min(left, s.horizon_cap)))
    if n <= 0:
        return 0.0
    keep = 1.0 - s.discount
    if s.discount <= 0:
        return float(n)
    return keep**delay * (1.0 - keep**n) / s.discount


def city_yield(inputs: DecisionInputs, w: ProductionWeights) -> float:
    """The city's measured weighted yield per turn over all six yields."""
    return _yield_value(tuple(inputs.yields), w)


def _left(inputs: DecisionInputs) -> float | None:
    return None if inputs.last_turn is None else float(inputs.last_turn - inputs.turn)


def _plan(inputs: DecisionInputs) -> CityGrowth | None:
    g = inputs.growth
    if g is None:
        return None
    return CityGrowth(
        city_id=0,
        name="",
        pop=inputs.pop,
        target=inputs.pop + int(g.get("gap", 0)),
        desire=0,
        stance=Stance(g.get("stance", "hold")),
        housing_limit=0,
        food_limit=0,
        tile_limit=0,
        housing_gap=int(g.get("housing_gap", 0)),
        food_gap=int(g.get("food_gap", 0)),
    )


def _result(
    benefit: float, option: OptionInput, reason: str, s: BuildValueSettings, w: ProductionWeights
) -> BuildValue:
    """Net the benefit against the production the build costs and form the ranking value."""
    cost = option.cost * s.cost_per_production * w.production
    net = benefit - cost
    value = net / max(option.turns, 1) if s.per_build_turn else net
    return BuildValue(value, reason, net, benefit, cost)


def _unvalued(reason: str) -> BuildValue:
    return BuildValue(None, reason)


def _profile_rate(
    profile: tuple[float, ...], inputs: DecisionInputs, w: ProductionWeights
) -> tuple[float, str]:
    """Weighted yield per turn of a table profile. The food part follows the growth plan."""
    plan = _plan(inputs)
    rate = _yield_value(profile, w)
    note = "table"
    if plan is not None and profile[0] > 0:
        factor = food_multiplier(plan, GrowthWeights())
        rate += (factor - 1.0) * profile[0] * w.food
        note = f"table, food x{factor:.2f} by the growth plan"
    return max(rate, 0.0), note


def _housing_value(
    option: OptionInput,
    inputs: DecisionInputs,
    s: BuildValueSettings,
    w: ProductionWeights,
) -> BuildValue:
    """Room for citizens: the citizens the cap costs, at the city's measured yield per citizen."""
    plan = _plan(inputs)
    if plan is None:
        return _unvalued("housing item with no growth plan: the cap's cost is unknown")
    citizens = min(plan.housing_gap, s.housing_per_item)
    per_citizen = city_yield(inputs, w) / max(inputs.pop, 1)
    delay = option.turns + s.housing_fill_turns / 2.0
    benefit = citizens * per_citizen * discounted_turns(delay, _left(inputs), s)
    reason = (
        f"housing: {citizens} citizens (gap {plan.housing_gap}, item adds {s.housing_per_item}) "
        f"x {per_citizen:.1f} yield each"
    )
    return _result(benefit, option, reason, s, w)


def _building_value(
    option: OptionInput, inputs: DecisionInputs, s: BuildValueSettings, w: ProductionWeights
) -> BuildValue:
    name = _short(option.item)
    if name in _DEFENSIVE_BUILDINGS:
        return _unvalued("walls: the hit points they add are not known before they exist")
    if name in _HOUSING_ITEMS:
        return _housing_value(option, inputs, s, w)
    profile = _BUILDING_YIELDS.get(name) or _EXTRA_BUILDINGS.get(name)
    if profile is None:
        return _unvalued(f"{name}: no yield profile")
    rate, note = _profile_rate(profile, inputs, w)
    benefit = rate * discounted_turns(option.turns, _left(inputs), s)
    return _result(benefit, option, f"{note}: {rate:.2f} a turn", s, w)


def _district_value(
    option: OptionInput, inputs: DecisionInputs, s: BuildValueSettings, w: ProductionWeights
) -> BuildValue:
    name = _base_district(option.item)
    if name in _HOUSING_ITEMS:
        return _housing_value(option, inputs, s, w)
    profile = _DISTRICT_YIELDS.get(name)
    if profile is None or not any(profile):
        return _unvalued(f"{name}: no yield profile")
    rate, note = _profile_rate(profile, inputs, w)
    if name == "HOLY_SITE" and inputs.holy_site_adjacency > 0:
        rate = rate * s.district_unlock_share + inputs.holy_site_adjacency * w.faith
        note = f"table x{s.district_unlock_share:g} plus adjacency {inputs.holy_site_adjacency}"
    benefit = rate * discounted_turns(option.turns, _left(inputs), s)
    return _result(benefit, option, f"{note}: {rate:.2f} a turn", s, w)


def _settler_value(
    option: OptionInput, inputs: DecisionInputs, s: BuildValueSettings, w: ProductionWeights
) -> BuildValue:
    """The expected city, from this city's measured yields (see `BuildValueSettings`)."""
    gate = ""
    if not inputs.sites_ok:
        gate = "no safe site known"
    elif inputs.held_settlers > 0:
        gate = "a settler already waits with nowhere to go"
    elif inputs.num_cities + inputs.settlers >= inputs.settler_cap:
        gate = f"cities plus settlers reach the cap {inputs.settler_cap}"
    elif inputs.pop < s.min_settler_pop:
        gate = f"city size {inputs.pop} is below {s.min_settler_pop}"
    elif inputs.pressed:
        gate = "the city is pressed"
    if gate:
        return _result(0.0, option, f"settler adds no city: {gate}", s, w)
    base = city_yield(inputs, w)
    start = option.turns + s.settler_travel_turns
    ramp = max(s.new_city_ramp_turns, 1)
    left = _left(inputs)
    n = s.horizon_cap if left is None else int(max(0.0, min(left - start, s.horizon_cap)))
    keep = 1.0 - s.discount
    span = s.new_city_mature_share - s.new_city_start_share
    benefit = sum(
        base * (s.new_city_start_share + span * min(1.0, k / ramp)) * keep ** (start + k)
        for k in range(n)
    )
    # The settler also takes a citizen, regrown over `settler_pop_regrow_turns`.
    per_citizen = city_yield(inputs, w) / max(inputs.pop, 1)
    benefit -= per_citizen * s.settler_pop_regrow_turns
    reason = (
        f"expected city {s.new_city_start_share:g} to {s.new_city_mature_share:g} of this city's "
        f"{base:.1f} over {ramp} turns, minus a citizen"
    )
    return _result(benefit, option, reason, s, w)


def _builder_value(
    option: OptionInput, inputs: DecisionInputs, s: BuildValueSettings, w: ProductionWeights
) -> BuildValue:
    if inputs.idle_builders > 0:
        return _result(0.0, option, "a builder already has nothing to do", s, w)
    if inputs.builders >= max(1, inputs.num_cities):
        return _result(0.0, option, "one builder for each city already exists", s, w)
    if inputs.spare_jobs is not None:
        jobs, seen = inputs.spare_jobs, f"{inputs.spare_jobs} unclaimed jobs"
    elif inputs.unimproved > 0:
        jobs, seen = inputs.unimproved, f"{inputs.unimproved} unimproved resources (lower bound)"
    else:
        return _unvalued("builder: the number of jobs is unknown and no resource is unimproved")
    charges = min(s.builder_charges, jobs)
    rate = charges * s.improvement_yield * w.production
    delay = option.turns + s.builder_setup_turns
    benefit = rate * discounted_turns(delay, _left(inputs), s)
    return _result(benefit, option, f"{charges} improvements from {seen}", s, w)


def _trader_value(
    option: OptionInput, inputs: DecisionInputs, s: BuildValueSettings, w: ProductionWeights
) -> BuildValue:
    free = inputs.trade_capacity - inputs.traders
    if free <= 0 or not inputs.trade_safe:
        why = "no free route slot" if free <= 0 else "every route is too dangerous"
        return _result(0.0, option, f"trader adds no route: {why}", s, w)
    rate = s.route_yield * w.production
    delay = option.turns + s.trader_setup_turns
    benefit = rate * discounted_turns(delay, _left(inputs), s)
    return _result(benefit, option, f"one route of {s.route_yield:g} a turn", s, w)


def _scout_value(
    option: OptionInput, inputs: DecisionInputs, s: BuildValueSettings, w: ProductionWeights
) -> BuildValue:
    if inputs.recon > 0 or inputs.scouts_built >= s.max_scouts or inputs.turn > s.scout_turns:
        return _result(0.0, option, "enough scouts, or past the early turns", s, w)
    if inputs.sites_ok:
        return _unvalued("scout: sites are known and exploration has no yield measure")
    city = _settler_value(option, replace_sites(inputs), s, w)
    unblocked = (city.benefit if city.value is not None else 0.0) * s.scout_unblock_share
    return _result(
        unblocked, option, f"unblocks settlers: {s.scout_unblock_share:g} of a settler's city", s, w
    )


def replace_sites(inputs: DecisionInputs) -> DecisionInputs:
    """The inputs as if a safe site were known, to value what a scout would unblock."""
    data = inputs.to_dict()
    data["sites_ok"] = True
    return DecisionInputs.from_dict(data)


def _defender_value(
    option: OptionInput, inputs: DecisionInputs, s: BuildValueSettings, w: ProductionWeights
) -> BuildValue:
    """Expected loss avoided: the share of the city's future yield at risk, from the threat map."""
    if inputs.camp_in_reach:
        return _unvalued("camp unit: a camp force is short in reach; the mission is not modelled")
    reading = inputs.reading
    if reading is None:
        return _unvalued("military unit: no threat-map reading, so the risk is not measured")
    strength = defence_strength(option.item)
    if strength <= 0:
        return _unvalued("unit holds no city")
    pressure = float(reading.get("net", 0.0)) + s.fog_weight * float(reading.get("fog", 0.0))
    risk = min(1.0, pressure / s.full_pressure) if s.full_pressure > 0 else 1.0
    cover = max(float(reading.get("cover", 0.0)), 0.0)
    share = strength / (strength + cover)
    city_value = city_yield(inputs, w) * discounted_turns(0.0, _left(inputs), s)
    turns = reading.get("turns")
    # With walls `turns` is when the walls break, not when the city falls, so it is no deadline.
    late = turns is not None and not inputs.has_walls and float(turns) < option.turns
    benefit = 0.0 if late else risk * share * s.fall_loss_share * city_value
    reason = (
        "arrives after the city falls"
        if late
        else f"risk {risk:.2f} x share {share:.2f} of the city's {city_value:.0f}"
    )
    return _result(benefit, option, reason, s, w)


def value_option(
    option: OptionInput,
    inputs: DecisionInputs,
    settings: BuildValueSettings | None = None,
    weights: ProductionWeights | None = None,
) -> BuildValue:
    """What the option is worth to this city in yield-turns, or no value and the reason."""
    s = settings or BuildValueSettings()
    w = weights or ProductionWeights()
    item = option.item
    if item in WONDERS:
        return _unvalued("wonder: no yield table; the race estimator's inputs are not logged")
    if option.category == "PROJECT":
        return _unvalued("project: no yield profile")
    if option.category == "UNIT":
        kind = canonical(item)
        if kind == "UNIT_SETTLER":
            return _settler_value(option, inputs, s, w)
        if kind == "UNIT_BUILDER":
            return _builder_value(option, inputs, s, w)
        if kind == "UNIT_TRADER":
            return _trader_value(option, inputs, s, w)
        if kind == "UNIT_SCOUT":
            return _scout_value(option, inputs, s, w)
        if defence_strength(item) > 0:
            return _defender_value(option, inputs, s, w)
        return _unvalued("unit: no value model")
    if option.category == "DISTRICT":
        return _district_value(option, inputs, s, w)
    return _building_value(option, inputs, s, w)


def is_defence_option(option: OptionInput) -> bool:
    """A unit that holds a city, or walls (the picks a threatened city must not lose)."""
    if option.category == "UNIT":
        return defence_strength(option.item) > 0
    return option.category == "BUILDING" and _short(option.item) in _DEFENSIVE_BUILDINGS


@dataclass(frozen=True)
class Reranked:
    """The old and new pick of one logged decision."""

    old: OptionInput | None
    new: OptionInput | None
    old_value: BuildValue | None
    new_value: BuildValue | None
    values: dict[str, BuildValue] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.old is not None and self.new is not None and self.old.item != self.new.item


def rerank(
    options: list[OptionInput],
    inputs: DecisionInputs,
    settings: BuildValueSettings | None = None,
    weights: ProductionWeights | None = None,
    defence_only: bool = False,
) -> Reranked:
    """Old pick (the first the policy ranked) against the best valued option.

    An old pick with no value is never replaced: nothing is comparable to it. A new pick must have
    a higher value than the old one, so the change is a gain under this model by construction.
    Options are in the policy's ranked order; dropped ones (no score) are not candidates.
    """
    live = [o for o in options if o.score is not None]
    if defence_only:
        # The live guard: a defence-only call changes nothing unless the best option is a defence
        # option that scores above zero.
        if not live or not is_defence_option(live[0]) or live[0].score <= 0:
            return Reranked(None, None, None, None)
        live = [o for o in live if is_defence_option(o)]
    if not live:
        return Reranked(None, None, None, None)
    values = {o.item + ("#r" if o.is_repair else ""): value_option(o, inputs, settings, weights)
              for o in live}  # fmt: skip
    old = live[0]
    old_value = values[old.item + ("#r" if old.is_repair else "")]
    if old_value.value is None:
        return Reranked(old, old, old_value, old_value, values)
    best, best_value = old, old_value
    for o in live[1:]:
        v = values[o.item + ("#r" if o.is_repair else "")]
        if v.value is not None and v.value > (best_value.value or 0.0) + 1e-9:
            best, best_value = o, v
    return Reranked(old, best, old_value, best_value, values)


# The log line production writes once per decision, and the version of its fields.
LOG_TAG = "production_options"
LOG_VERSION = 1


def decision_record(
    city: Any,
    offered: Iterable[Any],
    ranked: Iterable[Any],
    scores: dict[int, float],
    ctx: Any,
    weights: ProductionWeights,
    last_turn: int | None,
    defence_only: bool = False,
) -> dict[str, Any]:
    """Everything `rerank` needs for one decision, from what production already holds.

    `ranked` is the policy's order (best first) and `scores[id(option)]` the score it used.
    `offered` is every option the game offered: those absent from `ranked` were dropped before
    scoring and are logged with a null score. Options are compact arrays: item, category, cost,
    turns, score, repair flag. No game call is made.
    """
    ranked = list(ranked)
    kept = {id(o) for o in ranked}
    rows = [
        [o.item, o.category, o.cost, o.turns, round(scores[id(o)], 3), int(o.is_repair)]
        for o in ranked
    ]
    rows += [
        [o.item, o.category, o.cost, o.turns, None, int(o.is_repair)]
        for o in offered
        if id(o) not in kept
    ]
    plan = ctx.growth.get(city.city_id)
    reading = ctx.readings.get(city.city_id)
    turns_left = getattr(reading, "turns", None)
    inputs = DecisionInputs(
        turn=ctx.turn,
        last_turn=last_turn,
        pop=city.pop,
        housing=city.housing,
        food_surplus=round(city.food_surplus, 2),
        yields=tuple(
            round(float(v), 2)
            for v in (city.food, city.production, city.gold, city.science, city.culture, city.faith)
        ),
        has_walls=city.has_walls,
        garrisoned=city.garrison_unit is not None,
        unimproved=len(city.unimproved_resources),
        num_cities=ctx.num_cities,
        builders=ctx.builders,
        settlers=ctx.settlers,
        recon=ctx.recon,
        scouts_built=ctx.scouts_built,
        army=ctx.army,
        traders=ctx.traders,
        trade_capacity=ctx.trade_capacity,
        trade_safe=ctx.trade_safe,
        sites_ok=ctx.sites_ok,
        held_settlers=ctx.held_settlers,
        idle_builders=ctx.idle_builders,
        spare_jobs=ctx.spare_jobs,
        settler_cap=settler_cap(ctx.turn, ctx.num_cities, ctx.sites_ok, weights.target_cities),
        hostiles_near=hostiles_near(city, ctx),
        under_threat=under_threat(city, ctx),
        pressed=is_pressed(city, ctx),
        holy_site_adjacency=ctx.holy_site_adjacency,
        camp_in_reach=ctx.camp_units > 0
        and (
            ctx.camp_target is None
            or hex_distance(city.x, city.y, *ctx.camp_target) <= _CAMP_CITY_REACH
        ),
        growth=None
        if plan is None
        else {
            "stance": str(plan.stance),
            "housing_gap": plan.housing_gap,
            "food_gap": plan.food_gap,
            "gap": plan.gap,
        },
        reading=None
        if reading is None
        else {
            "threat": round(reading.threat, 2),
            "fog": round(reading.fog, 2),
            "cover": round(reading.cover, 2),
            "damage": round(reading.damage, 2),
            "net": round(reading.net, 2),
            "turns": None if turns_left is None or math.isinf(turns_left) else round(turns_left, 1),
        },
    )
    return {
        "v": LOG_VERSION,
        "turn": ctx.turn,
        "city": city.name,
        "city_id": city.city_id,
        "defence_only": defence_only,
        "inputs": inputs.to_dict(),
        "options": rows,
    }


def log_line(record: dict[str, Any]) -> str:
    """The record as one compact JSON line (no spaces)."""
    return json.dumps(record, separators=(",", ":"))


def options_from_record(record: dict[str, Any]) -> list[OptionInput]:
    """The logged options, in the order production ranked them."""
    return [
        OptionInput(item, category, float(cost), int(turns), score, bool(repair))
        for item, category, cost, turns, score, repair in record["options"]
    ]
