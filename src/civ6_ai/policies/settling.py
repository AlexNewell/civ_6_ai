"""Exploration and settling: scout routing, settle-site choice and settler escorts.

`SettlingPolicy` is a turn handler (see `orchestrator.Handler`). Each turn it moves every settler
and scout that has moves left, using only the typed views in `civ6_ai.state`. It makes no model
calls, and the same state and settings always give the same choice.

When to found the first city is an open research question (E.5). The rule here is deliberately
simple and is provisional until that study reports. It is controlled by two settings:

- `first_city_delay_turns`: how many turns the first settler may spend walking to a better site.
  The default 0 founds the first city where the settler stands.
- `settle_now_score`: during that window, found at once on any listed site scoring at least this.

Every settler decision is recorded in `SettlingPolicy.decisions` and logged as one JSON line,
with each candidate site's score and every hostile threat in sight, so E.5 can replay a save with
different settings and compare the choices.
"""

import json
import logging
import math
from collections.abc import Collection, Iterable
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Protocol

from civ6_ai.blockers import action_succeeded, move_succeeded
from civ6_ai.policies.expansion import ExpansionState
from civ6_ai.policies.game_data import COMBAT_UNITS
from civ6_ai.state import (
    City,
    Exploration,
    GameReads,
    Threat,
    Unit,
    read_cities,
    read_exploration,
    read_known_cities,
    read_rivals,
    read_threats,
    read_units,
)
from civ6_ai.state.models import RivalCity, SettleSite
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, FogCount, hex_distance

log = logging.getLogger(__name__)

HOSTILE_NEAR_SITE = "hostile unit near the site"
FOG_PENDING = "fog around the site not measured yet"
BELOW_MIN_SCORE = "below minimum site score"
NEEDS_LOOK = "rival side of the site unexplored (needs a look)"
# How much further than the middle of an unexplained border its unseen city is placed.
PHANTOM_PUSH = 2.0

SETTLER = "UNIT_SETTLER"
SCOUT = "UNIT_SCOUT"
# Decisions that leave a settler waiting with nowhere to go this turn (issue 283).
_HELD_ACTIONS = frozenset({"hold", "no_site"})

# Fog rays follow civ6-mcp's strategic map query, which steps along these offsets from a city.
_FOG_RAYS = ((0, -1), (1, -1), (1, 1), (0, 1), (-1, 1), (-1, -1))
_EVEN_ROW_NEIGHBOURS = ((1, 0), (-1, 0), (0, -1), (-1, -1), (0, 1), (-1, 1))
_ODD_ROW_NEIGHBOURS = ((1, 0), (-1, 0), (0, -1), (1, -1), (0, 1), (1, 1))
_SEA_UNITS = (
    "GALLEY",
    "QUADRIREME",
    "CARAVEL",
    "FRIGATE",
    "IRONCLAD",
    "DESTROYER",
    "CRUISER",
    "BATTLESHIP",
    "CARRIER",
    "SUBMARINE",
    "PRIVATEER",
    "SEADOG",
    "CORSAIR",
    "UBOAT",
    "LONGSHIP",
    "BIREME",
)
_MAX_SCOUT_TRIES = 3


@dataclass(frozen=True)
class SettlingSettings:
    """Every tunable of the policy. The defaults are provisional until E.5 reports."""

    # First-city rule.
    first_city_delay_turns: int = 0
    settle_now_score: float = 0.0
    # Later cities.
    min_site_score: float = 50.0
    min_city_distance: int = 4  # The game refuses a city within 3 tiles of another.
    max_city_distance: int = 8  # From the nearest own city, so the empire stays compact.
    # Run 11: the near land was taken and every site left within reach was under a rival's pull,
    # while sites 9 to 14 tiles out (loyalty rate +20, safe +17, score 174) were refused as "too
    # far" and four settlers were disbanded. A site beyond `max_city_distance` and within
    # `far_city_distance` competes, minus `far_penalty` a tile past the normal limit, but only
    # where the safe loyalty rate is at least `far_min_safe_rate` (no rival near).
    far_city_distance: int = 14
    far_penalty: float = 6.0
    far_min_safe_rate: float = 8.0
    travel_turn_cost: float = 6.0  # Score given up for each turn spent walking to a site.
    # Site score. The weights follow the ones civ6-mcp ranks its list with, minus its hidden terms.
    food_weight: float = 2.0
    production_weight: float = 2.0
    luxury_weight: float = 4.0
    strategic_weight: float = 3.0
    defense_weight: float = 1.0
    fresh_water_bonus: float = 5.0
    coast_bonus: float = 3.0
    # Issue 324: site-quality factors the owner named. Measured and logged only: every weight is 0
    # so the chosen site never changes until a later issue uses them. Each acts on its raw value
    # (see `score_site_breakdown`); the negative factors are subtracted.
    inner_ring_weight: float = 0.0  # Per food+production point on the six tiles around the centre.
    outer_ring_weight: float = 0.0  # Per food+production point two and three tiles out.
    water_grade_weight: float = 0.0  # Per grade: fresh 2, coast 1, none 0.
    centre_resource_penalty: float = 0.0  # Off when a luxury or strategic lies under the centre.
    capital_distance_weight: float = 0.0  # Off per tile the capital is from the ideal distance.
    capital_ideal_distance: int = 6  # Close enough to link and defend, far enough to share little.
    overlap_penalty: float = 0.0  # Off per workable tile (radius 3) an own city already covers.
    # Safety.
    threat_radius: int = 3  # A tile this close to a hostile unit is unsafe to stand on.
    flee_radius: int = 2  # A settler this close to a hostile unit runs.
    flee_mounted_extra: int = 2  # A mounted hostile unit is this many tiles further a danger.
    flee_escorted_hostiles: int = 2  # An escort is no cover against this many hostile units near.
    escort_search_radius: int = 5
    scout_threat_radius: int = 2
    # Issue 288: a hostile unit seen near a site and now out of sight has not gone, it is in the
    # fog (the threat map keeps it). A site with a fogged surround (`fog_share_min`) and such a
    # sighting within `threat_radius` of it is walked to with an escort, never alone; the
    # sighting ages out after `recent_sighting_turns`, so a site with no escort free is waited for
    # only while the evidence is fresh. A site near any recent sighting is also not stepped onto
    # with no moves left to found: the settler stops one tile short and goes in next turn.
    recent_sighting_turns: int = 6
    # A city's last defender may escort a settler when the threat near the city is limited.
    # Hostile units within `garrison_threat_radius` of the city (two turns of march) are weighed:
    # at most `garrison_max_hostiles` of them, none a siege unit, none stronger than the city's
    # own defence strength (floored at `garrison_min_strength`, the ancient-era melee value), and
    # together at most `garrison_total_ratio` times that. Never while we are at war with a civ.
    garrison_threat_radius: int = 5
    garrison_max_hostiles: int = 2
    garrison_min_strength: int = 20
    garrison_total_ratio: float = 2.0
    # A settler standing on a good site founds the city at once, with a hostile unit near,
    # when the threat is small: the city can't be taken in a turn, it is usually safe from
    # one or two barbarians, and running costs the momentum of the early game. At most
    # `found_max_hostiles` hostile land units within `found_threat_radius`, none siege, each
    # no stronger than `found_max_strength` and together at most `found_max_total`. Never
    # while we are at war with a civ. More than that, and the settler still runs.
    found_threat_radius: int = 4
    found_max_hostiles: int = 2
    found_max_strength: int = 20
    found_max_total: int = 30
    # Bounds on waiting and retrying, so nothing stalls for good.
    no_site_disband_turns: int = 20  # A settler with no site this many turns in a row is disbanded.
    max_move_failures: int = 3  # Refused moves to one site before it is set aside.
    # Loyalty. A city leans towards the owner of the bigger, nearer cities within `loyalty_radius`.
    loyalty_radius: int = 9
    loyalty_city_base: int = 2  # Added to a city's population, so a small city still pulls.
    loyalty_reject_margin: float = 12.0  # Reject a site whose margin is below minus this.
    loyalty_penalty_weight: float = 1.0  # Score lost per point of margin below zero.
    # The game's own rule, as its settle scan models it: each city within `loyalty_range` pulls
    # with population * (range + 1 - distance) / range, and the rate is ten times the gap over the
    # smaller side, capped at plus or minus `loyalty_rate_cap`. Run 9 flipped three cities that
    # the linear margin above had passed, so the rate gates sites too.
    loyalty_range: int = 9
    loyalty_rate_cap: float = 20.0
    # Capitals pull twice as hard (Run 12 breakdown: our capital, pop 3 at 8 tiles, 1.195 against
    # 0.6 for a plain pop 3 city there; Sumeria's capital, pop 7 at 5 tiles, 7 against 3.5).
    loyalty_capital_weight: float = 2.0
    # A rival-owned revealed tile with no known city of its owner within this many tiles is
    # evidence of an unseen city: count a phantom of `rival_city_default_pop - 2` on it.
    # Run 13: a Mapuche city 5 tiles off "explained" the borders of three cities we never saw, and
    # Uxmal, founded among them, flipped. A city owns tiles out to about 3, so more is another city.
    loyalty_border_explain_radius: int = 3
    loyalty_reject_rate: float = -6.0  # Reject a site whose estimated rate is below this.
    loyalty_rate_penalty_weight: float = 4.0  # Score lost per point of rate below zero.
    # The estimate is a snapshot of a city that does not exist yet. Run 10 founded Palenque at an
    # estimated +2.3 a turn and the game said -27 a turn the next turn. The "safe" rate is the
    # estimate after the three things it leaves out; a site must stay above `loyalty_reject_safe`
    # on it and is marked down for every point under `loyalty_comfort_rate`.
    # 1. Non-pressure losses of a new city: not following the neighbour's religion (-3 in the
    #    game's breakdown), no governor and no happiness bonus yet.
    #    Run 12's breakdown for Tikal (turn 84): happiness -3, religion 0, rival Diplomat
    #    Governors nearby -4; Run 10 had religion -3. Assumed -3 a turn, the happiness term seen
    #    in the game; religion and a rival's Amani (-4) are not known at the site and left out.
    #    Run 13: at -6 with the +4 floor, Run 12's Palenque site (67,21), which held all game,
    #    read safe -1.0 and was refused, and a settler was disbanded with no site for 20 turns.
    loyalty_new_city_drag: float = 3.0
    # 2. Growth: a new city starts at population 1 while rival cities keep growing. Over the first
    #    ~20 turns assume rival cities gain `rival_growth` pop and our existing ones `own_growth`.
    loyalty_rival_growth: int = 2
    loyalty_own_growth: int = 1
    # 3. Fog: a site within range of a seen rival city is in that rival's heartland, where cities
    #    we have not seen are likely. Count one extra rival city, default population less 2, at
    #    `loyalty_fog_distance` tiles. Rival-owned tiles are not in the settle scan, so this is the
    #    only stand-in for them.
    loyalty_fog_distance: int = 8
    # 4. Unexplored rival side (Run 13: Uxmal, safe +4.1, lost to three Mapuche cities that were
    #    never seen, one a capital 6 tiles off). The robust rate also counts one unseen city, as
    #    large as our median city (at least `unseen_pop_min`), at the nearest unrevealed far-side
    #    tile (never closer than `unseen_min_distance`; a city nearer than that shows its border).
    #    A site that stays at or above `loyalty_reject_safe` on it is held by our own pull and
    #    needs no look. Otherwise, if `fog_share_min` of the far-side tiles in range (and at least
    #    `fog_min_tiles`) are unrevealed, it needs a look: not taken while a scout lives to look,
    #    and when none does it competes with `fog_penalty_weight` score per point of shortfall.
    unseen_pop_min: int = 2
    unseen_capital_share: float = 0.5  # This much of the rival side unexplored: a heartland.
    unseen_min_distance: int = 5
    fog_share_min: float = 0.25
    fog_min_tiles: int = 6
    fog_penalty_weight: float = 3.0
    fog_max_sites: int = 12  # Sites whose fog is read each turn (nearest to our cities first).
    fog_own_cities: int = 4  # Our nearest cities that define a site's far side.
    # Run 11's Chichen Itza (safe 1.9 then) was judged with no rival city known at all; with the
    # rival cities, capitals and border phantoms now counted (PRs 212 and 221), that site reads
    # about -10 and Run 12's Tikal about -19, so a floor of 0 refuses both. Run 13: a floor of 4
    # on top of a -6 drag refused Run 12's Palenque site (67,21), which held all game.
    loyalty_reject_safe: float = 0.0  # Reject a site whose safe rate is below this.
    loyalty_comfort_rate: float = 8.0  # A safe rate under this costs score.
    loyalty_safe_penalty_weight: float = 2.0  # Score lost per point of safe rate under comfort.
    rival_city_default_pop: int = 5  # A rival city seen but never with its population read.
    retry_turns: int = 10  # How long a site or frontier that could not be reached is set aside.
    scout_wait_turns: int = 2  # A scout waits this long for a hostile unit on its route, then goes.
    automate_retry_turns: int = 3  # Before an idle scout is handed to the game's explorer again.
    map_width: int | None = STANDARD_MAP_WIDTH


