"""When a special district is worth building, in place of a fixed ban list.

The game only offers a Dam or Canal where the geography allows one, so the question here is
whether the city or the plan needs it. Everything is a pure function of what the production
policy already reads: the city, the turn, the empire's districts and the policy document's victory
target. No game call.

Reasons, one per district (also in docs/decisions.md):

- Spaceport: the space race projects need one, once per empire. A science victory wants it as soon
  as it is offered. With no victory aim it waits until the late game, when the projects can still
  matter, so early production goes elsewhere.
- Aerodrome: only air units and air-based plans need it. A domination victory wants one.
- Dam: adds housing along a river or floodplain. Wanted when housing caps a city that has grown.
- Canal: joins two water bodies, which shortens naval and trade routes. Wanted in a city that has
  grown into its housing cap, since it also adds housing and trade yield, and not in the early game.
- Stadium: amenities and culture for a sports-led plan. Wanted for a culture victory once the city
  is established. The city's net amenities are not read (the overview gives provided plus needed,
  not the balance), so a shortage alone does not ask for one.
"""

from collections.abc import Collection

SPECIAL_DISTRICTS = frozenset({"SPACEPORT", "AERODROME", "DAM", "CANAL", "STADIUM"})

LATE_TURN = 200  # With no victory aim, a Spaceport waits until here.
_MIN_POP = 4  # A city this size is worth a housing or amenity district.
_EARLY_TURN = 80

# What a wanted special district is worth, in the same units as `_DISTRICT_YIELDS` scores.
BASE_VALUE = {"SPACEPORT": 4.0, "AERODROME": 3.0, "DAM": 3.5, "CANAL": 3.0, "STADIUM": 3.0}
PLANNED_VALUE = 10.0  # A district the victory target asks for beats ordinary districts.


def is_planned(name: str, victory: str) -> bool:
    """True when the victory target asks for this district."""
    return (name, victory) in {
        ("SPACEPORT", "science"),
        ("AERODROME", "domination"),
        ("STADIUM", "culture"),
    }


def wants_district(
    name: str,
    *,
    victory: str,
    turn: int,
    pop: int,
    housing: float,
    empire_districts: Collection[str] = (),
) -> bool:
    """Whether a special district is worth building in this city now. Others are always wanted."""
    if name not in SPECIAL_DISTRICTS:
        return True
    capped = housing - pop <= 1 and pop >= _MIN_POP
    if name == "SPACEPORT":
        if name in empire_districts:
            return False  # One serves every project.
        return victory == "science" or (victory == "any" and turn >= LATE_TURN)
    if name == "AERODROME":
        return victory == "domination"
    if name == "DAM":
        return capped
    if name == "CANAL":
        return capped and turn >= _EARLY_TURN
    if name == "STADIUM":
        return victory == "culture" and pop >= _MIN_POP and turn >= _EARLY_TURN
    return True
