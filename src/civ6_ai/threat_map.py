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
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from civ_mcp.lua._helpers import SENTINEL

from civ6_ai.policies.game_data import COMBAT_UNITS
from civ6_ai.policies.settling import _SEA_UNITS as _POLICY_SEA_UNITS
from civ6_ai.state import read_cities, read_threats, read_units
from civ6_ai.state.models import City, Threat, Tile, Unit
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance, read_area
from civ6_ai.terrain import TerrainStore

log = logging.getLogger(__name__)

BARBARIAN_ID = 63
_RANGED_SHIPS = ("QUADRIREME", "FRIGATE", "BATTLESHIP", "CRUISER", "DESTROYER", "MISSILE")
UNIT_MAX_HP = 100.0  # COMBAT_MAX_HIT_POINTS: a unit at full health.
CAMP_IMPROVEMENT = "IMPROVEMENT_BARBARIAN_CAMP"
CAMP = "camp"
UNIT = "unit"
_SEA_UNITS = (
    *_POLICY_SEA_UNITS,
    "TRIREME",
    "GALLEASS",
)  # Every ship the policies know, plus the older names this map met first.


@dataclass(frozen=True)
class ThreatMapSettings:
    """Bounds of the map. All overridable; none is a rule (see the issue's settings split)."""

    map_width: int | None = STANDARD_MAP_WIDTH
    # A camp's strength is that of the strongest barbarian land unit seen so far (they spawn
    # at the barbarians' tech, which can be far from ours); until one is seen, this floor.
    camp_strength: float = 15.0
    camp_radius: int = 5  # Tiles over which a camp's threat fades to nothing.
    unit_radius: int = 3  # The same for a hostile unit.
    unit_memory_turns: int = 10  # A hostile unit not seen again is dropped after this many turns.
    resight_radius: int = 3  # A new sighting this close replaces an older one of the same kind.
    scan_radius: int = 7  # Tiles read around each city for camps.
    baseline_radius: int = 14  # The first read of a session looks this far around each city,
    # to take in every camp the saved game's map already shows.
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
    # Our side. Cover is what our units and cities answer with; it is not threat's opposite, so
    # the two are kept as separate layers and combined by whoever decides.
    own_horizon_turns: float = 2.0  # Our unit's cover fades out over this many turns of reach.
    # A city cannot strike at range without walls, so an unwalled city gives no cover beside it;
    # with walls it covers tiles within `ranged_reach`. Our units' cover spreads as before.
    # A city is short of defenders (issue 267) when the attackers that can reach it this turn or
    # next (`attack_horizon_turns`) would break its walls, or empty its hit points against its
    # healing, within `siege_turns` (about the time to build or buy a defender), and the loss
    # rate a turn is more than `short_margin` hit points. The damage is worked out as the game
    # resolves it: see `ThreatMap.city_reading`.
    attack_horizon_turns: float = 2.0
    siege_turns: float = 6.0
    short_margin: float = 0.0
    # Fog is mild threat: a tile beside ground we have never seen could hold anything.
    fog_penalty: float = 3.0  # At most, on a tile whose surroundings are all unseen.
    fog_radius: int = 2  # Tiles around a tile that are looked at for unseen ground.
    fog_range: int = 10  # Fog is counted this close to a city of ours (further it is just far).
    vision: int = 1  # A source this close to one of ours, and not seen, is no longer there.
    read_radius: int = 6  # Tiles of terrain read around a source that has none yet.
    read_retry_turns: int = 10  # Turns before an area that revealed nothing new is read again.
    # A camp nobody has seen, inferred from where barbarian units first show up (issue 293).
    suspect_sightings: int = 3  # Arrivals from the same area before it is suspected.
    suspect_radius: int = 4  # Arrivals this close to one another are one area.
    suspect_range: int = 9  # Only arrivals this close to a city of ours count.
    suspect_lead: int = 3  # Tiles past the arrivals, away from the city, the camp is guessed at.
    suspect_look: int = 2  # One of our units this close to the guess has seen the ground there.
    suspect_memory: int = 60  # Most arrivals kept (a bound on memory, not a timer).


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
    hp_frac: float = 1.0  # Hit points left over the most it can have, as last seen.
    defence: float = 0.0  # Its combat strength when it defends; 0 when not known.

    def age(self, turn: int) -> int | None:
        return None if self.last_seen is None else turn - self.last_seen


@dataclass(frozen=True)
class Arrival:
    """A barbarian land unit first seen at a tile, with no earlier sighting of it close by."""

    turn: int
    x: int
    y: int
    unit_type: str


@dataclass(frozen=True)
class Suspect:
    """A camp that is probably there but unseen: where, from how many arrivals, and why."""

    x: int
    y: int
    arrivals: int
    turns: int  # Distinct turns the arrivals were seen on.
    first: int  # Turn of the earliest arrival.
    city: tuple[int, int]  # Our nearest city.

    def as_row(self) -> dict[str, Any]:
        return {
            "x": self.x, "y": self.y, "arrivals": self.arrivals, "turns": self.turns,
            "first": self.first, "city": list(self.city),
        }  # fmt: skip