def _unpack(city: tuple) -> tuple[int, int, int, bool]:
    """A city as (x, y, population) or (x, y, population, is_capital)."""
    return city[0], city[1], city[2], len(city) > 3 and bool(city[3])


def loyalty_pressure(
    x: int,
    y: int,
    cities: Iterable[tuple[int, int, int]],
    settings: SettlingSettings,
) -> float:
    """How hard a set of cities, as (x, y, population), pulls on a city at (x, y).

    Each city pulls with its population plus a base, in proportion to how far inside the radius the
    site lies. A city outside the radius pulls nothing.
    """
    total = 0.0
    for city in cities:
        cx, cy, pop, _ = _unpack(city)
        d = hex_distance(x, y, cx, cy, settings.map_width)
        if d <= settings.loyalty_radius:
            total += (pop + settings.loyalty_city_base) * (settings.loyalty_radius + 1 - d)
    return total


def loyalty_margin(
    x: int,
    y: int,
    own: Iterable[tuple[int, int, int]],
    rivals: Iterable[tuple[int, int, int]],
    settings: SettlingSettings,
) -> float:
    """Our pull on a site minus the rivals'. Below zero the new city leans towards a rival.

    Live: cities 9 to 12 tiles from the capital and next to a rival flipped 6 to 9 turns after a
    loyalty warning (Runs 3 to 5), and Run 7 lost Calakmul 4 turns after founding. Only cities we
    can see are counted, so an unseen rival makes the margin look better than it is.
    """
    return loyalty_pressure(x, y, own, settings) - loyalty_pressure(x, y, rivals, settings)


def loyalty_rate(
    x: int,
    y: int,
    own: Iterable[tuple[int, int, int]],
    rivals: Iterable[tuple[int, int, int]],
    settings: SettlingSettings,
) -> float:
    """Estimated loyalty per turn of a city at (x, y), from the cities pulling on it.

    The shape matters more than the weights. A site is safe only while our side outweighs the
    rivals', and the rate saturates: Run 9's Chichen Itza showed minus 24 a turn, which no linear
    margin could say. The 1.0 on our side is the site's own pull, as in the game's scan.
    """
    reach = settings.loyalty_range
    capital_weight = settings.loyalty_capital_weight

    def pull(cities: Iterable[tuple[int, int, int]]) -> float:
        total = 0.0
        for city in cities:
            cx, cy, pop, cap = _unpack(city)
            d = hex_distance(x, y, cx, cy, settings.map_width)
            if 0 < d <= reach:
                total += pop * (reach + 1 - d) / (reach + 1) * (capital_weight if cap else 1.0)
        return total

    friendly, enemy = 1.0 + pull(own), pull(rivals)
    rate = 10.0 * (friendly - enemy) / (min(friendly, enemy) + 0.5)
    cap = settings.loyalty_rate_cap
    return max(-cap, min(cap, rate))


