"""Move citizens off food when a city hits its housing or amenity cap (issue 215).

The owner's habit to avoid, from the first observed human game: letting every city grow as large
as it can, past its amenity or housing cap. Growth at the cap is wasted (housing: growth crawls or
stops) or harmful (amenities: unhappiness). The rule instead: when a city is at a cap, favor a
non-food yield so its citizens work production, science, culture, gold or faith tiles.

Two pure functions, no game calls:
- `decide_cap_focus`: which yield to favor for one city, or None, with the reason to log.
- `settler_delay_note`: the early-game min-max, log only for now.

The caller (`CityProductionPolicy`) makes the one `set_city_focus` call per change. A starving
city is never touched here: the existing food-focus rule (PRs 193, 195) wins. The setting is
`GrowthWeights.cap_focus`, off by default.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

from civ6_ai.state import City, Empire

# Typical size of each yield next to science, so "lowest" compares like with like. Faith is left
# out: the empire read gives its balance, not an income, so it cannot be compared with these.
_TYPICAL = {"science": 1.0, "culture": 0.8, "gold": 1.2}
_SETTLER = "UNIT_SETTLER"
SETTLER_WINDOW = 3  # Turns before growth in which a finishing settler is worth a note.


@dataclass(frozen=True)
class CapFocus:
    """The verdict for one city. `focus` None means leave the citizens alone."""

    city_id: int
    focus: str | None
    reason: str
    at_cap: bool = False


def housing_room(city: City) -> int:
    """Citizens the city can still add before housing stops growth (0 at or past the cap)."""
    return max(0, math.floor(city.housing) - city.pop)


def at_housing_cap(city: City) -> bool:
    return city.pop >= math.floor(city.housing)


def most_needed_yield(
    city: City, empire: Empire | None, weights: Mapping[str, float] | None = None
) -> str:
    """The yield to favor: production while the city is mid-build, else the empire's lowest.

    The empire's science, culture and gold per turn are compared after scaling by how much
    each is worth (`weights`, e.g. the production weights) and its typical size.
    """
    if city.building is not None and city.build_turns_left > 1:
        return "production"
    if empire is None:
        return "production"
    weights = weights or {}
    have = {
        "science": empire.science,
        "culture": empire.culture,
        "gold": empire.gold_per_turn,
    }

    def need(name: str) -> float:
        scale = _TYPICAL[name] * max(weights.get(name, 1.0), 0.01)
        return have[name] / scale

    return min(have, key=need)


def decide_cap_focus(
    city: City,
    empire: Empire | None = None,
    *,
    enabled: bool = False,
    amenity_net: int | None = None,
    weights: Mapping[str, float] | None = None,
) -> CapFocus:
    """Pure. Which yield a capped city should favor, or None to leave it.

    `amenity_net` is the city's amenities minus what it needs. The game read gives only the sum
    (`City.amenities_total`), so it is None until a net read exists; None means "not known", and
    only the housing cap triggers. A net of 0 or less counts as the amenity cap: one more citizen
    would push the city into unhappiness.
    """
    if not enabled:
        return CapFocus(city.city_id, None, "off")
    if city.food_surplus < 0 and city.pop > 1:
        return CapFocus(city.city_id, None, "losing food: the food-focus rule decides")
    if city.loyalty_per_turn < 0 and city.loyalty < city.loyalty_max:
        return CapFocus(city.city_id, None, "loyalty falling: population is pressure")
    house_cap = at_housing_cap(city)
    amen_cap = amenity_net is not None and amenity_net <= 0
    if not (house_cap or amen_cap):
        return CapFocus(city.city_id, None, "below the caps: let it grow")
    focus = most_needed_yield(city, empire, weights)
    caps = []
    if house_cap:
        caps.append(f"housing cap {city.pop}/{math.floor(city.housing)}")
    if amen_cap:
        caps.append(f"amenities {amenity_net:+d} net")
    reason = f"{city.name} at {' and '.join(caps)}: growth stops, moving citizens to {focus}"
    return CapFocus(city.city_id, focus, reason, at_cap=True)


def settler_delay_note(city: City) -> str | None:
    """Early-game min-max, log only: would holding a finishing Settler a turn or more keep a pop?

    A Settler costs a citizen. Finishing just after the city grows leaves the new citizen's
    food and the housing room in use; finishing a few turns before the growth drops the city
    first, then it grows back to the same size later. Returns a line when the settler finishes
    1 to `SETTLER_WINDOW` turns before the city grows, else None. Does not act.
    """
    if city.building != _SETTLER or city.food_surplus <= 0 or city.build_turns_left <= 0:
        return None
    gap = city.turns_to_grow - city.build_turns_left
    if not 1 <= gap <= SETTLER_WINDOW:
        return None
    return (
        f"{city.name}: settler done in {city.build_turns_left}t, growth in {city.turns_to_grow}t: "
        f"delaying it {gap}t would finish just after the growth (log only, not acted on)"
    )