@dataclass
class Ally:
    """One of ours that gives cover: a combat unit, or a city."""

    kind: str  # "unit" or "city"
    x: int
    y: int
    strength: float
    unit_type: str = ""
    naval: bool = False
    base: float = 0.0  # A unit's strength at full health.
    unit_id: int = 0  # Of a unit, so a move this turn can be applied (`relocate`).
    walls: bool = False  # A city with walls shoots at range, so it covers tiles beside it.
    # A unit's strength when it defends (its combat strength, not its ranged one), at full health.
    defence_base: float = 0.0
    hp_frac: float = 1.0  # A unit's hit points over its most.
    # A city's hit points, and its walls', as the game reports them.
    hp: float = 0.0
    max_hp: float = 0.0
    wall_hp: float = 0.0
    wall_max_hp: float = 0.0


def _city_strength(city: City) -> float:
    """A city's own strength as the game reports it, less what damage has taken off (its ranged
    strike is on that scale). Walls are not added: they are hit points (`wall_hp`), not strength.
    The garrison's own stats are not counted here: our units on the tile defend with their own."""
    hp = city.garrison_hp / city.garrison_max_hp if city.garrison_max_hp > 0 else 1.0
    return float(city.defense_strength) * min(1.0, hp)


@dataclass(frozen=True)
class CityReading:
    """What the map says about one city, on its own tile.

    `defender` is the strength each attacker is hit against (the garrison unit's, else the
    city's). `damage` is what the attackers that can reach the city deal in a turn to its hit
    points once walls are down, after our units nearby have struck them; `wall_damage` is what
    they deal to the walls. `net` is the hit points lost a turn now (walls take it first, and do
    not heal under attack; the city's own hit points heal), never below zero. `turns` is how long
    until the walls break, or the city falls if it has none, at that rate; the city is short when
    `net` is above the margin and `turns` is within `horizon`.
    """

    threat: float
    fog: float
    cover: float
    damage: float
    net: float
    defender: float = 0.0
    wall_damage: float = 0.0
    turns: float = float("inf")
    horizon: float = 0.0

    def pressed(self, margin: float = 0.0) -> bool:
        return self.net > margin and self.turns <= self.horizon


# The game's own combat-healing rules (GlobalParameters). Walls do not heal for
# `outer_defence_cooldown` turns after an attack, so during a siege only the city's own hit points
# come back, and an attacking force makes progress only by out-damaging that.
@dataclass(frozen=True)
class CombatHeal:
    city_garrison: float = 20.0  # COMBAT_HEAL_CITY_GARRISON: city hit points healed per turn.
    outer_defences: float = (
        10.0  # COMBAT_HEAL_CITY_OUTER_DEFENSES (1 in Base, 10 in the expansions).
    )
    outer_defence_cooldown: float = 3.0  # COMBAT_HEAL_OUTER_DEFENSES_COOLDOWN, in turns.
    # What share of an attack's damage reaches a city's walls while they stand, by the kind of
    # attacker: COMBAT_DEFENSE_DAMAGE_PERCENT_MELEE, _RANGED and _BOMBARD (siege).
    wall_percent_melee: float = 15.0
    wall_percent_ranged: float = 50.0
    wall_percent_bombard: float = 100.0
    # Where the values came from: "game" (read), or a fallback with its reason. Not compared.
    source: str = field(default="table", compare=False)


# What is used when the read fails: the values the installed game's tables give.
DEFAULT_COMBAT_HEAL = CombatHeal()
_HEAL_PARAMETERS = {
    "COMBAT_HEAL_CITY_GARRISON": "city_garrison",
    "COMBAT_HEAL_CITY_OUTER_DEFENSES": "outer_defences",
    "COMBAT_HEAL_OUTER_DEFENSES_COOLDOWN": "outer_defence_cooldown",
    "COMBAT_DEFENSE_DAMAGE_PERCENT_MELEE": "wall_percent_melee",
    "COMBAT_DEFENSE_DAMAGE_PERCENT_RANGED": "wall_percent_ranged",
    "COMBAT_DEFENSE_DAMAGE_PERCENT_BOMBARD": "wall_percent_bombard",
}
COMBAT_HEAL_LUA = f"""
for name, _ in pairs({{{", ".join(f"{n} = true" for n in _HEAL_PARAMETERS)}}}) do
    pcall(function()
        local row = GameInfo.GlobalParameters[name]
        if row ~= nil then print("HEAL|" .. name .. "|" .. tostring(row.Value)) end
    end)
end
print("HEALDONE")
print("{SENTINEL}")
"""


