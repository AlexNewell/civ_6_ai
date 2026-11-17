"""A shared map of what threatens us, kept between turns (issue 238, Stage 1).

The map is a list of *sources* (a camp, a hostile unit last seen at a tile) and a threat value per
tile worked out from them. The sources are the record: a policy acts on a source, and a person
diagnosing a run reads one. The grid is only a view of the sources.

Stage 1 changes no behaviour. The handler reads what the game already shows the player, keeps the
map, and writes one line per turn to a dump. Nothing else imports this module yet.

Legal reads only (see `state/fog.py`):
- A camp is a source once seen on a revealed tile. The state layer keeps a camp on a fogged tile,
  as the player's own map does, and it is forgotten only when a *visible* tile shows it gone.
- A hostile unit is a source at the tile where it was seen. It is dropped after
  `unit_memory_turns`, or when a newer sighting of the same kind appears close by.
- Our own units and cities are not sources.

Spread here is tile distance. Spread by movement cost (rivers, hills, water) needs terrain reads
and comes with the stages that use it; a decision to use the map must not assume it.
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from civ6_ai.policies.game_data import COMBAT_UNITS
from civ6_ai.state import read_cities, read_threats, read_units
from civ6_ai.state.models import City, Threat, Tile, Unit
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance, read_area
from civ6_ai.terrain import TerrainStore

log = logging.getLogger(__name__)

BARBARIAN_ID = 63
CAMP_IMPROVEMENT = "IMPROVEMENT_BARBARIAN_CAMP"
CAMP = "camp"
UNIT = "unit"
_SEA_UNITS = ("GALLEY", "QUADRIREME", "CARAVEL", "FRIGATE", "IRONCLAD", "TRIREME", "GALLEASS")


@dataclass(frozen=True)
class ThreatMapSettings:
    """Bounds of the map. All overridable; none is a rule (see the issue's settings split)."""

    map_width: int | None = STANDARD_MAP_WIDTH
    camp_strength: float = 25.0  # What a camp is worth as a source (it spawns units).
    camp_radius: int = 5  # Tiles over which a camp's threat fades to nothing.
    unit_radius: int = 3  # The same for a hostile unit.
    unit_memory_turns: int = 10  # A hostile unit not seen again is dropped after this many turns.
    resight_radius: int = 3  # A new sighting this close replaces an older one of the same kind.
    scan_radius: int = 7  # Tiles read around each city for camps.
    scan_turns: int = 3  # Turns between those reads (a call per city).
    near_city: int = 9  # A source this close to a city counts as near it in the metrics.
    heat_floor: float = 0.5  # Threat below this is left out of the dump.
    # Reach by movement (needs a terrain store; without one the spread is plain tile distance).
    unit_horizon_turns: float = 3.0  # A hostile unit's threat fades out over this many turns.
    camp_horizon_turns: float = 3.0  # So does a camp's, for the units it sends.
    land_moves: float = 2.0  # Movement points per turn of an ordinary walker, and of a camp's.
    cavalry_moves: float = 4.0
    scout_moves: float = 3.0
    ship_moves: float = 3.0
    ranged_reach: int = 2  # Tiles a ranged unit shoots from where it stands.
    melee_reach: int = 1  # And a melee unit strikes.
    read_radius: int = 6  # Tiles of terrain read around a source that has none yet.
    read_retry_turns: int = 10  # Turns before an area that revealed nothing new is read again.


@dataclass
class Source:
    kind: str  # CAMP or UNIT.
    x: int
    y: int
    strength: float
    first_seen: int
    last_seen: int | None  # Turn last confirmed on a visible tile; None if only ever fogged.
    unit_type: str = ""
    hostile_owner: int = BARBARIAN_ID
    naval: bool = False

    def age(self, turn: int) -> int | None:
        return None if self.last_seen is None else turn - self.last_seen


@dataclass
class ThreatMap:
    settings: ThreatMapSettings = field(default_factory=ThreatMapSettings)
    sources: list[Source] = field(default_factory=list)
    terrain: TerrainStore | None = None
    _last_scan: int = -10_000
    _baselined: bool = False
    _fields: dict[tuple, dict[tuple[int, int], float]] = field(default_factory=dict)

    # ----- updating -----

    def due_for_scan(self, turn: int) -> bool:
        return turn - self._last_scan >= self.settings.scan_turns

    def mark_scanned(self, turn: int) -> None:
        self._last_scan = turn

    def take_baseline(self) -> bool:
        """True once: for the first scan of the session."""
        first = not self._baselined
        self._baselined = True
        return first

    def see_tiles(self, turn: int, tiles: Iterable[Tile], baseline: bool = False) -> None:
        """Camps from tiles read this turn.

        A camp seen on a visible tile is kept until a visible tile shows no camp (a fogged tile
        can't be believed gone). A camp on a *fogged* tile is only accepted in the `baseline`
        read, the first one of a session: it stands for what the player's map already showed.
        Later, a fogged camp is ignored until it is seen, because the live read of a fogged tile
        would show a camp that spawned there since, which the player could not know (Run 17:
        a camp spawned out of sight on turn 73 and the map knew it three turns before any unit
        saw it).
        """
        for tile in tiles:
            known = next(
                (s for s in self.sources if s.kind == CAMP and (s.x, s.y) == (tile.x, tile.y)),
                None,
            )
            if tile.improvement == CAMP_IMPROVEMENT:
                if known is None and not (tile.visible or baseline):
                    continue
                if known is None:
                    seen = turn if tile.visible else None
                    self.sources.append(
                        Source(CAMP, tile.x, tile.y, self.settings.camp_strength, turn, seen)
                    )
                    known = self.sources[-1]
                if tile.visible:
                    known.last_seen = turn
            elif tile.visible and known is not None:
                self.sources.remove(known)

    def see_threats(self, turn: int, threats: Iterable[Threat]) -> None:
        """Hostile units visible now, then drop the old ones that are stale or replaced."""
        s = self.settings
        fresh = [
            Source(
                UNIT,
                t.x,
                t.y,
                float(max(t.combat_strength, t.ranged_strength)),
                turn,
                turn,
                unit_type=t.unit_type,
                hostile_owner=t.owner_id,
                naval=any(w in t.unit_type for w in _SEA_UNITS),
            )
            for t in threats
            if t.hostile
        ]
        keep = []
        for old in self.sources:
            if old.kind != UNIT:
                keep.append(old)
                continue
            if (old.age(turn) or 0) >= s.unit_memory_turns:
                continue
            replaced = any(
                f.unit_type == old.unit_type
                and hex_distance(f.x, f.y, old.x, old.y, s.map_width) <= s.resight_radius
                for f in fresh
            )
            if not replaced:
                keep.append(old)
        self.sources = keep + fresh

    # ----- queries -----

    def _reach(self, source: Source) -> int:
        return self.settings.camp_radius if source.kind == CAMP else self.settings.unit_radius

    def _profile(self, src: Source) -> tuple[float, bool, int, float]:
        """(moves per turn, naval, shooting reach, horizon in turns) for a source."""
        s = self.settings
        if src.kind == CAMP:
            return s.land_moves, False, s.melee_reach, s.camp_horizon_turns
        if src.naval:
            return s.ship_moves, True, 0, s.unit_horizon_turns
        stats = COMBAT_UNITS.get(src.unit_type)
        klass = stats[4] if stats else ""
        moves = s.land_moves
        if klass in ("LIGHT_CAVALRY", "HEAVY_CAVALRY") or "HORSE" in src.unit_type:
            moves = s.cavalry_moves
        elif klass == "RECON" or "SCOUT" in src.unit_type:
            moves = s.scout_moves
        ranged = (stats is not None and stats[1] > 0) or klass in ("RANGED", "SIEGE")
        return moves, False, s.ranged_reach if ranged else s.melee_reach, s.unit_horizon_turns

    def _field(self, src: Source) -> dict[tuple[int, int], float]:
        """One source's threat on each tile it reaches, before any fade with age."""
        known = len(self.terrain) if self.terrain is not None else 0
        key = (src.kind, src.x, src.y, src.unit_type, src.strength, src.naval, known)
        if key in self._fields:
            return self._fields[key]
        width = self.settings.map_width
        field_: dict[tuple[int, int], float] = {}
        if self.terrain is None:
            reach = self._reach(src)
            for tx, ty in self._tiles_around(src):
                d = hex_distance(tx, ty, src.x, src.y, width)
                field_[tx, ty] = src.strength * (1 - d / (reach + 1))
        else:
            moves, naval, rng, horizon = self._profile(src)
            turns = self.terrain.reach(src.x, src.y, moves, naval, horizon, rng)
            for tile, t in turns.items():
                if t <= horizon:
                    field_[tile] = src.strength * (1 - t / (horizon + 1))
        self._fields[key] = field_
        return field_

    def _fade(self, src: Source, turn: int | None) -> float:
        if src.kind == UNIT and turn is not None:
            return max(0.0, 1 - (src.age(turn) or 0) / self.settings.unit_memory_turns)
        return 1.0

    def threat_at(self, x: int, y: int, turn: int | None = None) -> float:
        """Threat on a tile: what each source can bring to it, by the turns it needs to arrive
        (or by tile distance with no terrain store). A unit source also fades with age; a camp
        does not (it stays until seen gone)."""
        return sum(
            self._field(src).get((x, y), 0.0) * self._fade(src, turn) for src in self.sources
        )

    def danger_near(self, x: int, y: int, radius: int, turn: int | None = None) -> float:
        """The most threat on any tile within `radius` of (x, y)."""
        width = self.settings.map_width
        tiles = {t for src in self.sources for t in self._field(src)}
        near = (t for t in tiles if hex_distance(x, y, t[0], t[1], width) <= radius)
        return max((self.threat_at(tx, ty, turn) for tx, ty in near), default=0.0)

    def _tiles_around(self, source: Source) -> list[tuple[int, int]]:
        reach = self._reach(source)
        width = self.settings.map_width
        return [
            (x, y)
            for y in range(source.y - reach, source.y + reach + 1)
            for x in range(source.x - reach - 1, source.x + reach + 2)
            if hex_distance(x, y, source.x, source.y, width) <= reach
        ]

    def camps(self) -> list[Source]:
        return [s for s in self.sources if s.kind == CAMP]

    def near_city(self, source: Source, cities: Iterable[City]) -> int | None:
        """Distance to our nearest city, or None with no city."""
        w = self.settings.map_width
        return min((hex_distance(source.x, source.y, c.x, c.y, w) for c in cities), default=None)

    # ----- the dump -----

    def heat(self, turn: int) -> list[list[float]]:
        """Every tile with threat, hottest first, as [x, y, value]."""
        tiles = {t for src in self.sources for t in self._field(src)}
        values = {t: self.threat_at(*t, turn) for t in tiles}
        ranked = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
        return [[x, y, round(v, 1)] for (x, y), v in ranked if v >= self.settings.heat_floor]

    def record(
        self, turn: int, cities: Iterable[City], units: Iterable[Unit] = ()
    ) -> dict[str, Any]:
        cities = list(cities)
        rows = []
        for src in sorted(self.sources, key=lambda s: (s.kind, s.x, s.y)):
            row = asdict(src)
            row["age"] = src.age(turn)
            row["city_distance"] = self.near_city(src, cities)
            rows.append(row)
        near = [
            r for r in rows
            if r["kind"] == CAMP
            and r["city_distance"] is not None
            and r["city_distance"] <= self.settings.near_city
        ]  # fmt: skip
        return {
            "turn": turn,
            "sources": rows,
            "camps_near_city": len(near),
            "heat": self.heat(turn),
            # Where we stand, so a picture of the map can show our cities (no threat there) and
            # units. [x, y, name] and [x, y, unit_type].
            "cities": [[c.x, c.y, c.name] for c in cities],
            "units": [[u.x, u.y, u.unit_type] for u in units if u.is_combat],
            # Ground learned since the last line, "x,y": code (see terrain.Ground.code).
            "terrain_new": {
                f"{x},{y}": code
                for (x, y), code in (self.terrain.take_new() if self.terrain else {}).items()
            },
        }


class ThreatMapHandler:
    """Keeps the map each turn and writes its dump. Runs before the policies."""

    name = "threat_map"

    def __init__(
        self,
        settings: ThreatMapSettings | None = None,
        dump: Path | None = None,
        threat_map: ThreatMap | None = None,
        terrain: TerrainStore | None = None,
    ) -> None:
        settings = settings or ThreatMapSettings()
        self.terrain = terrain if terrain is not None else TerrainStore(settings.map_width)
        self.map = threat_map or ThreatMap(settings, terrain=self.terrain)
        if self.map.terrain is None:
            self.map.terrain = self.terrain
        self.settings = self.map.settings
        self._dump = dump

    async def run(self, game: Any) -> None:
        turn = (await game.get_game_overview()).turn
        cities = await read_cities(game, self.settings.map_width)
        units = await read_units(game, self.settings.map_width)
        self.map.see_threats(turn, await read_threats(game))
        if self.map.due_for_scan(turn):
            self.map.mark_scanned(turn)
            baseline = self.map.take_baseline()
            for city in cities:
                try:
                    tiles = await read_area(game, city.x, city.y, self.settings.scan_radius)
                except ConnectionError:
                    raise
                except Exception:
                    log.exception("Threat map: reading the area around a city failed")
                    continue
                self.terrain.learn(tiles)
                self.map.see_tiles(turn, tiles, baseline)
        await self._read_ground_around_sources(game, turn)
        if self._dump is not None:
            with self._dump.open("a", encoding="utf-8") as out:
                out.write(json.dumps(self.map.record(turn, cities, units)) + "\n")

    async def _read_ground_around_sources(self, game: Any, turn: int) -> None:
        """Terrain around each source, so its reach follows the ground. Terrain doesn't change,
        so an area is read once; one that revealed nothing new is not read again for a while."""
        s = self.settings
        for src in list(self.map.sources):
            if self.terrain.unknown_near(src.x, src.y, s.read_radius) == 0:
                continue
            if self.terrain.tried_within(src.x, src.y, turn, s.read_retry_turns):
                continue
            self.terrain.mark_tried(src.x, src.y, turn)
            try:
                self.terrain.learn(await read_area(game, src.x, src.y, s.read_radius))
            except ConnectionError:
                raise
            except Exception:
                log.exception("Threat map: reading the ground around a source failed")
