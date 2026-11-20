"""The early-game boosts the bot can cause itself, read from the game's own descriptions (#302).

Every tech and civic option carries a `boost_desc` ("Build a Quarry.", "Kill a unit with a
Slinger.", "Grow your civilization to at least 6 population."). `parse_boost` turns one into a
`BoostSpec`: the kind of action that earns it, how many, of what, and which of our policies can
cause it. The table is built from the descriptions seen in the logs (91 distinct strings across
runs and the observed human game); a string it doesn't know comes back as `BoostKind.OTHER`
with no policies, so a new description never breaks anything, it is just not chased.

Pure functions only. `boost_judgement.py` uses the specs to estimate reach.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class BoostKind(StrEnum):
    IMPROVEMENT = "improvement"  # A tile improvement built (Quarry, Mine, Farm, Fishing Boats).
    DISTRICT = "district"  # A district built (any specialty district, Campus, Encampment).
    BUILDING = "building"  # A building built in a city (Temple, Market, Water Mill).
    UNIT = "unit"  # Units built or owned (Galleys, Archers, land combat units).
    KILL = "kill"  # A kill, by a unit type or of barbarians.
    POPULATION = "population"  # Total or single-city population.
    MEETING = "meeting"  # Meet a civilization or city-states.
    FOUND = "found"  # Found a pantheon, a religion or a city.
    ADOPT = "adopt"  # Have a civic, adopt a government, research another tech.
    WONDER = "wonder"  # Build a wonder.
    OTHER = "other"  # Not understood, or not something a policy can cause.


# Which of our policies can cause the boost. Names match the modules in `policies/`.
BUILDER = "builder"
PRODUCTION = "production"
MILITARY = "military"
SETTLING = "settling"
FAITH = "faith"
RESEARCH = "research"
EXPLORATION = "exploration"  # Scouts, run by the military policy's scout handling.

# Targets that are not a plain type name.
ANY = "ANY"  # "Improve 3 tiles", "Build any specialty district".
DIFFERENT = "DIFFERENT"  # "Build 3 different specialty districts".
SEA_RESOURCE = "SEA_RESOURCE"  # "Improve 2 sea resources": Fishing Boats and the like.
LAND_COMBAT = "LAND_COMBAT"  # "Have 8 land combat units in your military".
BARBARIAN = "BARBARIAN"  # "Kill 3 Barbarians".
BARBARIAN_OUTPOST = "BARBARIAN_OUTPOST"  # "Clear a Barbarian Outpost".
CITY = "CITY"  # Population in one city.
TOTAL = "TOTAL"  # Population of the whole civilization.
CIVILIZATION = "CIVILIZATION"
CITY_STATE = "CITY_STATE"
PANTHEON = "PANTHEON"
RELIGION = "RELIGION"
COASTAL_CITY = "COASTAL_CITY"


@dataclass(frozen=True)
class BoostSpec:
    """What earns a boost. `count` is how many are needed, `target` what of (see the constants)."""

    kind: BoostKind
    count: int = 1
    target: str = ANY
    # True when the boost needs a resource under the tile ("Mine a resource", "Farm a resource").
    on_resource: bool = False
    policies: tuple[str, ...] = ()
    description: str = ""

    @property
    def self_triggerable(self) -> bool:
        """True when at least one of our policies can cause it."""
        return self.kind is not BoostKind.OTHER and bool(self.policies)


_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}


def _count(text: str) -> int:
    text = text.lower()
    return int(text) if text.isdigit() else _NUMBER_WORDS.get(text, 1)


def _key(name: str) -> str:
    """`Crossbowman`, `Stock Exchanges` and `Universities` become `CROSSBOWMAN`, `STOCK_EXCHANGE`
    and `UNIVERSITY`."""
    words = name.strip().upper().replace("-", " ").split()
    if words and words[-1].endswith("IES") and len(words[-1]) > 4:
        words[-1] = words[-1][:-3] + "Y"  # Universities, Factories, Military Academies.
    elif words and words[-1].endswith("S") and not words[-1].endswith(("SS", "US")):
        words[-1] = words[-1][:-1]
    return "_".join(words)


name_key = _key  # For a policy that must match a tech or civic name against a boost's target.

# Improvements the Builder places. A plural or an article is stripped by `_key`.
_IMPROVEMENT_WORDS = {
    "QUARRY": "QUARRY",
    "MINE": "MINE",
    "FARM": "FARM",
    "PASTURE": "PASTURE",
    "LUMBER_MILL": "LUMBER_MILL",
    "IRON_MINE": "IRON_MINE",
    "NITER_MINE": "NITER_MINE",
    "COAL_MINE": "COAL_MINE",
    "PLANTATION": "PLANTATION",
    "CAMP": "CAMP",
    "FISHING_BOAT": "FISHING_BOATS",
}
# Strategic resources whose mine a boost names, as the boost's target: "Build an Iron Mine".
# The game's improvement is a plain mine, so the job's resource tells them apart.
RESOURCE_MINES = {"IRON": "IRON_MINE", "NITER": "NITER_MINE", "COAL": "COAL_MINE"}


_DISTRICT_WORDS = {
    "ENCAMPMENT": "ENCAMPMENT",
    "AQUEDUCT": "AQUEDUCT",
    "NEIGHBORHOOD": "NEIGHBORHOOD",
    "CAMPUS": "CAMPUS",
    "HARBOR": "HARBOR",  # A district, unlike the Shipyard on it.
    "COMMERCIAL_HUB": "COMMERCIAL_HUB",
    "HOLY_SITE": "HOLY_SITE",
    "THEATER_SQUARE": "THEATER_SQUARE",
    "INDUSTRIAL_ZONE": "INDUSTRIAL_ZONE",
}
_UNIT_WORDS = frozenset({"GALLEY", "CARAVEL", "FIELD_CANNON", "IRONCLAD", "TREBUCHET"})

_BUILD = re.compile(r"^Build (a|an|\d+) (.+?)\.?$", re.IGNORECASE)
_DISTRICTS = re.compile(r"^(.+?) districts?$", re.IGNORECASE)
_OWN = re.compile(r"^Own (a|an|\d+) (.+?)\.?$", re.IGNORECASE)
_KILL_WITH = re.compile(r"^Kill a unit with an? (.+?)\.?$", re.IGNORECASE)
_KILL_BARB = re.compile(r"^Kill (\d+) Barbarians?\.?$", re.IGNORECASE)
_GROW_CITY = re.compile(r"^Grow a city to (\d+) population\.?$", re.IGNORECASE)
_GROW_TOTAL = re.compile(r"^Grow your civilization to at least (\d+) population\.?$", re.I)
_MEET = re.compile(r"^Meet (another|\d+) (civilization|city-states?)\.?$", re.IGNORECASE)
_IMPROVE_TILES = re.compile(r"^Improve (\d+) tiles\.?$", re.IGNORECASE)
_IMPROVE_SEA = re.compile(r"^Improve (\d+) sea resources\.?$", re.IGNORECASE)
_HAVE_LAND = re.compile(r"^Have (\d+) land combat units in your military\.?$", re.IGNORECASE)
_HAVE_CIVIC = re.compile(r"^Have (?:the )?(.+?) civic\.?$", re.IGNORECASE)
_RESEARCH = re.compile(r"^Research (?:the )?(.+?)(?: technology)?\.?$", re.IGNORECASE)


def parse_boost(description: str) -> BoostSpec:
    """Classify a boost description. Anything not recognised is `BoostKind.OTHER`."""
    text = (description or "").strip()
    spec = _parse(text)
    return BoostSpec(
        spec.kind, spec.count, spec.target, spec.on_resource, spec.policies, description=text
    )


def _parse(text: str) -> BoostSpec:  # noqa: PLR0911 - one return per recognised shape.
    other = BoostSpec(BoostKind.OTHER)
    if not text:
        return other
    lowered = text.lower().rstrip(".")
    if lowered == "mine a resource":
        return BoostSpec(BoostKind.IMPROVEMENT, 1, "MINE", True, (BUILDER,))
    if lowered == "farm a resource":
        return BoostSpec(BoostKind.IMPROVEMENT, 1, "FARM", True, (BUILDER,))
    if lowered == "build any specialty district":
        return BoostSpec(BoostKind.DISTRICT, 1, ANY, False, (PRODUCTION,))
    if lowered == "found a pantheon":
        return BoostSpec(BoostKind.FOUND, 1, PANTHEON, False, (FAITH,))
    if lowered == "found a religion":
        return BoostSpec(BoostKind.FOUND, 1, RELIGION, False, (FAITH,))
    if lowered == "found a city on the coast":
        return BoostSpec(BoostKind.FOUND, 1, COASTAL_CITY, False, (SETTLING,))
    if lowered == "clear a barbarian outpost":
        return BoostSpec(BoostKind.KILL, 1, BARBARIAN_OUTPOST, False, (MILITARY,))
    if lowered.startswith("build a") and "wonder" in lowered:
        return BoostSpec(BoostKind.WONDER, 1, ANY, False, (PRODUCTION,))
    if m := _IMPROVE_TILES.match(text):
        return BoostSpec(BoostKind.IMPROVEMENT, int(m[1]), ANY, False, (BUILDER,))
    if m := _IMPROVE_SEA.match(text):
        return BoostSpec(BoostKind.IMPROVEMENT, int(m[1]), SEA_RESOURCE, False, (BUILDER,))
    if m := _GROW_TOTAL.match(text):
        return BoostSpec(BoostKind.POPULATION, int(m[1]), TOTAL)
    if m := _GROW_CITY.match(text):
        return BoostSpec(BoostKind.POPULATION, int(m[1]), CITY)
    if m := _KILL_BARB.match(text):
        return BoostSpec(BoostKind.KILL, int(m[1]), BARBARIAN, False, (MILITARY,))
    if m := _KILL_WITH.match(text):
        return BoostSpec(BoostKind.KILL, 1, _key(m[1]), False, (MILITARY, PRODUCTION))
    if m := _MEET.match(text):
        target = CIVILIZATION if m[2].lower() == "civilization" else CITY_STATE
        count = 1 if m[1].lower() == "another" else int(m[1])
        return BoostSpec(BoostKind.MEETING, count, target, False, (EXPLORATION,))
    if m := _HAVE_LAND.match(text):
        return BoostSpec(BoostKind.UNIT, int(m[1]), LAND_COMBAT, False, (PRODUCTION, MILITARY))
    if m := _OWN.match(text):
        return BoostSpec(BoostKind.UNIT, _count(m[1]), _key(m[2]), False, (PRODUCTION,))
    if m := _HAVE_CIVIC.match(text):
        return BoostSpec(BoostKind.ADOPT, 1, _key(m[1]), False, (RESEARCH,))
    if lowered.startswith("adopt a government"):
        return BoostSpec(BoostKind.ADOPT, 1, "GOVERNMENT", False, (RESEARCH,))
    if m := _RESEARCH.match(text):
        return BoostSpec(BoostKind.ADOPT, 1, _key(m[1]), False, (RESEARCH,))
    if lowered == "build ancient walls":
        return BoostSpec(BoostKind.BUILDING, 1, "ANCIENT_WALLS", False, (PRODUCTION,))
    if m := _BUILD.match(text):
        return _parse_build(_count(m[1]), m[2])
    return other


def _parse_build(count: int, what: str) -> BoostSpec:
    other = BoostSpec(BoostKind.OTHER)
    if " and " in what.lower() or " adjacent " in what.lower():
        return other  # A compound or placement condition: not worth chasing.
    key = _key(what)
    if key in _IMPROVEMENT_WORDS:
        return BoostSpec(BoostKind.IMPROVEMENT, count, _IMPROVEMENT_WORDS[key], False, (BUILDER,))
    if key in _DISTRICT_WORDS:
        return BoostSpec(BoostKind.DISTRICT, count, _DISTRICT_WORDS[key], False, (PRODUCTION,))
    if m := _DISTRICTS.match(what):
        name = m[1].strip().lower()
        if name.startswith("different specialty"):
            return BoostSpec(BoostKind.DISTRICT, count, DIFFERENT, False, (PRODUCTION,))
        return BoostSpec(BoostKind.DISTRICT, count, _key(m[1]), False, (PRODUCTION,))
    if key in _UNIT_WORDS:
        return BoostSpec(BoostKind.UNIT, count, key, False, (PRODUCTION,))
    if key and key != "NATIONAL_PARK":
        return BoostSpec(BoostKind.BUILDING, count, key, False, (PRODUCTION,))
    return other
