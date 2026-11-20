import pytest

from civ6_ai.strategy import boosts as b
from civ6_ai.strategy.boosts import BoostKind as K
from civ6_ai.strategy.boosts import parse_boost

CASES = [
    ("Mine a resource.", K.IMPROVEMENT, 1, "MINE", (b.BUILDER,)),
    ("Build a Quarry.", K.IMPROVEMENT, 1, "QUARRY", (b.BUILDER,)),
    ("Build 3 Mines.", K.IMPROVEMENT, 3, "MINE", (b.BUILDER,)),
    ("Build 6 Farms.", K.IMPROVEMENT, 6, "FARM", (b.BUILDER,)),
    ("Build a Pasture.", K.IMPROVEMENT, 1, "PASTURE", (b.BUILDER,)),
    ("Build an Iron Mine.", K.IMPROVEMENT, 1, "IRON_MINE", (b.BUILDER,)),
    ("Farm a resource.", K.IMPROVEMENT, 1, "FARM", (b.BUILDER,)),
    ("Improve 3 tiles.", K.IMPROVEMENT, 3, b.ANY, (b.BUILDER,)),
    ("Improve 2 sea resources.", K.IMPROVEMENT, 2, b.SEA_RESOURCE, (b.BUILDER,)),
    ("Build any specialty district.", K.DISTRICT, 1, b.ANY, (b.PRODUCTION,)),
    ("Build 2 Campus districts.", K.DISTRICT, 2, "CAMPUS", (b.PRODUCTION,)),
    ("Build 3 different specialty districts.", K.DISTRICT, 3, b.DIFFERENT, (b.PRODUCTION,)),
    ("Build an Encampment.", K.DISTRICT, 1, "ENCAMPMENT", (b.PRODUCTION,)),
    ("Build an Aqueduct.", K.DISTRICT, 1, "AQUEDUCT", (b.PRODUCTION,)),
    ("Build 2 Harbors.", K.DISTRICT, 2, "HARBOR", (b.PRODUCTION,)),
    ("Build 2 Temples.", K.BUILDING, 2, "TEMPLE", (b.PRODUCTION,)),
    ("Build a Water Mill.", K.BUILDING, 1, "WATER_MILL", (b.PRODUCTION,)),
    ("Build 2 Stock Exchanges.", K.BUILDING, 2, "STOCK_EXCHANGE", (b.PRODUCTION,)),
    ("Build Ancient Walls.", K.BUILDING, 1, "ANCIENT_WALLS", (b.PRODUCTION,)),
    ("Own 2 Galleys.", K.UNIT, 2, "GALLEY", (b.PRODUCTION,)),
    ("Own 3 Archers.", K.UNIT, 3, "ARCHER", (b.PRODUCTION,)),
    ("Own 2 Crossbowman.", K.UNIT, 2, "CROSSBOWMAN", (b.PRODUCTION,)),
    ("Build 2 Caravels.", K.UNIT, 2, "CARAVEL", (b.PRODUCTION,)),
    (
        "Have 8 land combat units in your military.",
        K.UNIT,
        8,
        b.LAND_COMBAT,
        (b.PRODUCTION, b.MILITARY),
    ),
    ("Kill a unit with a Slinger.", K.KILL, 1, "SLINGER", (b.MILITARY, b.PRODUCTION)),
    ("Kill 3 Barbarians.", K.KILL, 3, b.BARBARIAN, (b.MILITARY,)),
    ("Clear a Barbarian Outpost.", K.KILL, 1, b.BARBARIAN_OUTPOST, (b.MILITARY,)),
    ("Grow your civilization to at least 6 population.", K.POPULATION, 6, b.TOTAL, ()),
    ("Grow a city to 10 population.", K.POPULATION, 10, b.CITY, ()),
    ("Meet another civilization.", K.MEETING, 1, b.CIVILIZATION, (b.EXPLORATION,)),
    ("Meet 3 city-states.", K.MEETING, 3, b.CITY_STATE, (b.EXPLORATION,)),
    ("Found a pantheon.", K.FOUND, 1, b.PANTHEON, (b.FAITH,)),
    ("Found a city on the Coast.", K.FOUND, 1, b.COASTAL_CITY, (b.SETTLING,)),
    ("Build a wonder.", K.WONDER, 1, b.ANY, (b.PRODUCTION,)),
    ("Build a Classical era or later wonder", K.WONDER, 1, b.ANY, (b.PRODUCTION,)),
    ("Have the Feudalism civic.", K.ADOPT, 1, "FEUDALISM", (b.RESEARCH,)),
    ("Research the Astronomy technology.", K.ADOPT, 1, "ASTRONOMY", (b.RESEARCH,)),
    ("Research Radio.", K.ADOPT, 1, "RADIO", (b.RESEARCH,)),
]


@pytest.mark.parametrize(("text", "kind", "count", "target", "policies"), CASES)
def test_known_descriptions(text, kind, count, target, policies):
    spec = parse_boost(text)
    assert (spec.kind, spec.count, spec.target, spec.policies) == (kind, count, target, policies)
    assert spec.description == text


def test_on_resource_flag():
    assert parse_boost("Mine a resource.").on_resource
    assert not parse_boost("Build 3 Mines.").on_resource


def test_population_is_not_self_triggerable_by_a_policy():
    # Growth comes from the cities, not from a policy we call on purpose.
    assert not parse_boost("Grow a city to 10 population.").self_triggerable


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Be the target of a Declaration of War.",
        "Have an alliance with another civilization.",
        "Earn a Great Scientist.",
        "Build a Coal Mine and an Ironclad.",
        "Build a University adjacent to a Mountain.",
        "Something the game added later.",
        "Build a National Park.",
    ],
)
def test_unknown_is_other(text):
    spec = parse_boost(text)
    assert spec.kind is K.OTHER
    assert not spec.self_triggerable
