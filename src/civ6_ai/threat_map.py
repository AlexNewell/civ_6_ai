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

from civ6_ai.state import read_cities, read_threats
from civ6_ai.state.models import City, Threat, Tile
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance, read_area

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
    heat_tiles: int = 40  # Tiles written to the dump, hottest first.


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
    _last_scan: int = -10_000

    # ----- updating -----

    def due_for_scan(self, turn: int) -> bool:
        return turn - self._last_scan >= self.settings.scan_turns

    def mark_scanned(self, turn: int) -> None:
        self._last_scan = turn

    def see_tiles(self, turn: int, tiles: Iterable[Tile]) -> None:
        """Camps from tiles read this turn. A camp on any revealed tile is kept; one is dropped
        only when a visible tile shows no camp, since a fogged tile can't be believed gone."""
        for tile in tiles:
            known = next(
                (s for s in self.sources if s.kind == CAMP and (s.x, s.y) == (tile.x, tile.y)),
                None,
            )
            if tile.improvement == CAMP_IMPROVEMENT:
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

    def threat_at(self, x: int, y: int, turn: int | None = None) -> float:
        """Threat on a tile: each source fades linearly with distance. A unit source also fades
        with age; a camp does not (it stays until seen gone)."""
        s = self.settings
        total = 0.0
        for src in self.sources:
            reach = self._reach(src)
            d = hex_distance(x, y, src.x, src.y, s.map_width)
            if d > reach:
                continue
            weight = src.strength * (1 - d / (reach + 1))
            if src.kind == UNIT and turn is not None:
                weight *= max(0.0, 1 - (src.age(turn) or 0) / s.unit_memory_turns)
            total += weight
        return total

    def danger_near(self, x: int, y: int, radius: int, turn: int | None = None) -> float:
        """The most threat on any tile within `radius` of (x, y)."""
        best = 0.0
        for src in self.sources:
            if hex_distance(x, y, src.x, src.y, self.settings.map_width) > radius + self._reach(
                src
            ):
                continue
            for tx, ty in self._tiles_around(src):
                if hex_distance(x, y, tx, ty, self.settings.map_width) <= radius:
                    best = max(best, self.threat_at(tx, ty, turn))
        return best

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
        """Hottest tiles first, as [x, y, value]."""
        seen: dict[tuple[int, int], float] = {}
        for src in self.sources:
            for tx, ty in self._tiles_around(src):
                if (tx, ty) not in seen:
                    seen[tx, ty] = self.threat_at(tx, ty, turn)
        ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
        return [[x, y, round(v, 1)] for (x, y), v in ranked[: self.settings.heat_tiles]]

    def record(self, turn: int, cities: Iterable[City]) -> dict[str, Any]:
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
        }


class ThreatMapHandler:
    """Keeps the map each turn and writes its dump. Runs before the policies."""

    name = "threat_map"

    def __init__(
        self,
        settings: ThreatMapSettings | None = None,
        dump: Path | None = None,
        threat_map: ThreatMap | None = None,
    ) -> None:
        self.map = threat_map or ThreatMap(settings or ThreatMapSettings())
        self.settings = self.map.settings
        self._dump = dump

    async def run(self, game: Any) -> None:
        turn = (await game.get_game_overview()).turn
        cities = await read_cities(game, self.settings.map_width)
        self.map.see_threats(turn, await read_threats(game))
        if self.map.due_for_scan(turn):
            self.map.mark_scanned(turn)
            for city in cities:
                try:
                    tiles = await read_area(game, city.x, city.y, self.settings.scan_radius)
                except ConnectionError:
                    raise
                except Exception:
                    log.exception("Threat map: reading the area around a city failed")
                    continue
                self.map.see_tiles(turn, tiles)
        if self._dump is not None:
            with self._dump.open("a", encoding="utf-8") as out:
                out.write(json.dumps(self.map.record(turn, cities)) + "\n")
