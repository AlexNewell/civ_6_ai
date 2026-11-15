"""How large each city should grow, and how hard to push it there (issue 215).

Run 12 logged 124 starving notices. Nothing decided what size a city was meant to reach, so growth
was only ever steered once a city was already losing food. This module gives every city a target
size and a stance. It makes no game calls: it reads the `City` records the production policy
already has, and production scoring reads the result (`ProductionContext.growth`).

The model, in order:

1. Supply limits, each a city size the city can sustain now:
   - housing: `floor(housing)`; growth stops at the cap and crawls just below it.
   - food: `pop + floor(surplus)`, assuming a new citizen nets about -1 food (it eats 2 and the
     average unworked tile gives about 1). A starving or flat city is therefore food-limited at
     its current size.
   - tiles: a generous estimate that grows with the turn (borders widen), plus one slot for each
     specialty district. Worked tiles per city are not read, so this rarely binds today.
   - amenities: an optional net figure; the game read gives only provided plus needed, so it is
     ignored until a net read exists (`amenity_limit` argument).
2. Desire, the size worth growing to, which can be below the supply limits:
   - Settler pump: while the empire is short of its city target in the early game, a city stops
     emphasising growth at `pump_pop`, the size that feeds settlers, and no further. This is the
     "reach N quickly, then plateau" shape. The capital is not capped.
   - District unlock: a capped city is raised to the next population that allows a district.
   - Slow growth: a city whose next citizen is more than `slow_turns` away accepts the trickle.
   - Late game: with `late_turns` or fewer left, a citizen cannot pay back its food, so hold.
   - Loyalty pressure: a city losing loyalty wants all the people it can get, overriding the
     plateaus above, because population is what pushes loyalty (PRs 205 and 212).
3. Target = desire clamped by every supply limit, never below the current population.
4. Stance: FEED when losing food, SHRINK_OK when the deficit cannot cost a citizen before the game
   ends, GROW when below target, otherwise HOLD.
5. Gaps: for each supply limit, how many citizens it alone is costing, so production can value
   the item that lifts the binding limit and ignore the rest.

Every number is a knob in `GrowthWeights`. The tile and district figures are estimates and need
live tuning. Each plan carries its reasons, which the policy logs on change.
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from civ6_ai.state import City, Empire

log = logging.getLogger(__name__)

_BIG = 999
_NO_DISTRICT_COUNT = frozenset(
    {"CITY_CENTER", "AQUEDUCT", "BATH", "CANAL", "DAM", "NEIGHBORHOOD", "PRESERVE"}
)


class Stance(StrEnum):
    GROW = "grow"  # Below target: growth buildings and food count in full.
    HOLD = "hold"  # At target: keep what we have, take free growth, buy none.
    FEED = "feed"  # Losing food: stop the loss first.
    SHRINK_OK = "shrink_ok"  # A deficit that cannot cost a citizen before the game ends.


@dataclass(frozen=True)
class GrowthWeights:
    """Knobs for the target model. Zero or large values turn a factor off."""

    new_citizen_net_food: float = 1.0  # Food a new citizen costs beyond what its tile yields.
    tile_base: int = 10  # Workable tiles a young city has.
    tile_per_turns: int = 25  # One more tile per this many turns (borders widen).
    tile_per_district: int = 1  # Specialist slots a specialty district adds.
    pump_pop: int = 4  # Size at which a settler-pump city stops emphasising growth.
    pump_until_turn: int = 110  # Pumping only counts before this turn.
    slow_turns: int = 40  # A next citizen further away than this is a trickle to accept.
    late_turns: int = 20  # Turns from the end in which new growth cannot pay back.
    district_pop_step: int = 3  # Population per district allowed (estimate).
    housing_lead: int = 2  # Housing items count once the cap is this close.
    housing_gap_scale: float = 1.0  # Extra value per citizen the housing cap costs.
    housing_cap: float = 3.0  # Largest housing multiplier.
    food_gap_scale: float = 0.25
    food_cap: float = 2.0
    food_grow_idle: float = 0.8  # Food items when growing and food is not the limit.
    food_hold: float = 0.4  # Food items in a city that is at its target.
    housing_idle: float = 0.3  # Housing items with the cap out of reach (the old rule's value).
    log_every: int = 25  # Turns between full plan lines. Changes are logged at once.


@dataclass(frozen=True)
class CityGrowth:
    """The plan for one city."""

    city_id: int
    name: str
    pop: int
    target: int
    desire: int
    stance: Stance
    housing_limit: int
    food_limit: int
    tile_limit: int
    housing_gap: int = 0
    food_gap: int = 0
    amenity_gap: int = 0
    reasons: tuple[str, ...] = ()

    @property
    def gap(self) -> int:
        return self.target - self.pop

    def line(self) -> str:
        return (
            f"{self.name} pop {self.pop}/{self.target} {self.stance} "
            f"(house {self.housing_limit}, food {self.food_limit}, tiles {self.tile_limit}, "
            f"gaps h{self.housing_gap} f{self.food_gap} a{self.amenity_gap}): "
            + "; ".join(self.reasons)
        )


def _specialty_districts(city: City) -> int:
    count = 0
    for district in city.districts:
        name = district.district_type.removeprefix("DISTRICT_")
        if name not in _NO_DISTRICT_COUNT:
            count += 1
    return count


def plan_city(
    city: City,
    turn: int,
    max_turns: int,
    num_cities: int,
    target_cities: int,
    weights: GrowthWeights | None = None,
    amenity_limit: int | None = None,
) -> CityGrowth:
    """The target size and stance for one city. Pure: same inputs, same plan."""
    w = weights or GrowthWeights()
    reasons: list[str] = []
    pop = city.pop
    surplus = city.food_surplus
    starving = surplus < 0 and pop > 1
    turns_left = max_turns - turn if max_turns > 0 else None
    specialty = _specialty_districts(city)

    housing_limit = int(city.housing)
    cost = max(w.new_citizen_net_food, 0.1)
    food_limit = pop + int(max(0.0, surplus) // cost)
    tile_limit = w.tile_base + turn // max(1, w.tile_per_turns) + w.tile_per_district * specialty
    amen_limit = amenity_limit if amenity_limit is not None else _BIG

    # What the city is worth growing to, before supply limits.
    desire = tile_limit
    losing_loyalty = city.loyalty_per_turn < 0 and city.loyalty < city.loyalty_max
    late = turns_left is not None and turns_left <= w.late_turns
    pump = not city.is_capital and turn < w.pump_until_turn and num_cities < target_cities
    slow = surplus > 0 and city.turns_to_grow > w.slow_turns
    plateau = None
    if late:
        plateau = pop
        reasons.append(f"late game ({turns_left} turns left): growth cannot pay back")
    elif pump:
        plateau = w.pump_pop
        reasons.append(f"settler pump below {target_cities} cities: plateau at {w.pump_pop}")
    elif slow:
        plateau = pop
        reasons.append(f"next citizen {city.turns_to_grow} turns away: accept the trickle")
    if plateau is not None and not late:
        # A capped city is raised to the next size that allows another district.
        allowed = pop // max(1, w.district_pop_step) + 1
        if specialty >= allowed:
            unlock = (pop // w.district_pop_step + 1) * w.district_pop_step
            if unlock > plateau:
                plateau = unlock
                reasons.append(f"district cap: grow to {unlock} to unlock the next")
    if losing_loyalty and not late:
        plateau = None
        reasons.append(f"loyalty falling ({city.loyalty_per_turn:+.1f}/t): population is pressure")
    if plateau is not None:
        desire = min(desire, max(plateau, pop))
    if city.is_capital and not late:
        reasons.append("capital: no plateau")

    limits = {"housing": housing_limit, "food": food_limit, "amenity": amen_limit}
    target = max(pop, min(desire, *limits.values()))

    def gap_of(name: str) -> int:
        others = min(desire, *(v for k, v in limits.items() if k != name))
        return max(0, others - limits[name])

    housing_gap = gap_of("housing") if housing_limit - pop <= w.housing_lead else 0
    food_gap = gap_of("food")
    amenity_gap = gap_of("amenity") if amenity_limit is not None else 0

    if starving:
        stance = Stance.FEED
        reasons.append(f"losing {-surplus:.1f} food/t: stop the loss, never plan a drop")
        if (
            turns_left is not None
            and city.food_stored + surplus * turns_left >= 0
            and turns_left <= w.late_turns
        ):
            stance = Stance.SHRINK_OK
            reasons.append("stored food outlasts the game")
    elif target > pop:
        stance = Stance.GROW
    else:
        stance = Stance.HOLD
    if pop >= housing_limit:
        reasons.append(f"at housing cap {housing_limit}")
    return CityGrowth(
        city.city_id,
        city.name,
        pop,
        target,
        desire,
        stance,
        housing_limit,
        food_limit,
        tile_limit,
        housing_gap,
        food_gap,
        amenity_gap,
        tuple(reasons),
    )


def food_multiplier(plan: CityGrowth, weights: GrowthWeights | None = None) -> float:
    """Scale for the food a building or district yields, by what limits this city."""
    w = weights or GrowthWeights()
    if plan.stance is Stance.SHRINK_OK:
        return 0.0
    if plan.food_gap > 0:
        return min(w.food_cap, 1.0 + w.food_gap_scale * plan.food_gap)
    if plan.stance is Stance.HOLD:
        return w.food_hold
    if plan.stance is Stance.FEED:
        return 1.0
    return w.food_grow_idle


def housing_multiplier(plan: CityGrowth, weights: GrowthWeights | None = None) -> float:
    """Scale for a housing item: full when the cap is costing citizens, low when out of reach."""
    w = weights or GrowthWeights()
    if plan.housing_gap > 0:
        return min(w.housing_cap, 1.0 + w.housing_gap_scale * plan.housing_gap)
    return w.housing_idle


@dataclass
class GrowthPolicy:
    """Keeps the plan for every city and logs it when it changes."""

    weights: GrowthWeights = field(default_factory=GrowthWeights)
    plans: dict[int, CityGrowth] = field(default_factory=dict)
    _logged: dict[int, tuple] = field(default_factory=dict)
    _last_full: int = -10_000

    def update(
        self,
        cities: Iterable[City],
        empire: Empire,
        target_cities: int,
        amenity_limits: Mapping[int, int] | None = None,
    ) -> Mapping[int, CityGrowth]:
        """Plan every city. No game calls. Logs changes at once and all plans every N turns."""
        cities = list(cities)
        plans = {}
        for city in cities:
            limit = amenity_limits.get(city.city_id) if amenity_limits else None
            plans[city.city_id] = plan_city(
                city,
                empire.turn,
                empire.max_turns,
                empire.num_cities,
                target_cities,
                self.weights,
                limit,
            )
        self.plans = plans
        full = empire.turn - self._last_full >= self.weights.log_every
        if full:
            self._last_full = empire.turn
        for plan in plans.values():
            key = (plan.target, plan.stance, plan.housing_gap, plan.food_gap)
            if full or self._logged.get(plan.city_id) != key:
                self._logged[plan.city_id] = key
                log.info("growth turn=%d %s", empire.turn, plan.line())
        return plans

    def plan_for(self, city_id: int) -> CityGrowth | None:
        return self.plans.get(city_id)
