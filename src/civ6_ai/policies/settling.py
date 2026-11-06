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
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Protocol

from civ6_ai.state import (
    Exploration,
    GameReads,
    Threat,
    Unit,
    read_cities,
    read_exploration,
    read_rivals,
    read_threats,
    read_units,
)
from civ6_ai.state.models import SettleSite
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance

log = logging.getLogger(__name__)

SETTLER = "UNIT_SETTLER"
SCOUT = "UNIT_SCOUT"

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
    travel_turn_cost: float = 6.0  # Score given up for each turn spent walking to a site.
    # Site score. The weights follow the ones civ6-mcp ranks its list with, minus its hidden terms.
    food_weight: float = 2.0
    production_weight: float = 2.0
    luxury_weight: float = 4.0
    strategic_weight: float = 3.0
    defense_weight: float = 1.0
    fresh_water_bonus: float = 5.0
    coast_bonus: float = 3.0
    # Safety.
    threat_radius: int = 3  # A tile this close to a hostile unit is unsafe to stand on.
    flee_radius: int = 2  # A settler this close to a hostile unit runs.
    escort_search_radius: int = 5
    scout_threat_radius: int = 2
    map_width: int | None = STANDARD_MAP_WIDTH


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


class SettlingGame(GameReads, Protocol):
    """The reads plus the three actions this policy takes."""

    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str: ...

    async def found_city(self, unit_index: int) -> str: ...

    async def automate_explore(self, unit_index: int) -> str: ...


def _succeeded(result: str) -> bool:
    return not result.startswith(("Error", "ERR:"))


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