def parse_combat_heal(output: str) -> CombatHeal:
    """Parse the read. Raises ValueError unless its `HEALDONE` line is present and it names the
    city garrison heal. Parameters the reply leaves out keep their defaults."""
    if "HEALDONE" not in output.split():
        raise ValueError("combat heal read incomplete: no end marker")
    found: dict[str, float] = {}
    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0] == "HEAL" and parts[1] in _HEAL_PARAMETERS:
            try:
                found[_HEAL_PARAMETERS[parts[1]]] = float(parts[2])
            except ValueError:
                continue
    if "city_garrison" not in found:
        raise ValueError("combat heal read has no city garrison value")
    return CombatHeal(**{**asdict(DEFAULT_COMBAT_HEAL), **found, "source": "game"})


async def read_combat_heal(game: Any) -> CombatHeal:
    """The game's healing rules, or the defaults when the read fails (logged once)."""
    try:
        reply = await game.execute_lua(COMBAT_HEAL_LUA, "ingame")
        return parse_combat_heal(reply if isinstance(reply, str) else chr(10).join(reply))
    except ConnectionError:
        raise
    except Exception as err:
        log.warning("Combat heal read failed, using the defaults: %s", err)
        return replace(DEFAULT_COMBAT_HEAL, source=f"fallback ({err})")


_CIVILIAN_WORDS = ("SETTLER", "BUILDER", "TRADER", "MISSIONARY", "APOSTLE", "GREAT_", "ENGINEER")


def foreign_civilian(entry: str) -> tuple[str, str] | None:
    """(owner, unit) for a map-area unit entry like "Barbarian SETTLER", or None if not civilian."""
    owner, _, unit = entry.rpartition(" ")
    if not owner or not any(word in unit for word in _CIVILIAN_WORDS):
        return None
    return owner, unit


@dataclass(frozen=True)
class Civilian:
    """A foreign civilian unit seen on a visible tile (issue 280)."""

    x: int
    y: int
    owner: str
    unit: str
    seen: int  # The turn.
    guarded: bool = False  # A soldier stands on the tile too: not a prize to walk onto.