def safe_loyalty_rate(
    x: int,
    y: int,
    own: Iterable[tuple[int, int, int]],
    rivals: Iterable[tuple[int, int, int]],
    settings: SettlingSettings,
    unseen_distance: int | None = None,
    unseen_pop: int | None = None,
    unseen_capital: bool = False,
) -> float:
    """The loyalty estimate after growth, fog and the non-pressure drag. See the settings.

    With `unseen_distance` it is the robust rate: one more rival city, as large as our median city,
    is assumed that far off."""
    s = settings
    rivals = list(rivals)
    grown_own = [
        (cx, cy, pop + s.loyalty_own_growth, cap) for cx, cy, pop, cap in map(_unpack, own)
    ]
    grown_rivals = [
        (cx, cy, pop + s.loyalty_rival_growth, cap) for cx, cy, pop, cap in map(_unpack, rivals)
    ]
    if unseen_distance is not None:
        # The pessimistic stand-in for what the fog may hide, straight east at that distance.
        # Its size follows the game: as large as our median city (never under `unseen_pop_min`),
        # and not a capital. A fixed pop-5 capital (as heavy as pop 10) flagged every early site
        # near a pop-2 or pop-3 capital, when every city on the map is that small.
        if unseen_pop is None:
            pops = sorted(pop for _, _, pop, _ in map(_unpack, own)) or [s.unseen_pop_min]
            unseen_pop = pops[len(pops) // 2]
        unseen = max(s.unseen_pop_min, unseen_pop)
        px = (x + max(unseen_distance, s.unseen_min_distance)) % (s.map_width or 10_000)
        grown_rivals.append((px, y, unseen + s.loyalty_rival_growth, unseen_capital))
    if any(
        0 < hex_distance(x, y, cx, cy, s.map_width) <= s.loyalty_range
        for cx, cy, _, _ in map(_unpack, rivals)
    ):
        # A phantom city straight east on the same row, at the assumed distance. Only the
        # distance matters to the pull, and wrapping is handled by hex_distance's width.
        px = (x + s.loyalty_fog_distance) % (s.map_width or 10_000)
        grown_rivals.append((px, y, s.rival_city_default_pop - 2))
    return loyalty_rate(x, y, grown_own, grown_rivals, s) - s.loyalty_new_city_drag


def score_site(site: SettleSite, settings: SettlingSettings) -> float:
    """Value a site from the visible fields only. The upstream score is never read."""
    score = (
        site.food * settings.food_weight
        + site.production * settings.production_weight
        + site.luxuries * settings.luxury_weight
        + site.strategics * settings.strategic_weight
        + site.defense * settings.defense_weight
    )
    if site.water == "fresh":
        score += settings.fresh_water_bonus
    elif site.water == "coast":
        score += settings.coast_bonus
    return score


SITE_RADIUS = 3  # A city works tiles up to three away.


def _overlap_tiles(
    site: SettleSite, cities: Iterable[tuple[int, int]], map_width: int | None
) -> int:
    """Tiles within `SITE_RADIUS` of the site and of at least one of `cities`."""
    centres = list(cities)
    count = 0
    for dy in range(-SITE_RADIUS, SITE_RADIUS + 1):
        for dx in range(-SITE_RADIUS, SITE_RADIUS + 1):
            x, y = site.x + dx, site.y + dy
            if hex_distance(site.x, site.y, x, y, None) > SITE_RADIUS:
                continue
            if any(hex_distance(x, y, cx, cy, map_width) <= SITE_RADIUS for cx, cy in centres):
                count += 1
    return count


def score_site_breakdown(
    site: SettleSite,
    settings: SettlingSettings,
    own_cities: Iterable[tuple[int, int]] = (),
    capital: tuple[int, int] | None = None,
) -> dict[str, tuple[float, float]]:
    """Each component of a site's value as `name: (raw, weighted)`, in a fixed order.

    The first components are exactly `score_site`'s terms, so the weighted values of those sum to
    it; the later ones (issue 324) have weight 0 by default and add nothing until set. A factor
    whose data is missing from the site reads raw 0. `own_cities` and `capital` are (x, y).
    """
    s = settings
    water_raw = {"fresh": 2.0, "coast": 1.0}.get(site.water, 0.0)
    water_bonus = {"fresh": s.fresh_water_bonus, "coast": s.coast_bonus}.get(site.water, 0.0)
    cities = list(own_cities)
    capital_gap = 0.0
    if capital is not None:
        capital_gap = float(hex_distance(site.x, site.y, *capital, s.map_width))
    under_centre = 1.0 if (site.centre_resource or "")[:2] in ("S:", "L:") else 0.0
    overlap = float(_overlap_tiles(site, cities, s.map_width)) if cities else 0.0
    parts = {
        "food": (site.food, site.food * s.food_weight),
        "production": (site.production, site.production * s.production_weight),
        "luxuries": (site.luxuries, site.luxuries * s.luxury_weight),
        "strategics": (site.strategics, site.strategics * s.strategic_weight),
        "defense": (site.defense, site.defense * s.defense_weight),
        "water": (water_raw, water_bonus),
        "inner_ring": (site.inner_yield or 0, (site.inner_yield or 0) * s.inner_ring_weight),
        "outer_ring": (site.outer_yield or 0, (site.outer_yield or 0) * s.outer_ring_weight),
        "water_grade": (water_raw, water_raw * s.water_grade_weight),
        "centre_resource": (under_centre, -under_centre * s.centre_resource_penalty),
        "capital_distance": (
            capital_gap,
            -abs(capital_gap - s.capital_ideal_distance) * s.capital_distance_weight
            if capital is not None
            else 0.0,
        ),
        "overlap": (overlap, -overlap * s.overlap_penalty),
    }
    return {name: (float(raw), float(weighted)) for name, (raw, weighted) in parts.items()}


def breakdown_total(breakdown: dict[str, tuple[float, float]]) -> float:
    """The weighted sum, which equals `score_site` while the new weights are 0."""
    total = 0.0
    for _, weighted in breakdown.values():
        total += weighted
    return total


@dataclass(frozen=True)
class CandidateLog:
    x: int
    y: int
    site_score: float
    distance: int
    turns: int
    net_score: float
    rejected: str | None  # None means the site can be chosen.
    path_threat: bool
    loyalty_margin: float = 0.0  # Our pull minus rivals', at the site. Below zero is a flip risk.
    loyalty_penalty: float = 0.0  # Taken off the site score.
    loyalty_rate: float = 0.0  # Estimated loyalty per turn at the site. Below zero it falls.
    safe_rate: float = 0.0  # The same after growth, fog and new-city drag.
    robust_rate: float = 0.0  # The same with an unseen rival capital at the nearest fogged tile.
    fog_share: float = 0.0  # Unrevealed share of the far-side tiles within loyalty range.
    recent_near: bool = False  # A hostile unit, now unseen, was recently seen near the site.
    fog_risk: bool = False  # Fogged surround and a recent sighting near: needs an escort.
    # Issue 324: the site score's components as name -> [raw, weighted], before loyalty penalties.
    breakdown: dict[str, tuple[float, float]] = field(default_factory=dict)
    # The site-quality score alone: no loyalty, fog or far-city penalty (`site_score` has them).
    quality_score: float = 0.0


@dataclass(frozen=True)
class ThreatLog:
    unit_type: str
    owner_id: int
    x: int
    y: int
    distance_to_settler: int


@dataclass(frozen=True)
class SettleDecision:
    """One settler's choice, with the evidence for it. Written for E.5 to replay."""

    turn: int
    unit_id: int
    x: int
    y: int
    action: str  # found, found_failed, move, escort_move, escort_gather, hold, flee, no_site
    reason: str
    first_city: bool
    cities: int
    waited_turns: int
    delay_turns: int
    settle_now_score: float
    here_score: float | None  # None when the tile is not on the advisor's list.
    target: tuple[int, int] | None = None
    result: str = ""
    candidates: tuple[CandidateLog, ...] = ()
    threats: tuple[ThreatLog, ...] = ()
    # The cities the loyalty estimate used, as (x, y, population): ours, and every rival city
    # remembered. Logged so a city that flips can be checked against what the estimate knew.
    own_cities: tuple[tuple[int, int, int], ...] = ()
    rival_cities: tuple[tuple[int, int, int], ...] = ()
    phantom_cities: tuple[tuple[int, int, int], ...] = ()  # Border evidence, not seen cities.


class SettlingGame(GameReads, Protocol):
    """The reads plus the three actions this policy takes."""

    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str: ...

    async def found_city(self, unit_index: int) -> str: ...

    async def automate_explore(self, unit_index: int) -> str: ...


def _land_military(unit: Unit) -> bool:
    """A land combat unit: never a scout, ship or aircraft (military.py defines it)."""
    from civ6_ai.policies.military import is_land_military  # military imports this module

    return is_land_military(unit)


def _holds_tile(unit: Unit) -> bool:
    """A unit the game will not stack with a military unit, engineers included."""
    from civ6_ai.policies.military import holds_tile

    return holds_tile(unit)


def _strength(threat: Threat) -> tuple[float, bool]:
    """A hostile unit's best attack value (game data first, the reading otherwise), and siege."""
    stats = COMBAT_UNITS.get(threat.unit_type)
    if stats is not None:
        return float(max(stats[0], stats[1])), stats[4] == "SIEGE" or stats[2] > 0
    return float(max(threat.combat_strength, threat.ranged_strength)), False


def escort_holds_tile(
    escort: Unit, tile: tuple[int, int], threats: Iterable[Threat], s: "SettlingSettings"
) -> tuple[bool, float, int]:
    """Whether `escort` plausibly holds `tile` for a turn, as (holds, damage taken, attackers).

    Issue 291: two settlers were taken after an escort led them to a tile hostile Coursers could
    reach. Every hostile unit that can reach the tile (a mounted one from further) attacks once
    with its own strength, and the escort defends with its melee strength scaled by its health
    and `assumed_defender_bonus` for terrain. Damage is the military policy's `expected_damage`,
    with its variance on the bad side. The escort holds when it survives the total.
    """
    from civ6_ai.policies.military import MilitaryWeights, expected_damage

    w = MilitaryWeights()
    defence = escort.combat_strength * escort.hp / max(escort.max_hp, 1) + w.assumed_defender_bonus
    taken = 0.0
    attackers = 0
    for t in threats:
        reach = s.flee_radius + (
            s.flee_mounted_extra if any(m in t.unit_type for m in _MOUNTED) else 0
        )
        if not t.hostile or hex_distance(tile[0], tile[1], t.x, t.y, s.map_width) > reach:
            continue
        attack = _strength(t)[0] * t.hp / max(t.max_hp, 1)
        if attack <= 0:
            continue
        attackers += 1
        taken += expected_damage(attack, defence) * (1 + w.damage_variance)
    return taken < escort.hp, taken, attackers


def garrison_may_leave(
    city: City,
    threats: Iterable[Threat],
    at_war: bool,
    settings: SettlingSettings | None = None,
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> bool:
    """True when `city` can stand without its last defender: the hostile units near it are few,
    weak and not siege. Run 12, turns 29-40: two slingers, two warriors and a scout sat beside the
    capital, so it must stay; turns 49-51: one scout, so the garrison may lead the settler out.
    """
    if at_war:
        return False
    s = settings or SettlingSettings()
    near = [
        t
        for t in threats
        if t.hostile
        and hex_distance(city.x, city.y, t.x, t.y, map_width) <= s.garrison_threat_radius
    ]
    if len(near) > s.garrison_max_hostiles:
        return False
    ceiling = float(max(city.defense_strength, s.garrison_min_strength))
    total = 0.0
    for t in near:
        strength, siege = _strength(t)
        if siege or strength > ceiling:
            return False
        total += strength
    return total <= s.garrison_total_ratio * ceiling


def found_under_threat_ok(
    site: tuple[int, int],
    threats: Iterable[Threat],
    at_war: bool,
    settings: SettlingSettings | None = None,
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> bool:
    """True when a settler on `site` may found its city now though hostile units are near:
    few, weak and not siege (see `found_threat_radius` in the settings)."""
    if at_war:
        return False
    s = settings or SettlingSettings()
    near = [
        t
        for t in threats
        if t.hostile and hex_distance(*site, t.x, t.y, map_width) <= s.found_threat_radius
    ]
    if len(near) > s.found_max_hostiles:
        return False
    total = 0.0
    for t in near:
        strength, siege = _strength(t)
        if siege or strength > s.found_max_strength:
            return False
        total += strength
    return total <= s.found_max_total


def spare_garrison_tiles(
    cities: Iterable[City],
    threats: Iterable[Threat],
    at_war: bool,
    settings: SettlingSettings | None = None,
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> frozenset[tuple[int, int]]:
    """Tiles of the cities whose last defender may leave (see `garrison_may_leave`)."""
    threats = tuple(threats)
    return frozenset(
        (c.x, c.y) for c in cities if garrison_may_leave(c, threats, at_war, settings, map_width)
    )


def find_escort(
    settler: Unit,
    units: Iterable[Unit],
    city_tiles: Iterable[tuple[int, int]],
    claimed: Collection[int],
    radius: int,
    map_width: int | None = STANDARD_MAP_WIDTH,
    lone_ok: Collection[tuple[int, int]] = (),
) -> Unit | None:
    """The nearest combat unit that can leave to escort `settler`, or None.

    Never a scout, a ship, a unit in `claimed`, or the only combat unit in a city, unless that
    city is in `lone_ok` (its threat is limited). A unit that leaves a spare is preferred.
    """
    city_tiles = set(city_tiles)
    combat = [u for u in units if _land_military(u) and u.can_move and u.unit_id not in claimed]
    garrisons: dict[tuple[int, int], int] = {}
    for u in combat:
        garrisons[u.x, u.y] = garrisons.get((u.x, u.y), 0) + 1
    usable = [
        u
        for u in combat
        if _land_military(u)
        and not any(word in u.unit_type for word in _SEA_UNITS)
        and not (
            (u.x, u.y) in city_tiles and garrisons[u.x, u.y] == 1 and (u.x, u.y) not in lone_ok
        )
        and hex_distance(u.x, u.y, settler.x, settler.y, map_width) <= radius
    ]

    def strips(u: Unit) -> bool:
        return (u.x, u.y) in city_tiles and garrisons[u.x, u.y] == 1

    usable.sort(
        key=lambda u: (
            strips(u),
            hex_distance(u.x, u.y, settler.x, settler.y, map_width),
            u.unit_id,
        )
    )
    return usable[0] if usable else None


def units_reserved_for_settling(
    units: Iterable[Unit],
    city_tiles: Iterable[tuple[int, int]],
    radius: int = SettlingSettings.escort_search_radius,
    map_width: int | None = STANDARD_MAP_WIDTH,
    lone_ok: Collection[tuple[int, int]] = (),
) -> frozenset[int]:
    """Ids of the units settling may use: scouts, and the escort each settler would take.

    Other policies must leave these alone. A settler counts whether or not it has moves left,
    because settling may already have moved it and its escort this turn. Every combat unit that
    shares a tile with a settler outside a city is an escort on the road, so it is reserved too.
    This is the one definition of who an escort is, and `SettlingPolicy` picks with the same
    `find_escort`, so the two can't drift apart.
    """
    units = list(units)
    city_tiles = list(city_tiles)
    reserved = {u.unit_id for u in units if u.unit_type == SCOUT}
    settlers = sorted((u for u in units if u.unit_type == SETTLER), key=lambda u: u.unit_id)
    settler_tiles = {(s.x, s.y) for s in settlers} - set(city_tiles)
    escorts = [u for u in units if u.is_combat and (u.x, u.y) in settler_tiles]
    reserved |= {u.unit_id for u in escorts}
    on_road = {(u.x, u.y) for u in escorts if u.unit_type != SCOUT}
    for settler in settlers:
        if (settler.x, settler.y) in on_road:
            continue  # Already travelling with an escort, as `_settle` also reads it.
        escort = find_escort(settler, units, city_tiles, reserved, radius, map_width, lone_ok)
        if escort is not None:
            reserved.add(escort.unit_id)
    return frozenset(reserved)


def unit_at(x: int, y: int) -> Unit:
    """A stand-in settler at a tile, for asking where sites and escorts are from there."""
    return Unit(0, 0, SETTLER, "", x, y, 2.0, 2.0, 100, 100)


def neighbours(x: int, y: int, map_width: int | None = STANDARD_MAP_WIDTH) -> list[tuple[int, int]]:
    offsets = _ODD_ROW_NEIGHBOURS if y & 1 else _EVEN_ROW_NEIGHBOURS
    found = [(x + dx, y + dy) for dx, dy in offsets]
    return [(nx % map_width if map_width else nx, ny) for nx, ny in found if ny >= 0]


def hex_line(
    start: tuple[int, int], end: tuple[int, int], map_width: int | None = STANDARD_MAP_WIDTH
) -> list[tuple[int, int]]:
    """The tiles from just after `start` up to and including `end`."""
    x1, y1 = start
    x2, y2 = end
    if map_width:  # Walk the short way round the east-west wrap.
        x2 = min((x2 + s * map_width for s in (-1, 0, 1)), key=lambda x: abs(x - x1))
    steps = hex_distance(x1, y1, x2, y2, None)
    if steps == 0:
        return []

    def axial(x: int, y: int) -> tuple[int, int]:
        return x - (y - (y & 1)) // 2, y

    q1, r1 = axial(x1, y1)
    q2, r2 = axial(x2, y2)
    line = []
    for i in range(1, steps + 1):
        t = i / steps
        q, r = _round_axial(q1 + (q2 - q1) * t, r1 + (r2 - r1) * t)
        x = q + (r - (r & 1)) // 2
        line.append((x % map_width if map_width else x, r))
    return line


def _round_axial(q: float, r: float) -> tuple[int, int]:
    s = -q - r
    rq, rr, rs = round(q), round(r), round(s)
    dq, dr, ds = abs(rq - q), abs(rr - r), abs(rs - s)
    if dq > dr and dq > ds:
        rq = -rr - rs
    elif dr > ds:
        rr = -rq - rs
    return rq, rr


def frontier_points(
    exploration: Exploration, map_width: int | None = STANDARD_MAP_WIDTH
) -> list[tuple[int, int]]:
    """Revealed tiles on the edge of the fog, one per ray from each city, in a fixed order."""
    points: list[tuple[int, int]] = []
    for edge in exploration.fog_edges:
        for (dx, dy), distance in zip(_FOG_RAYS, edge.distances, strict=False):
            if distance <= 0:  # -1 means the whole ray is already revealed.
                continue
            x, y = edge.x + dx * (distance - 1), edge.y + dy * (distance - 1)
            if y < 0:
                continue
            point = (x % map_width if map_width else x, y)
            if point not in points:
                points.append(point)
    return points


_MOUNTED = ("HORSE", "COURSER", "KNIGHT", "CAVALRY", "CHARIOT", "CAMEL", "ELEPHANT")


@dataclass
class _Danger:
    """Hostile units in sight, and tests for tiles and routes near them."""

    threats: tuple[Threat, ...]
    map_width: int | None
    # (x, y) of hostile units seen within the last few turns and now out of sight.
    recent: tuple[tuple[int, int], ...] = ()

    def recent_near(self, x: int, y: int, radius: int) -> bool:
        return any(hex_distance(x, y, rx, ry, self.map_width) <= radius for rx, ry in self.recent)

    def near(self, x: int, y: int, radius: int) -> bool:
        return any(
            hex_distance(x, y, t.x, t.y, self.map_width) <= radius
            for t in self.threats
            if t.hostile
        )

    def count_near(self, x: int, y: int, radius: int, mounted_extra: int = 0) -> int:
        """Hostile units that can be on the tile next turn: a mounted one reaches further."""
        return sum(
            1
            for t in self.threats
            if t.hostile
            and hex_distance(x, y, t.x, t.y, self.map_width)
            <= radius + (mounted_extra if any(w in t.unit_type for w in _MOUNTED) else 0)
        )

    def on_path(self, start: tuple[int, int], end: tuple[int, int], radius: int) -> bool:
        line = hex_line(start, end, self.map_width)
        return any(self.near(x, y, radius) for x, y in line)


@dataclass
class SettlingPolicy:
    """Moves settlers and scouts. Run it before the blocker resolver."""

    settings: SettlingSettings = field(default_factory=SettlingSettings)
    name: str = "settling"
    decisions: list[SettleDecision] = field(default_factory=list)
    _first_turn: int | None = field(default=None, init=False, repr=False)
    # Tiles where founding was refused. The tile itself is illegal, so this is kept for good.
    _failed_sites: set[tuple[int, int]] = field(default_factory=set, init=False, repr=False)
    # Refused moves are often temporary, so they only set a site or frontier aside for a while.
    _move_failures: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _set_aside: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _bad_frontiers: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _site_claims: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _holds: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    # Escort orders the game refused, as (escort, from, to, military units on `to`): not sent again
    # until one of those changes.
    _refused: set = field(default_factory=set, init=False, repr=False)
    # Issue 368: escorted steps the game refused, as (settler id, settler tile, step). Keyed on the
    # settler's tile, so it lapses on its own once the pair moves; the next pick avoids the tile.
    _refused_steps: set = field(default_factory=set, init=False, repr=False)
    # The threat state, or None to play without it (a war is then unknown, so no lone garrison
    # leaves). `_lone_ok` is this turn's cities whose last defender may leave; `_stripped` maps a
    # city tile whose last defender left with a settler to that escort's id.
    defence: Any = field(default=None, repr=False)
    # The terrain store (issue 238), set by the bot's wiring: it says what a step costs, so a
    # settler one tile off a good site can step on and found in the same turn.
    terrain: Any = field(default=None, repr=False)
    # The threat map (issue 288): it remembers hostile units that have gone out of sight.
    threat_map: Any = field(default=None, repr=False)
    _lone_ok: frozenset = field(default_factory=frozenset, init=False, repr=False)
    # Settlers that fled a hostile unit (issue 291), with the site they were heading for: that
    # route is not resumed while a hostile unit is near it.
    _fled: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _stripped: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _automated: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _scout_targets: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _scout_waits: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    # Shared with production and the expansion log. `run` fills it when it surveys the map.
    expansion: ExpansionState = field(default_factory=ExpansionState)
    _own_cities: list[tuple[int, int, int]] = field(default_factory=list, init=False, repr=False)
    # Settlers that founded a city this turn (so they are no longer on the map), and how many turns
    # in a row each has found no site.
    _founded: set[int] = field(default_factory=set, init=False, repr=False)
    _held_now: set[int] = field(default_factory=set, init=False, repr=False)
    _no_site_turns: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _rival_cities: list[tuple[int, int, int]] = field(default_factory=list, init=False, repr=False)
    # Every rival city seen on a visible tile, by tile, with its last seen population. The game
    # only lists a city while its tile is visible, so the estimate used to swing with the fog:
    # Run 9's Chichen Itza site read 82, 16, 92, 96 and 14 on successive turns.
    _rival_memory: dict[tuple[int, int], tuple[int, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _rival_capitals: set[tuple[int, int]] = field(default_factory=set, init=False, repr=False)
    # Owners seen to be city-states. Their cities count for spacing, never for loyalty pressure.
    _minor_owners: set[int] = field(default_factory=set, init=False, repr=False)
    # Foreign-owned tiles seen, by tile to owner; the evidence behind border phantoms.
    _border_memory: dict[tuple[int, int], int] = field(default_factory=dict, init=False, repr=False)
    _phantoms: list[tuple[int, int, int]] = field(default_factory=list, init=False, repr=False)
    # Unrevealed far-side tiles around the candidate sites (last read), by site; and whether a scout
    # lives to look. Both are re-read every turn, so a site is judged again when the fog moves.
    _fog: dict[tuple[int, int], FogCount] = field(default_factory=dict, init=False, repr=False)
    _fog_sites: list[tuple[int, ...]] = field(default_factory=list, init=False, repr=False)
    _scout_alive: bool = field(default=False, init=False, repr=False)
    # Sites the last scan found needing a look at their rival side (marked down, never refused).
    _needs_look: set[tuple[int, int]] = field(default_factory=set, init=False, repr=False)
    # Whether the fog can be measured this turn (the game runs Lua and the read has not failed).
    # While it can, a site with no measurement yet is not founded on: it is asked for by the
    # survey and judged the turn its counts arrive.
    _fog_can: bool = field(default=False, init=False, repr=False)
    # Where the unrevealed far side of each needs-a-look site lies, best site first: scouts head
    # for these ahead of the plain frontier.
    _look_points: list[tuple[int, int]] = field(default_factory=list, init=False, repr=False)

    async def run(self, game: SettlingGame) -> None:
        units = await read_units(game)
        settlers = [u for u in units if u.unit_type == SETTLER and u.can_move]
        scouts = [u for u in units if u.unit_type == SCOUT and u.can_move]
        self._founded = set()
        self._held_now = set()
        self.expansion.held_settlers = 0
        self._scout_alive = any(u.unit_type == SCOUT for u in units)
        self._fog_can = getattr(game, "execute_lua", None) is not None
        # Run 8: the count stayed at 1 from the turn a settler founded its city to the end of the
        # game, because it was only taken on turns with a settler to move. It is free here.
        self.expansion.settlers_on_map = sum(1 for u in units if u.unit_type == SETTLER)
        # Sightings are recorded every turn, so a rival city seen while no settler could move is
        # still remembered when one can.
        cities = await read_cities(game)
        below_target = bool(cities) and len(cities) < self.expansion.target_cities
        # The revealed-city read is one Lua call, made only when a settler or the survey needs it.
        rival_cities = await self._remember_rivals(
            game, cities, need_known=bool(settlers) or below_target
        )
        # With no settler and no scout the survey used to go stale, and production read a stale
        # survey as "sites unknown, ok" and built settlers for a map with no site (Run 11: four
        # settlers built, none used). Survey every turn while below the city target; it is
        # re-checked as the map changes, not on a timer.
        if not settlers and not scouts and not below_target:
            return
        turn = (await game.get_game_overview()).turn
        try:
            threats = await read_threats(game)
            exploration = await read_exploration(game)
        except Exception:
            if settlers or scouts:
                raise
            log.warning("Survey read failed; production keeps its last survey", exc_info=True)
            return
        danger = _Danger(threats, self.settings.map_width, self._recent_sightings(turn))
        city_tiles = [(c.x, c.y) for c in cities]
        self._lone_ok = (
            spare_garrison_tiles(
                cities, threats, self.defence.at_war, self.settings, self.settings.map_width
            )
            if self.defence is not None
            else frozenset()
        )
        self._tidy_stripped(units)
        self._own_cities = [
            (c.x, c.y, c.pop, True) if c.is_capital else (c.x, c.y, c.pop) for c in cities
        ]
        claimed: set[int] = set()
        self._forget_gone_units(units, turn)
        if settlers:
            rival_tiles = [(city.x, city.y) for city in rival_cities]
            if self._first_turn is None and not cities:
                self._first_turn = turn
            for settler in settlers:
                await self._settle(
                    game,
                    settler,
                    units,
                    city_tiles,
                    rival_tiles,
                    exploration,
                    danger,
                    turn,
                    claimed,
                )
        self.expansion.held_settlers = len(self._held_now)
        self._survey(settlers, units, exploration, city_tiles, danger, turn)
        for rank, scout in enumerate(sorted(scouts, key=lambda u: u.unit_id)):
            look = self._look_points[rank % len(self._look_points)] if self._look_points else None
            s_width = self.settings.map_width
            if look is None and rank == 0 and self.threat_map is not None:
                # Issue 293: a camp the threat map infers but nobody has seen is worth a look.
                suspect = getattr(self.threat_map, "suspected_camp", lambda: None)()
                if suspect is not None:
                    # Stop two tiles short: it looks, and never walks onto the camp.
                    line = hex_line((scout.x, scout.y), (suspect.x, suspect.y), s_width)
                    short = [
                        p for p in line if hex_distance(*p, suspect.x, suspect.y, s_width) >= 2
                    ]
                    look = short[-1] if short else None
                    log.info("scout %s sent toward the suspected camp at %s", scout.unit_id, look)
            await self._explore(game, scout, exploration, danger, turn, look)

    def _recent_sightings(self, turn: int) -> tuple[tuple[int, int], ...]:
        """Where hostile land units were last seen in recent turns and are not in sight now."""
        if self.threat_map is None:
            return ()
        window = self.settings.recent_sighting_turns
        return tuple(
            (src.x, src.y)
            for src in self.threat_map.sources
            if src.kind == "unit"
            and not src.naval
            and src.age(turn) is not None
            and 1 <= src.age(turn) <= window
        )

    async def _remember_rivals(
        self, game: SettlingGame, cities: Iterable[City], need_known: bool = True
    ) -> list[RivalCity]:
        """Update the rival city memory from what is visible now, and return those sightings.

        A tile now holding a city of ours is dropped (it flipped to us), and a sighting under a
        different owner replaces the old entry (it changed hands).
        """
        seen = []
        for rival in await read_rivals(game):
            for city in rival.visible_cities:
                self._rival_memory[city.x, city.y] = (rival.player_id, city.pop)
                seen.append(city)
        for city in cities:
            self._rival_memory.pop((city.x, city.y), None)
        own_tiles = {(city.x, city.y) for city in cities}
        known = await self._read_known(game, need_known)
        if known is not None:  # A failed read leaves the memory as it was.
            self._merge_known(known, own_tiles)
            if self._fog_sites:
                self._fog = {(f.x, f.y): f for f in known.fog}
            self._phantoms = self._phantom_cities(own_tiles)
        self._rival_cities = [
            (x, y, pop, True) if (x, y) in self._rival_capitals else (x, y, pop)
            for (x, y), (_, pop) in self._rival_memory.items()
        ]
        return seen

    def foreign_city_tiles(self) -> list[tuple[int, int]]:
        """Every remembered city that is not ours, rival and city-state alike, as (x, y).

        Unlike `_pressure_sources` this keeps the city-states (their borders take tiles too, though
        they exert no loyalty pressure) and leaves out the border phantoms (not real cities)."""
        return sorted(self._rival_memory)

    def _pressure_sources(self) -> list[tuple]:
        """Rival cities for the loyalty estimate: the remembered ones plus border phantoms.
        Phantoms are evidence, not cities: spacing and the log use `_rival_cities` alone.

        City-state cities are left out: the game's loyalty pressure comes from civilizations'
        cities only (issue 314: a new city 4 tiles from a pop-5 city-state capital held +21 a
        turn, and counting that city as a rival refused every site on the map)."""
        return [
            city
            for city in self._rival_cities
            if self._rival_memory.get((city[0], city[1]), (None,))[0] not in self._minor_owners
        ] + self._phantoms

    async def _read_known(self, game: SettlingGame, needed: bool):
        """Cities and borders on tiles visible now, in one Lua call; None if skipped or failed.

        Fair play: a rival city enters memory only when its plot is visible to us (on any turn);
        it stays while remembered, is updated (pop, capital, owner) only when visible again, and
        is removed only when its plot is seen visible without it. Borders follow the same rule.
        """
        execute = getattr(game, "execute_lua", None)
        if not needed or execute is None:
            return None
        try:
            return await read_known_cities(
                game,
                [*self._rival_memory, *self._border_memory],
                self._fog_sites,
                self.settings.loyalty_range,
            )
        except Exception:
            log.warning("Known-city read failed; keeping the memory as it was", exc_info=True)
            self._fog_can = False  # Unmeasurable this turn: sites are not held back for it.
            return None

    def _merge_known(self, known, own_tiles: set[tuple[int, int]]) -> None:
        self._minor_owners |= known.minors
        city_tiles = set()
        for city in known.cities:
            if (city.x, city.y) in own_tiles:
                continue
            city_tiles.add((city.x, city.y))
            self._rival_memory[city.x, city.y] = (city.owner, city.pop)
            if city.capital:
                self._rival_capitals.add((city.x, city.y))
            else:
                self._rival_capitals.discard((city.x, city.y))
        for tile in list(self._rival_memory):
            if tile in known.visible_watched and tile not in city_tiles:
                del self._rival_memory[tile]  # Seen, and no city there.
                self._rival_capitals.discard(tile)
        current = {(x, y): owner for owner, x, y in known.border_tiles}
        for tile in list(self._border_memory):
            if tile in known.visible_watched and tile not in current:
                del self._border_memory[tile]
        self._border_memory.update(current)

    def _phantom_cities(self, own_tiles: set[tuple[int, int]]) -> list[tuple[int, int, int]]:
        """Remembered foreign-owned tiles with none of their owner's known cities near are the
        border of a city we have not seen: stand one in for each such cluster (loyalty estimate
        only), where the city most likely is.

        A border is seen from our side, so its tiles lie between us and the city. Run 13: a
        phantom on the nearest border tile counted a pop-3 city 3 or 4 tiles from every early
        site, and all ten were refused while the real city was 7 tiles away. The stand-in goes
        to the middle of the cluster's tiles, moved `PHANTOM_PUSH` tiles further from our
        nearest city.
        """
        s = self.settings
        by_owner: dict[int, list[tuple[int, int]]] = {}
        for (x, y), (owner, _) in self._rival_memory.items():
            by_owner.setdefault(owner, []).append((x, y))
        radius = s.loyalty_border_explain_radius
        clusters: list[tuple[int, list[tuple[int, int]]]] = []
        for (x, y), owner in sorted(self._border_memory.items()):
            if (x, y) in own_tiles or owner in self._minor_owners:
                continue
            cities = by_owner.get(owner, [])
            if any(hex_distance(x, y, cx, cy, s.map_width) <= radius for cx, cy in cities):
                continue
            for o, tiles in clusters:
                if o == owner and hex_distance(x, y, *tiles[0], s.map_width) <= radius:
                    tiles.append((x, y))
                    break
            else:
                clusters.append((owner, [(x, y)]))
        width = s.map_width

        def near(x: float, ref: float) -> float:
            """`x` moved by whole map widths to the copy nearest `ref` (the map wraps east-west)."""
            if not width:
                return x
            return x - width * round((x - ref) / width)

        placed = []
        for _, tiles in clusters:
            ref = tiles[0][0]
            mx = sum(near(t[0], ref) for t in tiles) / len(tiles)
            my = sum(t[1] for t in tiles) / len(tiles)
            if own_tiles:
                ox, oy = min(
                    ((near(c[0], mx), c[1]) for c in own_tiles),
                    key=lambda c: (c[0] - mx) ** 2 + (c[1] - my) ** 2,
                )
                dx, dy = mx - ox, my - oy
                length = (dx * dx + dy * dy) ** 0.5
                if length > 0:
                    mx += PHANTOM_PUSH * dx / length
                    my += PHANTOM_PUSH * dy / length
            x = round(mx) % width if width else round(mx)
            placed.append((x, round(my), s.rival_city_default_pop - 2))
        return placed

    def _forget_gone_units(self, units: Iterable[Unit], turn: int) -> None:
        """Drop what is kept per unit once the unit is gone, and any expired set-asides."""
        alive = {u.unit_id for u in units}  # Including units with no moves left this turn.
        self._site_claims = {k: v for k, v in self._site_claims.items() if k in alive}
        self._holds = {k: v for k, v in self._holds.items() if k in alive}
        self._no_site_turns = {k: v for k, v in self._no_site_turns.items() if k in alive}
        self._scout_targets = {k: v for k, v in self._scout_targets.items() if k in alive}
        self._scout_waits = {k: v for k, v in self._scout_waits.items() if k in alive}
        self._automated = {k: v for k, v in self._automated.items() if k in alive}
        self._set_aside = {k: v for k, v in self._set_aside.items() if v >= turn}
        self._bad_frontiers = {k: v for k, v in self._bad_frontiers.items() if v >= turn}

    # ----- settlers -----

    def _survey(
        self,
        settlers: list[Unit],
        units: Iterable[Unit],
        exploration: Exploration,
        city_tiles: list[tuple[int, int]],
        danger: _Danger,
        turn: int,
    ) -> None:
        """Record how many safe sites there are and the best one, for production and the log."""
        if not city_tiles:
            return
        rival_tiles = [(city[0], city[1]) for city in self._rival_cities]
        origin = settlers[0] if settlers else None
        if origin is None:
            origin = unit_at(*city_tiles[0])
        # Judged as an escorted settler would judge them. A settler waits for an escort when a
        # hostile unit is near (and production builds one under threat), so a barbarian near
        # a site is no reason to build no settler at all. Run 13: four barbarians shadowed the
        # capital, every site read "hostile unit near the site", safe_sites stayed 0 from turn
        # 15 and no settler was built by turn 51.
        candidates = self._candidates(
            origin, exploration, city_tiles, rival_tiles, danger, False, turn, [], escorted=True
        )
        # Pending-fog sites count: their measurement arrives with the next read.
        sites = [c for c in candidates if c.rejected is None or c.rejected == FOG_PENDING]
        self._choose_fog_sites(exploration, city_tiles, [(u.x, u.y) for u in settlers])
        width = self.settings.map_width
        self._look_points = []
        for c in candidates:
            info = self._fog.get((c.x, c.y))
            # A site marked below the minimum by its own fog penalty is still worth a look.
            usable = c.rejected in (None, FOG_PENDING, BELOW_MIN_SCORE)
            if (
                usable
                and (c.x, c.y) in self._needs_look
                and info is not None
                and len(self._look_points) < 3
            ):
                lx = c.x + info.dx
                self._look_points.append((lx % width if width else lx, max(0, c.y + info.dy)))
        rejected: dict[str, int] = {}
        for c in candidates:
            if c.rejected is not None:
                rejected[c.rejected] = rejected.get(c.rejected, 0) + 1
        # Updated in place: production and the log line hold this same object.
        state = self.expansion
        state.survey_turn = turn
        state.safe_sites = len(sites)
        state.rejected = rejected
        state.best_site_score = max((c.site_score for c in sites), default=None)
        state.settlers_on_map = sum(
            1 for u in units if u.unit_type == SETTLER and u.unit_id not in self._founded
        )

    def _choose_fog_sites(
        self, exploration: Exploration, city_tiles, settler_tiles: Iterable = ()
    ) -> None:
        """The sites whose fog the next known-city read counts: settlers' own sites first, then
        the nearest to our cities."""
        s = self.settings
        if not city_tiles:
            self._fog_sites = []
            return

        def gap(site: SettleSite) -> int:
            return min(hex_distance(site.x, site.y, cx, cy, s.map_width) for cx, cy in city_tiles)

        ranked = sorted(exploration.settle_sites, key=lambda site: (gap(site), site.x, site.y))
        # A settler's target, and the tile it stands on, are always measured: founding waits for
        # a measurement (FOG_PENDING), so a far site outside the nearest few would never get one
        # and its settler would wait for nothing.
        wanted = set(self._site_claims.values()) | set(settler_tiles)
        chosen = [site for site in ranked if (site.x, site.y) in wanted]
        chosen += [site for site in ranked if (site.x, site.y) not in wanted]
        specs = []
        for site in chosen[: s.fog_max_sites]:
            near = sorted(
                city_tiles, key=lambda c: (hex_distance(site.x, site.y, *c, s.map_width), c)
            )[: s.fog_own_cities]
            specs.append((site.x, site.y, *(n for c in near for n in c)))
        self._fog_sites = specs

    def _fog_verdict(self, x: int, y: int, safe: float, own, sources) -> tuple[float, float, bool]:
        """(robust rate, unrevealed share, needs a look) for a site; see the settings."""
        s = self.settings
        info = self._fog.get((x, y))
        if info is None or info.unrevealed == 0 or info.total == 0:
            return safe, 0.0, False
        share = info.unrevealed / info.total
        # The unseen city is as large as the largest city we know on either side (early on every
        # city is small, so this stays small), and counts as a capital when most of the rival
        # side is unexplored: a heartland we have not seen. Run 14: El Mirador at (60,29), 81%
        # unexplored, was judged against an unseen pop-6 city and read +11.9; the unseen Mapuche
        # capital (pop 13, 6 tiles) pressed 10.4 and the city flipped 13 turns after founding.
        sizes = [pop for _, _, pop, _ in map(_unpack, list(own) + list(sources))]
        robust = safe_loyalty_rate(
            x,
            y,
            own,
            sources,
            s,
            unseen_distance=info.nearest,
            unseen_pop=max(sizes, default=s.unseen_pop_min),
            unseen_capital=share >= s.unseen_capital_share,
        )
        needs = (
            robust < s.loyalty_reject_safe
            and share >= s.fog_share_min
            and info.unrevealed >= s.fog_min_tiles
        )
        return robust, share, needs

    def _candidates(
        self,
        settler: Unit,
        exploration: Exploration,
        city_tiles: list[tuple[int, int]],
        rival_tiles: list[tuple[int, int]],
        danger: _Danger,
        first_city: bool,
        turn: int,
        reserved: list[tuple[int, int]],
        escorted: bool = False,
    ) -> list[CandidateLog]:
        self._needs_look = set()  # This scan's sites that need a look.
        s = self.settings
        found = []
        # The first own city listed stands for the capital in the distance component (logged only).
        capital = self._own_cities[0][:2] if self._own_cities else None
        for site in exploration.settle_sites:
            site_score = score_site(site, s)
            distance = hex_distance(settler.x, settler.y, site.x, site.y, s.map_width)
            turns = math.ceil(distance / max(1.0, settler.max_moves))
            # The first city can't be picky: a rival's pull is only counted once we have a city.
            margin, rate, safe = 0.0, 0.0, 0.0
            sources = self._pressure_sources()
            if not first_city:
                margin = loyalty_margin(site.x, site.y, self._own_cities, sources, s)
                rate = loyalty_rate(site.x, site.y, self._own_cities, sources, s)
                safe = safe_loyalty_rate(site.x, site.y, self._own_cities, sources, s)
            robust, fog_share, needs_look = safe, 0.0, False
            if not first_city:
                robust, fog_share, needs_look = self._fog_verdict(
                    site.x, site.y, safe, self._own_cities, sources
                )
            penalty = max(0.0, -margin) * s.loyalty_penalty_weight
            if needs_look:
                # Run 14: refusing these while a scout lived stalled expansion for 12+ turns (the
                # scout was on automatic exploration and never looked), and early on every site
                # near a small capital reads this way. The site competes, marked down for the
                # shortfall; scouts the bot directs still go to look (`_look_points`).
                penalty += (s.loyalty_reject_safe - robust) * s.fog_penalty_weight
                self._needs_look.add((site.x, site.y))
            penalty += max(0.0, -rate) * s.loyalty_rate_penalty_weight
            if not first_city:
                # Run 10: 17 site points of difference beat a site 3.4 rate points safer.
                penalty += max(0.0, s.loyalty_comfort_rate - safe) * s.loyalty_safe_penalty_weight
            site_score -= penalty
            nearest_own = min(
                (hex_distance(site.x, site.y, cx, cy, s.map_width) for cx, cy in city_tiles),
                default=None,
            )
            rejected = None
            if (site.x, site.y) in self._failed_sites:
                rejected = "founding failed here"
            elif self._set_aside.get((site.x, site.y), -1) >= turn:
                rejected = "recent moves to it were refused"
            elif any(
                hex_distance(site.x, site.y, cx, cy, s.map_width) < s.min_city_distance
                for cx, cy in city_tiles + rival_tiles
            ):
                rejected = "too close to a city"
            elif any(
                hex_distance(site.x, site.y, cx, cy, s.map_width) < s.min_city_distance
                for cx, cy in reserved
            ):
                rejected = "reserved by another settler"
            elif (
                margin < -s.loyalty_reject_margin
                or rate < s.loyalty_reject_rate
                or (not first_city and safe < s.loyalty_reject_safe)
            ):
                rejected = "likely to flip to a rival (loyalty)"
            elif not first_city and self._fog_can and (site.x, site.y) not in self._fog:
                rejected = FOG_PENDING
            elif not first_city and site_score < s.min_site_score:
                rejected = BELOW_MIN_SCORE
            elif nearest_own is not None and nearest_own > s.max_city_distance:
                if (
                    nearest_own <= s.far_city_distance
                    and safe >= s.far_min_safe_rate
                    and not first_city
                ):
                    site_score -= s.far_penalty * (nearest_own - s.max_city_distance)
                    if site_score < s.min_site_score:
                        rejected = BELOW_MIN_SCORE
                else:
                    rejected = "too far from our cities"
            # Last, so that HOSTILE_NEAR_SITE means a hostile unit is the site's only problem:
            # the founding shortcuts that waive it must not accept a site that fails anything else.
            if rejected is None and not escorted and danger.near(site.x, site.y, s.threat_radius):
                rejected = HOSTILE_NEAR_SITE
            path_threat = rejected is None and danger.on_path(
                (settler.x, settler.y), (site.x, site.y), s.threat_radius
            )
            # Issue 288: a camp or its units may lurk in the fog beside the site.
            recent_near = rejected is None and danger.recent_near(
                site.x, site.y, s.threat_radius + s.flee_mounted_extra
            )
            fog_risk = (
                rejected is None
                and fog_share >= s.fog_share_min
                and danger.recent_near(site.x, site.y, s.threat_radius)
            )
            path_threat = path_threat or fog_risk
            breakdown = score_site_breakdown(site, s, [c[:2] for c in self._own_cities], capital)
            found.append(
                CandidateLog(
                    site.x,
                    site.y,
                    site_score,
                    distance,
                    turns,
                    site_score - s.travel_turn_cost * turns,
                    rejected,
                    path_threat,
                    margin,
                    penalty,
                    rate,
                    safe,
                    robust,
                    fog_share,
                    recent_near,
                    fog_risk,
                    breakdown,
                    breakdown_total(breakdown),
                )
            )
        found.sort(key=lambda c: (-c.net_score, c.distance, c.x, c.y))
        return found

    async def _settle(
        self,
        game: SettlingGame,
        settler: Unit,
        units: Iterable[Unit],
        city_tiles: list[tuple[int, int]],
        rival_tiles: list[tuple[int, int]],
        exploration: Exploration,
        danger: _Danger,
        turn: int,
        claimed: set[int],
    ) -> None:
        s = self.settings
        first_city = not city_tiles
        waited = turn - self._first_turn if first_city and self._first_turn is not None else 0
        here = next(
            (
                site
                for site in exploration.settle_sites
                if (site.x, site.y) == (settler.x, settler.y)
            ),
            None,
        )
        here_score = score_site(here, s) if here else None
        reserved = [t for uid, t in sorted(self._site_claims.items()) if uid != settler.unit_id]
        # Live: barbarians shadowed an escorted settler for turns, so every site near it was
        # "hostile unit near the site" and it never founded. A settler with a combat unit on its
        # own tile has the escort the threat rules are there to demand, so it may found.
        escorted = any(
            u.is_combat and (u.x, u.y) == (settler.x, settler.y) and u.unit_type != SCOUT
            for u in units
        )
        candidates = self._candidates(
            settler,
            exploration,
            city_tiles,
            rival_tiles,
            danger,
            first_city,
            turn,
            reserved,
            escorted,
        )
        usable = [c for c in candidates if c.rejected is None]
        if not escorted:
            # A site refused only because a hostile unit is near it is open to an escorted
            # settler, which is how the survey counts it (Run 13). Offer it as a site that needs
            # an escort: the route rules below then gather one, or hold, and never go alone.
            usable += [
                replace(c, rejected=None, path_threat=True)
                for c in candidates
                if c.rejected == HOSTILE_NEAR_SITE
            ]

        def record(action: str, reason: str, target=None, result: str = "") -> None:
            if action in _HELD_ACTIONS:
                self._held_now.add(settler.unit_id)
            self._record(
                SettleDecision(
                    turn=turn,
                    unit_id=settler.unit_id,
                    x=settler.x,
                    y=settler.y,
                    action=action,
                    reason=reason,
                    first_city=first_city,
                    cities=len(city_tiles),
                    waited_turns=waited,
                    delay_turns=s.first_city_delay_turns,
                    settle_now_score=s.settle_now_score,
                    here_score=here_score,
                    target=target,
                    result=result,
                    candidates=tuple(candidates),
                    threats=tuple(
                        ThreatLog(
                            t.unit_type,
                            t.owner_id,
                            t.x,
                            t.y,
                            hex_distance(settler.x, settler.y, t.x, t.y, s.map_width),
                        )
                        for t in danger.threats
                        if t.hostile
                    ),
                    own_cities=tuple(self._own_cities),
                    rival_cities=tuple(self._rival_cities),
                    phantom_cities=tuple(self._phantoms),
                )
            )

        if escorted and self._stripped and await self._recall(game, settler, units, record):
            return
        at_war = self.defence.at_war if self.defence is not None else False
        hostile_only_here = here is not None and all(
            c.rejected in (None, HOSTILE_NEAR_SITE)
            for c in candidates
            if (c.x, c.y) == (settler.x, settler.y)
        )
        # On a good site with only a small threat near: found now, don't run (the user's
        # critique of Run 18: the city is safe from one or two barbarians, and a settler
        # that runs loses the turns).
        found_now_ok = hostile_only_here and found_under_threat_ok(
            (settler.x, settler.y), danger.threats, at_war, s, s.map_width
        )
        step_target = (
            None
            if found_now_ok
            else self._step_and_found_target(settler, candidates, danger, at_war)
        )
        if step_target is not None and await self._step_and_found(
            game, settler, step_target, city_tiles, record
        ):
            return
        near = danger.count_near(settler.x, settler.y, s.flee_radius, s.flee_mounted_extra)
        threatened = near > 0 and not found_now_ok
        # Live (Run 12): an escorted settler stepped out, then fled back to the city next turn
        # and the pair repeated it. A settler with its escort on the tile goes on instead. Live
        # (issue 283): two settlers were taken with an escort on the tile or beside it, with two
        # or more hostile units (cavalry among them) close; one escort is no cover against that.
        outnumbered = near >= s.flee_escorted_hostiles
        if (
            threatened
            and city_tiles
            and (not escorted or outnumbered)
            and await self._flee(game, settler, city_tiles, record, near)
        ):
            return
        # Issue 291: a settler in a city never flees, but must not walk out into the units the flee
        # rule would have run from. One hostile is left to the escort gate, which may gather one.
        if threatened and (settler.x, settler.y) in city_tiles and outnumbered:
            self._holds[settler.unit_id] = self._holds.get(settler.unit_id, 0) + 1
            record(
                "hold",
                f"{near} hostile unit(s) close to the city, so the settler stays inside",
            )
            return
        if settler.unit_id in self._fled:
            claim = self._fled[settler.unit_id]
            if danger.on_path((settler.x, settler.y), claim, s.threat_radius):
                # Only that route is backed off from; another site may still be taken.
                usable = [c for c in usable if (c.x, c.y) != claim]
                if not usable:
                    record(
                        "hold",
                        "fled a hostile unit that is still near the route, so not resuming yet",
                        claim,
                    )
                    return
            else:
                del self._fled[settler.unit_id]

        options = usable
        found_here = False
        reason = ""
        if first_city:
            window = s.first_city_delay_turns - waited
            good_here = here_score is not None and here_score >= s.settle_now_score
            if window <= 0 or good_here:
                found_here = True
                reason = "delay over" if window <= 0 else "site meets the settle-now score"
            else:
                reach = window * max(1.0, settler.max_moves)
                options = [
                    c
                    for c in usable
                    if 0 < c.distance <= reach and (here_score is None or c.site_score > here_score)
                ]
                if not options:
                    found_here, reason = True, "no better site within reach"
                    options = usable
        elif here is not None and all(
            c.rejected is None for c in candidates if (c.x, c.y) == (settler.x, settler.y)
        ):
            found_here, reason = True, "standing on an acceptable site"
        elif here is not None and found_now_ok:
            found_here, reason = True, "standing on a good site, the threat near is small"
        if found_here and (settler.x, settler.y) in self._failed_sites:
            record(
                "found_skipped",
                "founding already failed on this tile, so looking for another site",
                (settler.x, settler.y),
            )
            found_here = False
            options = usable
        if found_here:
            result = await game.found_city(settler.unit_index)
            if action_succeeded(result):
                record("found", reason, (settler.x, settler.y), result)
                self._site_claims.pop(settler.unit_id, None)
                self._fled.pop(settler.unit_id, None)
                self._holds.pop(settler.unit_id, None)
                self._no_site_turns.pop(settler.unit_id, None)
                self._founded.add(settler.unit_id)
                city_tiles.append((settler.x, settler.y))  # Later settlers this turn see it.
                return
            self._failed_sites.add((settler.x, settler.y))
            record("found_failed", reason, (settler.x, settler.y), result)
        better = [c for c in options if c.distance > 0 and c.rejected is None]

        if not better:
            self._site_claims.pop(settler.unit_id, None)
            idle = self._no_site_turns.get(settler.unit_id, 0) + 1
            self._no_site_turns[settler.unit_id] = idle
            if idle >= s.no_site_disband_turns and await self._disband(game, settler, record, idle):
                return
            record("no_site", "no acceptable site on the revealed map")
            return
        self._no_site_turns.pop(settler.unit_id, None)  # A site exists, so the streak ends.
        holds = self._holds.get(settler.unit_id, 0)
        # Live (Run 7): the escort chosen was another unit, and its move onto the settler's tile
        # was refused (STACKING_CONFLICT) because a combat unit already stood there, so the
        # settler held for turns. A unit already on the tile is the escort. Live (Run 12): the
        # same in the capital, where the garrison was excluded; a city's garrison may escort
        # when another combat unit stays behind, so the city is never left empty.
        on_tile = [
            u
            for u in units
            if _land_military(u)
            and (u.x, u.y) == (settler.x, settler.y)
            and u.unit_id not in claimed
        ]
        here_tile = (settler.x, settler.y)
        in_city = here_tile in city_tiles
        spare = len(on_tile) >= (2 if in_city and here_tile not in self._lone_ok else 1)
        movable = [u for u in on_tile if u.can_move]
        companion = min(movable, key=lambda u: u.unit_id) if spare and movable else None
        if on_tile and spare and companion is None:
            escort = None  # The unit with the settler has no moves left: wait, gather no second.
        elif companion is not None:
            escort = companion  # Never gather a second unit.
        else:
            escort = self._find_escort(settler, units, city_tiles, claimed, danger)
        crowded = any(_holds_tile(u) and (u.x, u.y) == (settler.x, settler.y) for u in units)
        if escort is not None and companion is None:
            # Skip an escort whose order the game already refused with nothing changed.
            excluded = set(claimed)
            while escort is not None and self._refused_order(
                settler, better[0], escort, units, crowded
            ):
                excluded.add(escort.unit_id)
                escort = self._find_escort(settler, units, city_tiles, excluded, danger)
        if escort is not None and (escort.x, escort.y) in city_tiles:
            origin = (escort.x, escort.y)  # Wherever the settler is, this city loses its defender.
            if sum(1 for u in units if _land_military(u) and (u.x, u.y) == origin) == 1:
                self._stripped[origin] = escort.unit_id
        target = next((c for c in better if not c.path_threat or escort is not None), None)
        if target is None:
            # Going alone is for a route the threat read finds clear (those candidates have no
            # path_threat); otherwise wait for an escort rather than walk into the threat.
            self._holds[settler.unit_id] = holds + 1
            fogged = any(c.fog_risk for c in better)
            record(
                "hold",
                "a hostile unit was seen near a fogged site and no escort is free; it goes when "
                "the sighting is stale"
                if fogged
                else "every route passes a hostile unit and no escort is free",
            )
            return
        reason = "heading for the best site"
        self._site_claims[settler.unit_id] = (target.x, target.y)
        moved = await self._advance(
            game,
            settler,
            target,
            escort,
            claimed,
            record,
            turn,
            reason,
            units,
            crowded,
            danger.threats,
        )
        self._holds[settler.unit_id] = 0 if moved else holds + 1

    async def _advance(
        self,
        game,
        settler,
        target,
        escort,
        claimed,
        record,
        turn,
        reason,
        units=(),
        crowded=False,
        danger_threats=(),
    ) -> bool:
        """Take one step towards the site. Returns False if the settler did not get closer."""
        s = self.settings
        goal = (target.x, target.y)
        if target.path_threat and escort is not None:
            step = self._pick_step(settler, goal)
            claimed.add(escort.unit_id)
            # Issue 291: the escort must be able to hold the tile the pair ends this turn on.
            holds, taken, attackers = escort_holds_tile(escort, step, danger_threats, s)
            if attackers and not holds:
                record(
                    "hold",
                    f"the escort ({escort.unit_type}, {escort.hp} hp) would not hold {step}: "
                    f"{attackers} hostile unit(s) can reach it for about {taken:.0f} damage",
                    goal,
                )
                return False
            if (escort.x, escort.y) != (settler.x, settler.y):
                if not crowded:
                    meet = (settler.x, settler.y)
                elif (escort.x, escort.y) == step:
                    # The escort waits on the free tile; a civilian may join it there.
                    result = await game.move_unit(settler.unit_index, *step)
                    record("escort_move", "the settler joins the escort on the route", goal, result)
                    return move_succeeded(result)
                else:
                    meet = step  # Two combat units can't share a tile, so meet on the route.
                result = await game.move_unit(escort.unit_index, *meet)
                if not move_succeeded(result):
                    self._refused.add(self._order_key(escort, meet, units))
                    if meet == step:
                        self._refused_steps.add((settler.unit_id, (settler.x, settler.y), step))
                action = "escort_gather" if move_succeeded(result) else "hold"
                record(action, "route is threatened, so the escort joins first", goal, result)
                return False
            result = await game.move_unit(escort.unit_index, *step)
            if not move_succeeded(result):
                self._refused_steps.add((settler.unit_id, (settler.x, settler.y), step))
                record("hold", "the escort could not move", goal, result)
                return False
            result = await game.move_unit(settler.unit_index, *step)
            moved = move_succeeded(result)
            if not moved:
                self._refused_steps.add((settler.unit_id, (settler.x, settler.y), step))
            record("escort_move", "route is threatened, so the escort leads", goal, result)
            return moved
        # Issue 288: a settler that reaches the site with no moves left spends a turn there
        # exposed, unable to found. With a hostile seen near, it stops a tile short instead.
        dest = goal
        gap = hex_distance(settler.x, settler.y, *goal, s.map_width)
        if target.recent_near and 2 <= gap <= max(1, int(settler.moves)):
            dest = self._pick_step(settler, goal, stop_short=True) or goal
            reason = "a hostile unit was seen near the site, so stopping a tile short"
        result = await game.move_unit(settler.unit_index, *dest)
        moved = move_succeeded(result)
        if moved:
            self._move_failures.pop(goal, None)
        else:
            failures = self._move_failures.get(goal, 0) + 1
            self._move_failures[goal] = failures
            if failures >= s.max_move_failures:
                self._set_aside[goal] = turn + s.retry_turns
                self._move_failures.pop(goal)
                self._site_claims.pop(settler.unit_id, None)
        record("move", reason if moved else "move refused, trying again later", goal, result)
        return moved

    def _pick_step(self, settler, goal, stop_short=False) -> tuple[int, int] | None:
        """The tile to send the settler (and escort) to this turn. Issue 368: the first stretch of
        a cheapest terrain path, never water or a mountain nor a step the game refused; the
        straight line when no path is known."""
        here = (settler.x, settler.y)
        moves = max(1, int(settler.moves))
        if self.terrain is not None and len(self.terrain):
            avoid = {
                t for sid, at, t in self._refused_steps if sid == settler.unit_id and at == here
            }
            found = self.terrain.step_toward(here, goal, moves, avoid, stop_short)
            if found is not None:
                return found
        line = hex_line(here, goal, self.settings.map_width)
        if stop_short:
            return line[-2] if len(line) >= 2 else None
        return line[min(moves, len(line)) - 1] if line else None

    def _order_key(self, escort, dest, units) -> tuple:
        crowd = sum(1 for u in units if _holds_tile(u) and (u.x, u.y) == tuple(dest))
        return (escort.unit_id, (escort.x, escort.y), tuple(dest), crowd)

    def _refused_order(self, settler, target, escort, units, crowded) -> bool:
        """True when the join this escort would be sent on was refused and nothing has changed."""
        if target is None or not target.path_threat:
            return False
        if not crowded:
            meet = (settler.x, settler.y)
        else:
            meet = self._pick_step(settler, (target.x, target.y))
            if meet is None:
                return False
        return self._order_key(escort, meet, units) in self._refused

    def _find_escort(self, settler, units, city_tiles, claimed, danger) -> Unit | None:
        s = self.settings
        return find_escort(
            settler,
            units,
            city_tiles,
            claimed,
            s.escort_search_radius,
            s.map_width,
            self._lone_ok,
        )

    def _tidy_stripped(self, units: Iterable[Unit]) -> None:
        """Forget a stripped city once it holds a defender again or its escort is gone."""
        units = list(units)
        ids = {u.unit_id for u in units}
        for tile, escort_id in list(self._stripped.items()):
            held = any(_land_military(u) and (u.x, u.y) == tile for u in units)
            if held or escort_id not in ids:
                del self._stripped[tile]

    async def _recall(self, game, settler, units, record) -> bool:
        """Bring a settler and the escort that was a city's last defender back to that city once
        the threat there is no longer limited, so a second wave never finds it empty."""
        here = (settler.x, settler.y)
        for tile, escort_id in sorted(self._stripped.items()):
            if tile in self._lone_ok or tile == here:
                continue
            escort = next((u for u in units if u.unit_id == escort_id and (u.x, u.y) == here), None)
            if escort is None or not escort.can_move:
                continue
            result = await game.move_unit(escort.unit_index, *tile)
            if not move_succeeded(result):
                continue
            await game.move_unit(settler.unit_index, *tile)
            record("flee", "the escort's city is threatened again, so both go back", tile, result)
            return True
        return False

    async def _disband(self, game, settler, record, idle: int) -> bool:
        """Disband a settler that has had no site for many turns. True if it is gone.

        Civ6-mcp has no join-city command, and an idle settler only costs upkeep and a pop.
        """
        delete = getattr(game, "delete_unit", None)
        if delete is None:
            return False
        result = await delete(settler.unit_index)
        record("disband", f"no acceptable site for {idle} turns", None, result)
        if not action_succeeded(result):
            return False
        self._no_site_turns.pop(settler.unit_id, None)
        self._founded.add(settler.unit_id)  # Gone, so not on the map.
        return True

    def _step_and_found_target(self, settler, candidates, danger, at_war):
        """The site one tile away that the settler can step onto and still have a move left
        to found on, when only a small threat is near it. None if there is none (or no terrain
        store, or the step's cost is unknown: an unknown tile counts as slow)."""
        s = self.settings
        if self.terrain is None:
            return None
        for c in candidates:
            # A hostile seen near and now unseen (issue 288) also steps on and founds at once,
            # so the settler is never left on the site with its moves spent.
            if c.rejected != HOSTILE_NEAR_SITE and not (c.rejected is None and c.recent_near):
                continue  # No hostile near: the usual route (step now, found next turn).
            if hex_distance(settler.x, settler.y, c.x, c.y, s.map_width) != 1:
                continue
            cost = self.terrain.step_cost(c.x, c.y, naval=False)
            if cost is None or settler.moves - cost <= 0:
                continue  # Blocked, or the step would use up the moves it needs to found.
            if found_under_threat_ok((c.x, c.y), danger.threats, at_war, s, s.map_width):
                return c
        return None

    async def _step_and_found(self, game, settler, target, city_tiles, record) -> bool:
        goal = (target.x, target.y)
        result = await game.move_unit(settler.unit_index, *goal)
        if not move_succeeded(result) or f"now_at:{goal[0]},{goal[1]}" not in result:
            record(
                "move", "stepping onto the site to found it, but it did not arrive", goal, result
            )
            return False
        found = await game.found_city(settler.unit_index)
        if action_succeeded(found):
            record(
                "found", "stepped onto the site and founded, the threat near is small", goal, found
            )
            self._site_claims.pop(settler.unit_id, None)
            self._holds.pop(settler.unit_id, None)
            self._no_site_turns.pop(settler.unit_id, None)
            self._founded.add(settler.unit_id)
            city_tiles.append(goal)
            return True
        # Not blacklisted: the step may have used the moves founding needs (the terrain cost is
        # an estimate), so the next turn tries from the site, and a real failure there is noted.
        record("found_failed", "stepped onto the site but founding failed", goal, found)
        return True  # It moved; nothing more to do with it this turn.

    async def _flee(self, game, settler, city_tiles, record, near: int = 0) -> bool:
        """Run for the nearest city, or the next nearest when the order is refused. A refusal is
        logged (issue 283: a settler stood in the open with hostile cavalry beside it and nothing
        said why it had not run)."""
        s = self.settings
        if (settler.x, settler.y) in city_tiles:
            return False  # Already inside walls: the usual hold and escort rules apply.
        ordered = sorted(
            city_tiles, key=lambda c: (hex_distance(settler.x, settler.y, *c, s.map_width), c)
        )
        for city in ordered[:2]:
            result = await game.move_unit(settler.unit_index, *city)
            if move_succeeded(result):
                claim = self._site_claims.get(settler.unit_id)
                if claim is not None:
                    self._fled[settler.unit_id] = claim
                record(
                    "flee",
                    f"{near} hostile unit(s) close, retreating to the nearest city",
                    city,
                    result,
                )
                return True
            record("hold", "fleeing to a city was refused", city, result)
        return False

    def _record(self, decision: SettleDecision) -> None:
        self.decisions.append(decision)
        log.info("settle_decision %s", json.dumps(asdict(decision), sort_keys=True))

    # ----- scouts -----

    async def _explore(
        self,
        game: SettlingGame,
        scout: Unit,
        exploration: Exploration,
        danger: _Danger,
        turn: int,
        look: tuple[int, int] | None = None,
    ) -> None:
        s = self.settings
        taken = {t for uid, t in self._scout_targets.items() if uid != scout.unit_id}
        here = (scout.x, scout.y)
        reachable = [
            p
            for p in frontier_points(exploration, s.map_width)
            if self._bad_frontiers.get(p, -1) < turn and p not in taken and p != here
        ]
        # Live (Run 15): a scout sat in the capital for ever because a camp's guards ruled out
        # every frontier, and the wait below never ended. After a short wait it goes anyway.
        impatient = self._scout_waits.get(scout.unit_id, 0) >= s.scout_wait_turns
        # A hostile unit only rules a frontier out for this turn, because it will move.
        frontier = [p for p in reachable if impatient or not danger.near(*p, s.scout_threat_radius)]
        if look is not None and look != here and self._bad_frontiers.get(look, -1) < turn:
            frontier.append(look)  # The fog a site's loyalty depends on, itself a target.
        previous = self._scout_targets.get(scout.unit_id)

        def toward(p: tuple[int, int]) -> int:
            return hex_distance(*look, *p, s.map_width) if look is not None else 0

        frontier.sort(
            key=lambda p: (
                # Keep a heading that is still on the edge of the fog, unless a look is wanted:
                # that takes priority over persistence.
                look is None and p != previous,
                toward(p),
                hex_distance(scout.x, scout.y, *p, s.map_width),
                p,
            )
        )
        tried = 0
        blocked_by_threat = len(reachable) > len(frontier)
        # Live: barbarians camped beside the capital from turn 9, so every route out started
        # within their radius and the first scout waited for ever. After a short wait it goes.
        for target in frontier:
            if not impatient and danger.on_path(here, target, s.scout_threat_radius):
                blocked_by_threat = True
                continue
            if tried == _MAX_SCOUT_TRIES:
                break
            tried += 1
            if move_succeeded(await game.move_unit(scout.unit_index, *target)):
                self._scout_targets[scout.unit_id] = target
                self._scout_waits.pop(scout.unit_id, None)
                return
            self._bad_frontiers[target] = turn + s.retry_turns
        self._scout_targets.pop(scout.unit_id, None)
        if blocked_by_threat and not tried:
            self._scout_waits[scout.unit_id] = self._scout_waits.get(scout.unit_id, 0) + 1
            log.info(
                "scout %s waits at %s: every frontier is near a hostile unit (wait %d of %d)",
                scout.unit_id,
                here,
                self._scout_waits[scout.unit_id],
                s.scout_wait_turns,
            )
            return  # Wait a turn for the hostile unit to move on.
        last = self._automated.get(scout.unit_id)
        if last is None or turn - last >= s.automate_retry_turns:
            result = await game.automate_explore(scout.unit_index)
            if action_succeeded(result):
                self._automated[scout.unit_id] = turn
            else:
                log.warning("Scout %s could not be automated: %s", scout.unit_id, result)