@dataclass
class _Danger:
    """Hostile units in sight, and tests for tiles and routes near them."""

    threats: tuple[Threat, ...]
    map_width: int | None

    def near(self, x: int, y: int, radius: int) -> bool:
        return any(
            hex_distance(x, y, t.x, t.y, self.map_width) <= radius
            for t in self.threats
            if t.hostile
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
    _failed_sites: set[tuple[int, int]] = field(default_factory=set, init=False, repr=False)
    _bad_frontiers: set[tuple[int, int]] = field(default_factory=set, init=False, repr=False)
    _automated: set[int] = field(default_factory=set, init=False, repr=False)
    _scout_targets: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)

    async def run(self, game: SettlingGame) -> None:
        units = await read_units(game)
        settlers = [u for u in units if u.unit_type == SETTLER and u.can_move]
        scouts = [u for u in units if u.unit_type == SCOUT and u.can_move]
        if not settlers and not scouts:
            return
        turn = (await game.get_game_overview()).turn
        threats = await read_threats(game)
        cities = await read_cities(game)
        exploration = await read_exploration(game)
        danger = _Danger(threats, self.settings.map_width)
        city_tiles = [(c.x, c.y) for c in cities]
        claimed: set[int] = set()
        if settlers:
            rival_tiles = [
                (city.x, city.y)
                for rival in await read_rivals(game)
                for city in rival.visible_cities
            ]
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
        for scout in scouts:
            await self._explore(game, scout, exploration, danger)

    # ----- settlers -----

    def _candidates(
        self,
        settler: Unit,
        exploration: Exploration,
        city_tiles: list[tuple[int, int]],
        rival_tiles: list[tuple[int, int]],
        danger: _Danger,
        first_city: bool,
    ) -> list[CandidateLog]:
        s = self.settings
        found = []
        for site in exploration.settle_sites:
            site_score = score_site(site, s)
            distance = hex_distance(settler.x, settler.y, site.x, site.y, s.map_width)
            turns = math.ceil(distance / max(1.0, settler.max_moves))
            nearest_own = min(
                (hex_distance(site.x, site.y, cx, cy, s.map_width) for cx, cy in city_tiles),
                default=None,
            )
            rejected = None
            if (site.x, site.y) in self._failed_sites:
                rejected = "founding failed here"
            elif any(
                hex_distance(site.x, site.y, cx, cy, s.map_width) < s.min_city_distance
                for cx, cy in city_tiles + rival_tiles
            ):
                rejected = "too close to a city"
            elif danger.near(site.x, site.y, s.threat_radius):
                rejected = "hostile unit near the site"
            elif not first_city and site_score < s.min_site_score:
                rejected = "below minimum site score"
            elif nearest_own is not None and nearest_own > s.max_city_distance:
                rejected = "too far from our cities"
            path_threat = rejected is None and danger.on_path(
                (settler.x, settler.y), (site.x, site.y), s.threat_radius
            )
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
        candidates = self._candidates(
            settler, exploration, city_tiles, rival_tiles, danger, first_city
        )
        usable = [c for c in candidates if c.rejected is None]

        def record(action: str, reason: str, target=None, result: str = "") -> None:
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
                )
            )

        threatened = danger.near(settler.x, settler.y, s.flee_radius)
        if threatened and city_tiles and await self._flee(game, settler, city_tiles, record):
            return

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
        if found_here:
            result = await game.found_city(settler.unit_index)
            if _succeeded(result):
                record("found", reason, (settler.x, settler.y), result)
                return
            self._failed_sites.add((settler.x, settler.y))
            record("found_failed", reason, (settler.x, settler.y), result)
        better = [c for c in options if c.distance > 0 and (c.x, c.y) not in self._failed_sites]

        if not better:
            record("no_site", "no acceptable site on the revealed map")
            return
        escort = self._find_escort(settler, units, city_tiles, claimed, danger)
        for target in better:
            if target.path_threat and escort is None:
                continue  # Another site may have a safe route.
            await self._advance(game, settler, target, escort, claimed, danger, record)
            return
        record("hold", "every route passes a hostile unit and no escort is free")

    async def _advance(self, game, settler, target, escort, claimed, danger, record) -> None:
        s = self.settings
        goal = (target.x, target.y)
        line = hex_line((settler.x, settler.y), goal, s.map_width)
        step = line[min(max(1, int(settler.moves)), len(line)) - 1]
        if target.path_threat and escort is not None:
            claimed.add(escort.unit_id)
            if (escort.x, escort.y) != (settler.x, settler.y):
                result = await game.move_unit(escort.unit_index, settler.x, settler.y)
                action = "escort_gather" if _succeeded(result) else "hold"
                record(action, "route is threatened, so the escort joins first", goal, result)
                return
            result = await game.move_unit(escort.unit_index, *step)
            if not _succeeded(result):
                record("hold", "the escort could not move", goal, result)
                return
            step_text = "route is threatened, so the escort leads"
            result = await game.move_unit(settler.unit_index, *step)
            record("escort_move", step_text, goal, result)
            return
        result = await game.move_unit(settler.unit_index, *goal)
        if not _succeeded(result):
            self._failed_sites.add(goal)
        record("move", "heading for the best site", goal, result)

    def _find_escort(self, settler, units, city_tiles, claimed, danger) -> Unit | None:
        s = self.settings
        combat = [u for u in units if u.is_combat and u.can_move and u.unit_id not in claimed]
        garrisons: dict[tuple[int, int], int] = {}
        for u in combat:
            garrisons[u.x, u.y] = garrisons.get((u.x, u.y), 0) + 1
        usable = [
            u
            for u in combat
            if u.unit_type != SCOUT
            and not any(word in u.unit_type for word in _SEA_UNITS)
            and not ((u.x, u.y) in city_tiles and garrisons[u.x, u.y] == 1)
            and hex_distance(u.x, u.y, settler.x, settler.y, s.map_width) <= s.escort_search_radius
        ]
        usable.sort(
            key=lambda u: (hex_distance(u.x, u.y, settler.x, settler.y, s.map_width), u.unit_id)
        )
        return usable[0] if usable else None

    async def _flee(self, game, settler, city_tiles, record) -> bool:
        s = self.settings
        nearest = min(
            city_tiles, key=lambda c: (hex_distance(settler.x, settler.y, *c, s.map_width), c)
        )
        result = await game.move_unit(settler.unit_index, *nearest)
        if _succeeded(result):
            record("flee", "hostile unit close, retreating to the nearest city", nearest, result)
            return True
        return False

    def _record(self, decision: SettleDecision) -> None:
        self.decisions.append(decision)
        log.info("settle_decision %s", json.dumps(asdict(decision), sort_keys=True))

    # ----- scouts -----

    async def _explore(
        self, game: SettlingGame, scout: Unit, exploration: Exploration, danger: _Danger
    ) -> None:
        s = self.settings
        taken = {t for uid, t in self._scout_targets.items() if uid != scout.unit_id}
        frontier = [
            p
            for p in frontier_points(exploration, s.map_width)
            if p not in self._bad_frontiers
            and p not in taken
            and not danger.near(*p, s.scout_threat_radius)
        ]
        here = (scout.x, scout.y)
        frontier = [p for p in frontier if p != here]
        previous = self._scout_targets.get(scout.unit_id)
        frontier.sort(
            key=lambda p: (
                p != previous,  # Keep a heading that is still on the edge of the fog.
                hex_distance(scout.x, scout.y, *p, s.map_width),
                p,
            )
        )
        for target in frontier[:_MAX_SCOUT_TRIES]:
            if danger.on_path(here, target, s.scout_threat_radius):
                self._bad_frontiers.add(target)
                continue
            if _succeeded(await game.move_unit(scout.unit_index, *target)):
                self._scout_targets[scout.unit_id] = target
                return
            self._bad_frontiers.add(target)
        self._scout_targets.pop(scout.unit_id, None)
        if scout.unit_id not in self._automated:
            self._automated.add(scout.unit_id)
            await game.automate_explore(scout.unit_index)