@dataclass
class ThreatMap:
    settings: ThreatMapSettings = field(default_factory=ThreatMapSettings)
    sources: list[Source] = field(default_factory=list)
    terrain: TerrainStore | None = None
    heal: CombatHeal = field(default_factory=lambda: DEFAULT_COMBAT_HEAL)  # Read once a session.
    barbarian_peak: float = 0.0  # Strongest barbarian land unit seen this session.
    allies: list["Ally"] = field(default_factory=list)
    arrivals: list[Arrival] = field(default_factory=list)
    # Turn each tile was last read while visible (camp check), to find units on unread tiles.
    _tile_read: dict[tuple[int, int], int] = field(default_factory=dict)
    # Foreign civilians on visible tiles, by tile (issue 280): the tile reads already tell us.
    civilians: dict[tuple[int, int], "Civilian"] = field(default_factory=dict)
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
            if tile.visible:
                self._tile_read[tile.x, tile.y] = turn
                self._see_civilian(turn, tile)
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
                self._drop_arrivals_near(known.x, known.y, self.settings.camp_radius)

    def _see_civilian(self, turn: int, tile: Tile) -> None:
        """Keep a foreign civilian on a visible tile; a visible tile without one forgets it."""
        found = [c for c in map(foreign_civilian, tile.foreign_units) if c is not None]
        if found:
            # Kept with a soldier too (issue 283): a settler we lost stands under the unit that
            # took it, and that is how the capture is told. Recapture leaves a guarded one.
            owner, unit = found[0]
            guarded = len(found) < len(tile.foreign_units)
            self.civilians[tile.x, tile.y] = Civilian(tile.x, tile.y, owner, unit, turn, guarded)
        else:
            self.civilians.pop((tile.x, tile.y), None)

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
                hp_frac=min(1.0, t.hp / t.max_hp) if t.max_hp > 0 else 1.0,
                defence=float(t.combat_strength),
            )
            for t in threats
            if t.hostile
        ]
        for f in fresh:
            if f.hostile_owner == BARBARIAN_ID and not f.naval:
                self.barbarian_peak = max(self.barbarian_peak, f.strength)
        known = [o for o in self.sources if o.kind == UNIT]
        for f in fresh:
            if f.hostile_owner != BARBARIAN_ID or f.naval or "SCOUT" in f.unit_type:
                continue
            continuing = any(
                o.unit_type == f.unit_type
                and hex_distance(f.x, f.y, o.x, o.y, s.map_width) <= s.resight_radius
                for o in known
            )
            explained = any(
                hex_distance(f.x, f.y, c.x, c.y, s.map_width) <= s.camp_radius for c in self.camps()
            )
            if not continuing and not explained:
                self.arrivals.append(Arrival(turn, f.x, f.y, f.unit_type))
        del self.arrivals[: -s.suspect_memory]
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

    def see_own(self, turn: int, units: Iterable[Unit], cities: Iterable[City]) -> None:
        """Our side for this turn: it gives cover, and it can show a remembered unit is gone.

        A hostile unit we are not seeing now, remembered from an earlier turn, is dropped when one
        of ours stands on or beside its tile: we would see it, so it has moved on (Run 17: a
        barbarian scout last seen on turn 57 was drawn for ten turns, with our unit standing on it
        from turn 61).
        """
        s = self.settings
        units = list(units)
        self.allies = [
            Ally(
                "unit",
                u.x,
                u.y,
                float(max(u.combat_strength, u.ranged_strength)) * (u.hp / max(u.max_hp, 1)),
                u.unit_type,
                any(w in u.unit_type for w in _SEA_UNITS),
                base=float(max(u.combat_strength, u.ranged_strength)),
                unit_id=u.unit_id,
                defence_base=float(u.combat_strength),
                hp_frac=u.hp / max(u.max_hp, 1),
            )
            for u in units
            if u.is_combat
        ] + [
            Ally(
                "city",
                c.x,
                c.y,
                _city_strength(c),
                walls=c.has_walls,
                hp=float(c.garrison_hp),
                max_hp=float(c.garrison_max_hp),
                wall_hp=float(c.wall_hp),
                wall_max_hp=float(c.wall_max_hp),
            )
            for c in cities
        ]
        suspect = self.suspected_camp()
        if suspect is not None and any(
            hex_distance(u.x, u.y, suspect.x, suspect.y, s.map_width) <= s.suspect_look
            for u in units
        ):
            reach = s.suspect_radius + s.suspect_lead
            dropped = self._drop_arrivals_near(suspect.x, suspect.y, reach)
            log.info(
                "suspected_camp_checked %s",
                json.dumps({"turn": turn, **suspect.as_row(), "dropped": dropped}),
            )
        self.sources = [
            src
            for src in self.sources
            if not (
                src.kind == UNIT
                and src.last_seen != turn
                and any(
                    hex_distance(a.x, a.y, src.x, src.y, s.map_width) <= s.vision
                    for a in self.allies
                )
            )
        ]

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
        return (*self._unit_profile(src.unit_type), s.unit_horizon_turns)

    def _unit_profile(self, unit_type: str) -> tuple[float, bool, int]:
        """(moves per turn, naval, shooting reach) for a unit of this type."""
        s = self.settings
        if any(w in unit_type for w in _SEA_UNITS):
            return s.ship_moves, True, 0
        stats = COMBAT_UNITS.get(unit_type)
        klass = stats[4] if stats else ""
        moves = s.land_moves
        if klass in ("LIGHT_CAVALRY", "HEAVY_CAVALRY") or "HORSE" in unit_type:
            moves = s.cavalry_moves
        elif klass == "RECON" or "SCOUT" in unit_type:
            moves = s.scout_moves
        ranged = (stats is not None and stats[1] > 0) or klass in ("RANGED", "SIEGE")
        return moves, False, s.ranged_reach if ranged else s.melee_reach

    def _field(self, src: Source) -> dict[tuple[int, int], float]:
        """One source's threat on each tile it reaches, before any fade with age."""
        known = self.terrain.revision if self.terrain is not None else 0
        strength = self.strength_of(src)
        key = (src.kind, src.x, src.y, src.unit_type, strength, src.naval, known)
        if key in self._fields:
            return self._fields[key]
        width = self.settings.map_width
        field_: dict[tuple[int, int], float] = {}
        if self.terrain is None:
            reach = self._reach(src)
            for tx, ty in self._tiles_around(src):
                d = hex_distance(tx, ty, src.x, src.y, width)
                field_[tx, ty] = strength * (1 - d / (reach + 1))
        else:
            moves, naval, rng, horizon = self._profile(src)
            turns = self.terrain.reach(src.x, src.y, moves, naval, horizon, rng)
            for tile, t in turns.items():
                if t <= horizon:
                    field_[tile] = strength * (1 - t / (horizon + 1))
        self._fields[key] = field_
        return field_

    def _cover_field(self, ally: Ally) -> dict[tuple[int, int], float]:
        s = self.settings
        key = ("cover", ally.kind, ally.x, ally.y, ally.strength, ally.unit_type, ally.walls)
        known = self.terrain.revision if self.terrain is not None else 0
        key = (*key, known)
        if key in self._fields:
            return self._fields[key]
        field_: dict[tuple[int, int], float] = {}
        if ally.kind == "city" or self.terrain is None:
            # An unwalled city covers its own tile only: it cannot strike at range.
            radius = (
                (s.ranged_reach if ally.walls else 0)
                if ally.kind == "city"
                else (s.melee_reach + 1)
            )
            for y in range(ally.y - radius, ally.y + radius + 1):
                for x in range(ally.x - radius - 1, ally.x + radius + 2):
                    d = hex_distance(x, y, ally.x, ally.y, s.map_width)
                    if d <= radius:
                        field_[self._wrap(x), y] = ally.strength * (1 - d / (radius + 1))
        else:
            moves, naval, rng = self._unit_profile(ally.unit_type)
            turns = self.terrain.reach(ally.x, ally.y, moves, naval, s.own_horizon_turns, rng)
            for tile, t in turns.items():
                if t <= s.own_horizon_turns:
                    field_[tile] = ally.strength * (1 - t / (s.own_horizon_turns + 1))
        self._fields[key] = field_
        return field_

    def cover_at(self, x: int, y: int) -> float:
        """What our units and cities can bring to a tile, by the same turns-to-arrive measure."""
        return sum(self._cover_field(a).get((x, y), 0.0) for a in self.allies)

    def fog_at(self, x: int, y: int) -> float:
        """A mild threat from ground never seen near the tile, only close to our cities, where
        not knowing what stands next door matters. Zero with no terrain store."""
        s = self.settings
        if self.terrain is None or not any(
            a.kind == "city" and hex_distance(x, y, a.x, a.y, s.map_width) <= s.fog_range
            for a in self.allies
        ):
            return 0.0
        total = unseen = 0
        for ty in range(y - s.fog_radius, y + s.fog_radius + 1):
            for tx in range(x - s.fog_radius - 1, x + s.fog_radius + 2):
                if hex_distance(x, y, tx, ty, s.map_width) <= s.fog_radius:
                    total += 1
                    unseen += self.terrain.get(self._wrap(tx), ty) is None
        return s.fog_penalty * unseen / total if total else 0.0

    def net_at(self, x: int, y: int, turn: int | None = None) -> float:
        """Threat plus fog, less our cover, never below zero."""
        return max(0.0, self.threat_at(x, y, turn) + self.fog_at(x, y) - self.cover_at(x, y))

    def _turns_to(self, src: Source, x: int, y: int) -> float | None:
        """Turns before `src` can strike the tile, or None if it cannot within its horizon.
        Worked back from the threat field: zero when it is already in range."""
        strength = self.strength_of(src)
        reach = self._field(src).get((x, y), 0.0)
        if strength <= 0 or reach <= 0:
            return None
        share = min(1.0, reach / strength)
        moves, _naval, rng, horizon = self._profile(src)
        if self.terrain is not None:
            return (1 - share) * (horizon + 1)
        distance = (1 - share) * (self._reach(src) + 1)
        return max(0.0, distance - rng) / moves

    def _attack_class(self, src: Source) -> str:
        """ "melee", "ranged" or "siege": how much of its damage the walls take."""
        if src.kind == CAMP:
            return "melee"
        stats = COMBAT_UNITS.get(src.unit_type)
        klass = stats[4] if stats else ""
        if klass == "SIEGE":
            return "siege"
        # Ships are melee (Galley, Caravel, Ironclad) or ranged (Quadrireme, Frigate and later).
        shoots = any(w in src.unit_type for w in _RANGED_SHIPS)
        if shoots or klass == "RANGED" or (stats is not None and stats[1] > 0):
            return "ranged"
        return "melee"

    def city_reading(self, x: int, y: int, turn: int | None = None) -> CityReading:
        """The map's view of the city at (x, y), judged as the game resolves an attack (issue 267).

        Each source that can reach the city this turn or next hits the *defender*: the best of our
        units standing in the city by its own strength, or the city's own strength with none.
        The damage is the game's formula (`expected_damage`). While walls stand they take a share
        of it by the attacker's kind (the game's COMBAT_DEFENSE_DAMAGE_PERCENT values) and the
        city's hit points take none; they do not heal under attack. Our other units that can
        reach the city, and the city's own strike when it has walls, each hit one attacker first,
        taking away the share of its hit points the blow removes (all of it for a kill). They cut
        the attack; they do not raise the defender's strength.

        Neighbouring tiles are not judged: an unwalled city covers none of them.
        """
        from civ6_ai.policies.military import expected_damage  # Military imports this module.

        s = self.settings
        cover = self.cover_at(x, y)
        city = next((a for a in self.allies if a.kind == "city" and (a.x, a.y) == (x, y)), None)
        here = [a for a in self.allies if a.kind == "unit" and (a.x, a.y) == (x, y) and not a.naval]
        defender_unit = max(here, key=lambda a: a.defence_base * a.hp_frac, default=None)
        if defender_unit is not None:
            defender = defender_unit.defence_base * defender_unit.hp_frac
        else:
            defender = city.strength if city is not None else 0.0
        # The attackers that arrive in time: strength, kind, hit points, and how fresh the sighting.
        defences: list[float] = []  # What our blows are hit against.
        spots: list[tuple[int, int]] = []
        kinds: list[str] = []
        hit_points: list[float] = []
        base: list[float] = []
        for src in self.sources:
            when = self._turns_to(src, x, y)
            if when is None or when > s.attack_horizon_turns:
                continue
            strength = self.strength_of(src)
            defences.append(src.defence if src.defence > 0 else strength)
            spots.append((src.x, src.y))
            kinds.append(self._attack_class(src))
            hit_points.append(max(1.0, UNIT_MAX_HP * (src.hp_frac if src.kind == UNIT else 1.0)))
            base.append(expected_damage(strength, defender) * self._fade(src, turn))
        # Our blows: (strength, tile, tiles it strikes from). A blow can only land on an attacker
        # it reaches: a unit's moves in a turn plus its shooting reach, the city's strike its range.
        blows: list[tuple[float, int, int, int]] = []
        for a in self.allies:
            if a.kind != "unit" or a.strength <= 0:
                continue
            moves, _naval, rng = self._unit_profile(a.unit_type)
            if a is defender_unit:
                # A garrison stays in the city; only a ranged one can shoot from it.
                if a.base > a.defence_base:
                    blows.append((a.strength, a.x, a.y, rng))
            elif self._cover_field(a).get((x, y), 0.0) > 0:
                blows.append((a.strength, a.x, a.y, int(moves) + rng))
        if city is not None and city.walls and city.strength > 0:
            blows.append((city.strength, city.x, city.y, s.ranged_reach))
        blows.sort(reverse=True)
        # What share of each attacker is left to hit: each blow goes where it takes off the most.
        left = [1.0] * len(base)
        for blow, bx, by, reach in blows:
            best, best_cut, best_gain = -1, 0.0, 0.0
            for i, (sx, sy) in enumerate(spots):
                if left[i] <= 0 or hex_distance(bx, by, sx, sy, s.map_width) > reach:
                    continue
                cut = min(left[i], expected_damage(blow, defences[i]) / hit_points[i])
                if base[i] * cut > best_gain:
                    best, best_cut, best_gain = i, cut, base[i] * cut
            if best < 0:
                continue
            left[best] -= best_cut
        heal = self.heal
        percent = {
            "melee": heal.wall_percent_melee,
            "ranged": heal.wall_percent_ranged,
            "siege": heal.wall_percent_bombard,
        }
        damage = sum(b * k for b, k in zip(base, left, strict=True))
        wall_damage = sum(
            b * k * percent[kind] / 100.0 for kind, b, k in zip(kinds, base, left, strict=True)
        )
        city_hp = city.hp if city is not None else 0.0
        wall_hp = city.wall_hp if city is not None else 0.0
        if wall_hp > 0:
            net = wall_damage  # Walls take it first and do not heal under attack.
            turns = wall_hp / wall_damage if wall_damage > 0 else float("inf")
        else:
            net = max(0.0, damage - heal.city_garrison)
            turns = city_hp / net if net > 0 else float("inf")
        return CityReading(
            threat=self.threat_at(x, y, turn),
            fog=self.fog_at(x, y),
            cover=cover,
            damage=damage,
            net=net,
            defender=defender,
            wall_damage=wall_damage,
            turns=turns,
            horizon=s.siege_turns,
        )

    def relocate(
        self, moves: dict[int, tuple[int, int]], health: dict[int, float] | None = None
    ) -> None:
        """Our units that moved this turn (unit id to tile): their cover goes with them. `health`
        is a unit id to its fraction of hit points left, for those that took damage."""
        for ally in self.allies:
            if ally.kind != "unit":
                continue
            if ally.unit_id in moves:
                ally.x, ally.y = moves[ally.unit_id]
            if health and ally.unit_id in health:
                ally.hp_frac = max(0.0, min(1.0, health[ally.unit_id]))
                ally.strength = ally.base * ally.hp_frac

    def forget_units_at(self, tiles: Iterable[tuple[int, int]]) -> int:
        """Drop hostile unit sources standing on these tiles (they died this turn). Returns how
        many went. A camp is not a unit and stays until a visible tile shows it gone."""
        gone = set(tiles)
        before = len(self.sources)
        self.sources = [s for s in self.sources if not (s.kind == UNIT and (s.x, s.y) in gone)]
        return before - len(self.sources)

    def strength_of(self, src: Source) -> float:
        """A unit's own combat value; a camp's is the strongest barbarian unit seen."""
        if src.kind == CAMP:
            return max(src.strength, self.barbarian_peak)
        return src.strength

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

    def camps_reaching(self, x: int, y: int) -> list[tuple[Source, float]]:
        """Each camp whose reach covers this tile, with the threat it brings there. Our cover
        is not taken off: a camp keeps sending units whatever stands on the tile (issue 276)."""
        found = [(c, self._field(c).get((x, y), 0.0)) for c in self.camps()]
        return [(c, v) for c, v in found if v > 0.0]

    def raw_danger_at(self, x: int, y: int, turn: int | None = None) -> float:
        """What a civilian on this tile faces: hostile strength that can reach it, with our cover
        NOT taken off (a Trader or Settler cannot fight back, so cover only lets us avenge it),
        and any camp's reach (issue 287). Not reduced by fog or cover, unlike net_at."""
        return max(
            self.threat_at(x, y, turn),
            max((v for _, v in self.camps_reaching(x, y)), default=0.0),
        )

    def nearest_hostile(
        self, tiles: Iterable[tuple[int, int]], turn: int | None = None
    ) -> tuple[Source, int] | None:
        """The nearest remembered hostile unit to any of `tiles` and its tile distance."""
        width = self.settings.map_width
        tiles = list(tiles)
        best: tuple[int, int, int, Source] | None = None
        for src in self.sources:
            if src.kind != UNIT or self._fade(src, turn) <= 0.0:
                continue
            d = min((hex_distance(src.x, src.y, tx, ty, width) for tx, ty in tiles), default=None)
            if d is not None and (best is None or (d, src.x, src.y) < best[:3]):
                best = (d, src.x, src.y, src)
        return None if best is None else (best[3], best[0])

    def danger_near(self, x: int, y: int, radius: int, turn: int | None = None) -> float:
        """The most threat on any tile within `radius` of (x, y)."""
        width = self.settings.map_width
        tiles = {t for src in self.sources for t in self._field(src)}
        near = (t for t in tiles if hex_distance(x, y, t[0], t[1], width) <= radius)
        return max((self.threat_at(tx, ty, turn) for tx, ty in near), default=0.0)

    def _wrap(self, x: int) -> int:
        """An x as the map stores it, wrapped east-west so tiles across the seam match."""
        width = self.settings.map_width
        return x % width if width else x

    def _tiles_around(self, source: Source) -> list[tuple[int, int]]:
        reach = self._reach(source)
        width = self.settings.map_width
        return [
            (self._wrap(x), y)
            for y in range(source.y - reach, source.y + reach + 1)
            for x in range(source.x - reach - 1, source.x + reach + 2)
            if hex_distance(x, y, source.x, source.y, width) <= reach
        ]

    def unread_hostile_tiles(self, turn: int) -> list[tuple[int, int]]:
        """Tiles holding a hostile land unit seen this turn that no read has checked for a camp
        this turn (issue 293: the periodic city scan missed a camp a passing unit saw on a
        turn between scans, and the spearman standing on it said so every turn)."""
        found = {
            (src.x, src.y)
            for src in self.sources
            if src.kind == UNIT
            and src.last_seen == turn
            and not src.naval
            and self._tile_read.get((src.x, src.y)) != turn
        }
        self._tile_read = {p: t for p, t in self._tile_read.items() if t == turn}
        return sorted(found)

    def _drop_arrivals_near(self, x: int, y: int, radius: int) -> int:
        """Forget arrivals within `radius` of a tile: their camp is found or gone."""
        w = self.settings.map_width
        before = len(self.arrivals)
        self.arrivals = [a for a in self.arrivals if hex_distance(a.x, a.y, x, y, w) > radius]
        return before - len(self.arrivals)

    def suspected_camp(self) -> Suspect | None:
        """Where an unseen camp probably is, from where barbarian units keep first appearing.

        Arrivals within `suspect_range` of one of our cities, and not within a known camp's
        reach, are grouped (`suspect_radius`). The biggest group with at least
        `suspect_sightings` arrivals on more than one turn is a suspected camp: the group's
        centre pushed `suspect_lead` tiles on, away from our nearest city (the units walk in
        from their camp), and moved off water or a mountain. It is dropped once one of ours
        stands within `suspect_look` of it (see `see_own`), and when a camp is seen near it.
        """
        s = self.settings
        w = s.map_width
        cities = [(a.x, a.y) for a in self.allies if a.kind == "city"]
        if not cities:
            return None
        pool = [
            a
            for a in self.arrivals
            if min(hex_distance(a.x, a.y, cx, cy, w) for cx, cy in cities) <= s.suspect_range
            and not any(hex_distance(a.x, a.y, c.x, c.y, w) <= s.camp_radius for c in self.camps())
        ]
        best: list[Arrival] = []
        best_seed = None
        for seed in pool:
            group = [
                a for a in pool if hex_distance(a.x, a.y, seed.x, seed.y, w) <= s.suspect_radius
            ]
            if len({a.turn for a in group}) < 2 or len(group) < s.suspect_sightings:
                continue  # A raid on one turn is no trail; it must not hide a real one.
            if len(group) > len(best):
                best, best_seed = group, seed
        if best_seed is None:
            return None

        def unwrapped(x: int) -> int:  # x as the copy nearest the seed, so a seam does not split.
            return best_seed.x + ((x - best_seed.x + w // 2) % w - w // 2) if w else x

        cx = sum(unwrapped(a.x) for a in best) / len(best)
        cy = sum(a.y for a in best) / len(best)
        city = min(cities, key=lambda c: (hex_distance(self._wrap(round(cx)), round(cy), *c, w), c))
        if w:  # Use the copy of the centre nearest the city.
            cx = min((cx + k * w for k in (-1, 0, 1)), key=lambda x: abs(x - city[0]))
        away = max(1.0, hex_distance(round(cx), round(cy), *city, None))
        factor = (away + s.suspect_lead) / away
        gx = round(city[0] + (cx - city[0]) * factor)
        gy = max(0, round(city[1] + (cy - city[1]) * factor))
        goal = self._habitable_near(self._wrap(gx), gy)
        return Suspect(
            goal[0], goal[1], len(best), len({a.turn for a in best}),
            min(a.turn for a in best), city,
        )  # fmt: skip

    def _habitable_near(self, x: int, y: int) -> tuple[int, int]:
        """The tile itself, or the nearest one within three that a camp could stand on."""
        if self.terrain is None:
            return x, y
        w = self.settings.map_width
        options = [
            (hex_distance(x, y, tx, ty, w), tx, ty)
            for ty in range(max(0, y - 3), y + 4)
            for tx in range(x - 4, x + 5)
            if hex_distance(x, y, tx, ty, w) <= 3
            and self.terrain.step_cost(self._wrap(tx), ty, naval=False) is not None
        ]
        if not options:
            return x, y
        _, tx, ty = min(options)
        return self._wrap(tx), ty

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

    def layer(self, tiles: Iterable[tuple[int, int]], value) -> list[list[float]]:
        out = [(x, y, value(x, y)) for x, y in tiles]
        return [[x, y, round(v, 1)] for x, y, v in out if v >= self.settings.heat_floor]

    def record(
        self, turn: int, cities: Iterable[City], units: Iterable[Unit] = ()
    ) -> dict[str, Any]:
        cities = list(cities)
        units = list(units)
        if not self.allies:
            self.see_own(turn, units, cities)
        s = self.settings
        near_cities = [
            (self._wrap(x), y)
            for c in cities
            for y in range(c.y - s.fog_range, c.y + s.fog_range + 1)
            for x in range(c.x - s.fog_range - 1, c.x + s.fog_range + 2)
            if hex_distance(x, y, c.x, c.y, s.map_width) <= s.fog_range
        ]
        covered = {t for a in self.allies for t in self._cover_field(a)}
        rows = []
        for src in sorted(self.sources, key=lambda s: (s.kind, s.x, s.y)):
            row = asdict(src)
            row["age"] = src.age(turn)
            row["strength"] = self.strength_of(src)
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
            "suspect": None if (sus := self.suspected_camp()) is None else sus.as_row(),
            "heat": self.heat(turn),
            "cover": self.layer(sorted(covered), self.cover_at),
            "fog": self.layer(sorted(set(near_cities)), self.fog_at),
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
        self._heal_read = False

    async def run(self, game: Any) -> None:
        turn = (await game.get_game_overview()).turn
        if not self._heal_read:  # The game's rules do not change in a session: read them once.
            self._heal_read = True
            self.map.heal = await read_combat_heal(game)
            log.info("Threat map: combat heal %s source=%s", self.map.heal, self.map.heal.source)
        cities = await read_cities(game, self.settings.map_width)
        units = await read_units(game, self.settings.map_width)
        self.map.see_threats(turn, await read_threats(game))
        await self._read_suspected(game, turn, units)
        self.map.see_own(turn, units, cities)
        if self.map.due_for_scan(turn):
            self.map.mark_scanned(turn)
            baseline = self.map.take_baseline()
            for city in cities:
                try:
                    radius = (
                        self.settings.baseline_radius if baseline else self.settings.scan_radius
                    )
                    tiles = await read_area(game, city.x, city.y, radius)
                except ConnectionError:
                    raise
                except Exception:
                    log.exception("Threat map: reading the area around a city failed")
                    continue
                self.terrain.learn(tiles)
                self.map.see_tiles(turn, tiles, baseline)
        await self._read_unit_tiles(game, turn)
        await self._read_ground_around_sources(game, turn)
        if self._dump is not None:
            with self._dump.open("a", encoding="utf-8") as out:
                out.write(json.dumps(self.map.record(turn, cities, units)) + "\n")

    async def _read_suspected(self, game: Any, turn: int, units: Iterable[Unit]) -> None:
        """One of ours is beside the suspected camp: read what it sees before the suspicion is
        cleared (see `ThreatMap.see_own`), so a camp beyond the city scan is registered."""
        sus = self.map.suspected_camp()
        s = self.settings
        if sus is None or not any(
            hex_distance(u.x, u.y, sus.x, sus.y, s.map_width) <= s.suspect_look for u in units
        ):
            return
        try:
            tiles = await read_area(game, sus.x, sus.y, s.suspect_look + 1)
        except ConnectionError:
            raise
        except Exception:
            log.exception("Threat map: reading the suspected camp's area failed")
            return
        self.terrain.learn(tiles)
        self.map.see_tiles(turn, tiles)

    async def _read_unit_tiles(self, game: Any, turn: int) -> None:
        """A hostile unit in sight stands on a tile we can see: read it for a camp now, not at
        the next city scan. One tile each, so it is cheap; the map's own tile record skips tiles
        a scan already read this turn."""
        for x, y in self.map.unread_hostile_tiles(turn)[:8]:
            try:
                tiles = await read_area(game, x, y, 0)
            except ConnectionError:
                raise
            except Exception:
                log.exception("Threat map: reading a hostile unit's tile failed")
                continue
            self.terrain.learn(tiles)
            before = len(self.map.camps())
            self.map.see_tiles(turn, tiles)
            if len(self.map.camps()) > before:
                log.info("Threat map: camp found under a hostile unit at %s,%s", x, y)

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
