"""One movement-and-reach service for every policy (epic #399, slice 1).

The same bug kept coming back in one policy after another: hex distance or a straight line where
the real walk, or the reach of a threat, decides. This module answers four questions from the
terrain store (`terrain.py`) so no policy keeps its own version:

* walking turns from A to B (`Movement.walk_turns`, one field per start, reused);
* the cost to a goal from every tile (`Movement.cost_field`, one search from the goal, reused);
* the next step this turn toward a goal, around tiles we hold and tiles the game refused
  (`Movement.next_step`, with the refusals kept per trip by `TripLog`);
* the danger along the WALKING route, read from the threat map (`Movement.route_danger`), and the
  tiles a hostile can attack next turn (`Movement.threat_reach`).

`TripLog` records each move-to-goal trip (unit, kind, start, goal, start turn, estimated turns) and
logs one `movement_trip` line when the unit gets there or the trip is abandoned, so the estimate
can be judged against what happened (`scripts/trip_report.py`).

The turn rule is the game's: a unit may enter a tile while it has any movement left, however much
the tile costs. A builder (2 moves) crosses two flat tiles or one hill a turn, not "total cost
over moves, rounded up". A tile never read keeps `TerrainStore`'s convention (a walker's guess).
"""

import heapq
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance
from civ6_ai.terrain import TerrainStore, neighbours

log = logging.getLogger(__name__)
Tile = tuple[int, int]
_FAR = 1 << 30


@dataclass(frozen=True)
class MovementSettings:
    """Every number the service uses, in one place (#399)."""

    horizon_turns: float = 20.0  # Walking fields stop this many turns out (a tile past is absent).
    route_max_cost: float = 80.0  # Terrain cost a walking route may have before it is not one.
    cost_field_max: float = 40.0  # Terrain cost a goal's cost field reaches.
    route_mode: str = "max"  # Route danger: "max" of the tiles, or "sum" of them.
    melee_attack_range: int = 1  # A melee hostile attacks a tile beside where it can stand.
    # Refusal reasons that hold for one turn only (a tile held by another of our units moves on).
    # Any other reason (territory, the game's own "no") is kept for the whole trip.
    transient_reasons: tuple[str, ...] = ("stacking",)
    trader_moves: float = 2.0  # A Trader's moves per turn; the trade-route read does not give it.
    trip_timeout_turns: int = 60  # A trip open this long is closed as abandoned.
    map_width: int | None = STANDARD_MAP_WIDTH  # Used only with no terrain store.


# ----- pure functions -----


def straight_line(x1: int, y1: int, x2: int, y2: int, map_width: int | None) -> list[Tile]:
    """Tiles on the straight hex line from one tile to another, both ends included.

    The fallback when no walking route is known. Odd rows sit half a tile east; x wraps, so the
    line takes the shorter way round the seam.
    """
    east = x2
    if map_width is not None:
        east = min(
            (x2 + k * map_width for k in (-1, 0, 1)),
            key=lambda e: hex_distance(x1, y1, e, y2, None),
        )

    def cube(x: int, y: int) -> tuple[float, float, float]:
        q = x - (y - (y & 1)) // 2
        return float(q), float(-q - y), float(y)

    a, b = cube(x1, y1), cube(east, y2)
    steps = hex_distance(x1, y1, east, y2, None)
    tiles: list[Tile] = []
    for i in range(steps + 1):
        t = i / steps if steps else 0.0
        cx, cy, cz = (a[k] + (b[k] - a[k]) * t for k in range(3))
        rx, ry, rz = round(cx), round(cy), round(cz)
        dx, dy, dz = abs(rx - cx), abs(ry - cy), abs(rz - cz)
        if dx > dy and dx > dz:
            rx = -ry - rz
        elif dy > dz:
            ry = -rx - rz
        else:
            rz = -rx - ry
        col = rx + (rz - (rz & 1)) // 2
        tile = (col % map_width if map_width else col, rz)
        if not tiles or tiles[-1] != tile:
            tiles.append(tile)
    return tiles


def turn_field(
    terrain: TerrainStore,
    start: Tile,
    moves: float,
    avoid: Iterable[Tile] = (),
    horizon_turns: float = 20.0,
    naval: bool = False,
    spent: float = 0.0,
) -> dict[Tile, int]:
    """The turn in which a unit with `moves` per turn first stands on each tile: 1 for a tile it
    reaches this turn, 0 for `start`. `spent` is movement already used this turn.

    The game's rule: a unit may enter a tile while it has any movement left. Water and mountains
    are never entered by a walker (a ship enters water only), nor are `avoid` tiles, nor rows past
    the map's height. A tile with no route within `horizon_turns` is absent.
    """
    blocked = set(avoid)
    per_turn = max(1.0, moves)
    best: dict[Tile, tuple[int, float]] = {start: (1, spent)}
    queue: list[tuple[int, float, int, int]] = [(1, spent, start[0], start[1])]
    while queue:
        turn, used, cx, cy = heapq.heappop(queue)
        if (turn, used) > best.get((cx, cy), (_FAR, 0.0)):
            continue
        for nxt in neighbours(cx, cy, terrain.map_width):
            step = terrain.step_cost(nxt[0], nxt[1], naval)
            if (
                step is None
                or nxt in blocked
                or (terrain.map_height is not None and nxt[1] >= terrain.map_height)
            ):
                continue
            state = (turn + 1, step) if used >= per_turn else (turn, used + step)
            if state[0] <= horizon_turns and state < best.get(nxt, (_FAR, 0.0)):
                best[nxt] = state
                heapq.heappush(queue, (state[0], state[1], nxt[0], nxt[1]))
    return {tile: (0 if tile == start else state[0]) for tile, state in best.items()}


def cost_to_goal(
    terrain: TerrainStore, goal: Tile, max_cost: float = 40.0, avoid: Iterable[Tile] = ()
) -> dict[Tile, float]:
    """Terrain cost from each tile to `goal`: what a walker standing there spends entering the
    tiles on its cheapest way, `goal` included. One search from the goal answers it for every tile,
    so a tile across a bay from the goal costs the way round. The goal costs 0; tiles past
    `max_cost` are left out. `avoid` tiles are never entered, so no way through them counts."""
    blocked = set(avoid)
    best: dict[Tile, float] = {goal: 0.0}
    queue: list[tuple[float, int, int]] = [(0.0, goal[0], goal[1])]
    while queue:
        cost, cx, cy = heapq.heappop(queue)
        if cost > best.get((cx, cy), float("inf")):
            continue
        enter = terrain.step_cost(cx, cy, False)
        if enter is None:
            continue  # Nobody walks through it; only the goal itself may sit on such a tile.
        for nxt in neighbours(cx, cy, terrain.map_width):
            if terrain.map_height is not None and nxt[1] >= terrain.map_height:
                continue
            if nxt in blocked or terrain.step_cost(nxt[0], nxt[1], False) is None:
                continue  # A walker never stands on water or a mountain.
            total = cost + enter
            if total <= max_cost and total < best.get(nxt, float("inf")):
                best[nxt] = total
                heapq.heappush(queue, (total, nxt[0], nxt[1]))
    return best


def attack_tiles(
    terrain: TerrainStore | None,
    start: Tile,
    moves: float,
    attack_range: int,
    naval: bool = False,
    map_width: int | None = STANDARD_MAP_WIDTH,
) -> set[Tile]:
    """Tiles a hostile at `start` can attack next turn: every tile within `attack_range` of a tile
    it can stand on after one turn of walking (a melee unit: moves plus 1; a ranged one: moves plus
    its range). With no terrain store, by hex distance."""
    if terrain is None:
        reach = int(moves)
        standable = {
            (x % map_width if map_width else x, y)
            for y in range(max(0, start[1] - reach), start[1] + reach + 1)
            for x in range(start[0] - reach - 1, start[0] + reach + 2)
            if hex_distance(start[0], start[1], x, y, map_width) <= reach
        }
        width, height = map_width, None
    else:
        field_ = turn_field(terrain, start, moves, (), 1.0, naval)
        standable = {tile for tile, t in field_.items() if t <= 1}
        width, height = terrain.map_width, terrain.map_height
    found = set(standable)
    frontier = set(found)
    for _ in range(attack_range):
        grown: set[Tile] = set()
        for x, y in frontier:
            for nxt in neighbours(x, y, width):
                if (height is None or nxt[1] < height) and nxt not in found:
                    grown.add(nxt)
        found |= grown
        frontier = grown
    return found


# ----- trips -----


@dataclass
class Trip:
    """One move-to-goal trip of one unit."""

    unit_id: int
    kind: str  # "builder", "trader", ...
    start: Tile
    goal: Tile
    start_turn: int
    estimated: int | None  # Turns the walk is expected to take; None when nothing could say.
    basis: str = "walk"  # "walk" (terrain) or "hex" (distance only).
    refused: dict[Tile, tuple[str, int]] = field(default_factory=dict)  # tile -> (reason, turn)
    waits: int = 0  # Turns spent waiting (hold, flee, gathering an escort), not walking.
    last_wait: int = -1

    def blocked(self, turn: int, transient: Iterable[str] = ("stacking",)) -> set[Tile]:
        """Tiles the game refused that still hold on `turn`: a transient reason (stacking) holds
        only for the turn it was given, any other for the whole trip."""
        short = set(transient)
        return {
            tile
            for tile, (reason, when) in self.refused.items()
            if reason not in short or when == turn
        }


@dataclass
class TripLog:
    """Opens a trip when a unit is sent, closes it when the unit arrives or the trip is dropped,
    and logs one `movement_trip` line for each. Policies call `start`, `refuse`, `observe`,
    `arrived`, `abandon` and `sweep`; nothing here moves a unit."""

    settings: MovementSettings = field(default_factory=MovementSettings)
    trips: dict[int, Trip] = field(default_factory=dict)
    closed: list[dict] = field(default_factory=list)

    def start(
        self,
        unit_id: int,
        kind: str,
        start: Tile,
        goal: Tile,
        turn: int,
        estimated: int | None,
        basis: str = "walk",
    ) -> Trip:
        """Open a trip. One already open to the same goal carries on (no new estimate); one to
        another goal is closed as retargeted."""
        open_trip = self.trips.get(unit_id)
        if open_trip is not None:
            if open_trip.goal == goal and open_trip.kind == kind:
                return open_trip
            self.abandon(unit_id, turn, "retargeted")
        trip = Trip(unit_id, kind, start, goal, turn, estimated, basis)
        self.trips[unit_id] = trip
        return trip

    def trip(self, unit_id: int) -> Trip | None:
        return self.trips.get(unit_id)

    def refuse(self, unit_id: int, tile: Tile, reason: str, turn: int) -> None:
        """Remember a tile the game would not let this unit enter, and why."""
        trip = self.trips.get(unit_id)
        if trip is not None:
            trip.refused[tile] = (reason, turn)

    def wait(self, unit_id: int, turn: int) -> None:
        """The unit spent `turn` waiting rather than walking (counted once per turn), so the
        trip can be judged on its moving turns."""
        trip = self.trips.get(unit_id)
        if trip is not None and trip.last_wait != turn:
            trip.last_wait = turn
            trip.waits += 1

    def observe(self, unit_id: int, at: Tile, turn: int) -> None:
        """The unit stands at `at` on `turn`: close its trip if that is the goal, or if it has
        been open longer than `trip_timeout_turns`."""
        trip = self.trips.get(unit_id)
        if trip is None:
            return
        if at == trip.goal:
            self._close(trip, turn, "arrived")
        elif turn - trip.start_turn >= self.settings.trip_timeout_turns:
            self._close(trip, turn, "timeout")

    def arrived(self, unit_id: int, turn: int) -> None:
        """The unit reached its goal during `turn` (the move said so)."""
        trip = self.trips.get(unit_id)
        if trip is not None:
            self._close(trip, turn, "arrived")

    def abandon(self, unit_id: int, turn: int, reason: str) -> None:
        trip = self.trips.get(unit_id)
        if trip is not None:
            self._close(trip, turn, reason)

    def sweep(self, alive: Iterable[int], turn: int, kind: str | None = None) -> None:
        """Close the trips of units that are gone (lost or used up)."""
        keep = set(alive)
        for uid in [
            u for u, t in self.trips.items() if u not in keep and (kind is None or t.kind == kind)
        ]:
            self._close(self.trips[uid], turn, "gone")

    def _close(self, trip: Trip, turn: int, outcome: str) -> None:
        del self.trips[trip.unit_id]
        # Turn counting: the turn the order was given is turn 1, so a unit that gets there on the
        # same turn took 1, which is what `turn_field` calls 1.
        row = {
            "unit_id": trip.unit_id,
            "kind": trip.kind,
            "start": list(trip.start),
            "goal": list(trip.goal),
            "start_turn": trip.start_turn,
            "end_turn": turn,
            "estimated": trip.estimated,
            "actual": turn - trip.start_turn + 1,
            "outcome": outcome,
            "basis": trip.basis,
            "waits": trip.waits,
            "refused": sorted([x, y, reason] for (x, y), (reason, _) in trip.refused.items()),
        }
        self.closed.append(row)
        log.info("movement_trip %s", json.dumps(row, sort_keys=True))


# ----- the service -----


@dataclass
class Movement:
    """Walking turns, next steps, route danger and threat reach over a `TerrainStore`, with the
    threat map for danger. With no terrain store every answer falls back to hex distance or a
    straight line, as policies did before (#399)."""

    terrain: TerrainStore | None = None
    threat_map: Any = field(default=None, repr=False)
    settings: MovementSettings = field(default_factory=MovementSettings)
    trips: TripLog = field(init=False)
    _cache: dict = field(default_factory=dict, init=False, repr=False)
    _cache_revision: int = field(default=-1, init=False, repr=False)

    def __post_init__(self) -> None:
        self.trips = TripLog(self.settings)

    @property
    def map_width(self) -> int | None:
        return self.terrain.map_width if self.terrain is not None else self.settings.map_width

    def _cached(self, key: tuple, make: Callable[[], Any]) -> Any:
        """Fields are reused until the terrain learns something new."""
        revision = self.terrain.revision if self.terrain is not None else 0
        if revision != self._cache_revision or len(self._cache) > 256:
            self._cache, self._cache_revision = {}, revision
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    # ----- walking turns -----

    def turn_field(
        self,
        start: Tile,
        moves: float,
        avoid: Iterable[Tile] = (),
        naval: bool = False,
        spent: float = 0.0,
    ) -> dict[Tile, int] | None:
        """Turns to every tile from `start` (see `turn_field`); None with no terrain store."""
        terrain = self.terrain
        if terrain is None:
            return None
        blocked = frozenset(avoid)
        return self._cached(
            ("turns", start, moves, blocked, naval, spent),
            lambda: turn_field(
                terrain, start, moves, blocked, self.settings.horizon_turns, naval, spent
            ),
        )

    def walk_turns(
        self, start: Tile, goal: Tile, moves: float, avoid: Iterable[Tile] = ()
    ) -> int | None:
        """Turns to walk from `start` to `goal`: 0 if already there, 1 if reachable this turn.
        None when there is no route within the horizon or no terrain store (use `hex_turns`)."""
        if start == goal:
            return 0
        field_ = self.turn_field(start, moves, avoid)
        return None if field_ is None else field_.get(goal)

    def hex_turns(self, start: Tile, goal: Tile, moves: float) -> int:
        """The estimate with no terrain: hex distance over moves, rounded up (0 if there)."""
        distance = hex_distance(start[0], start[1], goal[0], goal[1], self.map_width)
        return -(-distance // max(1, int(moves))) if distance else 0

    def estimate(self, start: Tile, goal: Tile, moves: float) -> tuple[int, str]:
        """(turns, basis) for a trip: the walk when known, else the hex estimate."""
        walked = self.walk_turns(start, goal, moves)
        if walked is not None:
            return walked, "walk"
        return self.hex_turns(start, goal, moves), "hex"

    def cost_field(self, goal: Tile) -> dict[Tile, float] | None:
        """Terrain cost to `goal` from every tile (one search, reused); None with no store."""
        terrain = self.terrain
        if terrain is None:
            return None
        return self._cached(
            ("cost", goal), lambda: cost_to_goal(terrain, goal, self.settings.cost_field_max)
        )

    # ----- the next step -----

    def next_step(
        self,
        start: Tile,
        goal: Tile,
        moves: float,
        avoid: Iterable[Tile] = (),
        trip: Trip | None = None,
        turn: int = 0,
        occupied: Iterable[Tile] = (),
        stop_short: bool = False,
    ) -> Tile | None:
        """The farthest tile along the cheapest walk that this turn's moves reach. `avoid` are
        tiles we must not enter; `trip`'s refused tiles join them (a stacking refusal only on the
        turn it was given); `occupied` tiles can be crossed but not ended on. None when there is
        no walk (use a straight step) or no terrain store."""
        terrain = self.terrain
        if terrain is None:
            return None
        blocked = set(avoid)
        if trip is not None:
            blocked |= trip.blocked(turn, self.settings.transient_reasons)
        taken = set(occupied)
        path = terrain.path_to(start, goal, blocked, self.settings.route_max_cost)
        if stop_short and path:
            path = path[:-1]
        if not path:
            return None
        # The same rule as `turn_field`: a tile is entered while any movement is left.
        spent, reached = 0.0, None
        for tile in path:
            if spent >= max(1.0, moves):
                break
            spent += terrain.step_cost(tile[0], tile[1], False) or 0.0
            if tile not in taken:
                reached = tile
        if reached is not None or not taken:
            return reached
        return self._free_step(start, goal, moves, blocked, taken, stop_short)

    def _free_step(
        self, start: Tile, goal: Tile, moves: float, blocked: set[Tile], taken: set[Tile], short
    ) -> Tile | None:
        """The path's reachable tiles are all taken: of every tile reachable this turn, the free
        one with the least terrain cost left to the goal, then the nearer by hex distance, then
        the lowest coordinates. A tile no nearer than where the unit stands is skipped."""
        field_ = self.turn_field(start, moves, blocked) or {}
        if blocked and self.terrain is not None:  # Costs must not run through refused tiles.
            left = cost_to_goal(self.terrain, goal, self.settings.cost_field_max, blocked)
        else:
            left = self.cost_field(goal) or {}
        here = left.get(start)
        ranked = []
        for tile, t in field_.items():
            if t != 1 or tile in taken or (short and tile == goal) or tile not in left:
                continue
            if here is not None and left[tile] >= here and tile != goal:
                continue
            ranked.append(
                (left[tile], hex_distance(tile[0], tile[1], *goal, self.map_width), *tile)
            )
        return min(ranked)[2:] if ranked else None  # type: ignore[return-value]

    # ----- danger -----

    def route_tiles(self, start: Tile, goal: Tile) -> list[Tile]:
        """The walking route from `start` to `goal`, both ends included. Where the walk is not
        known (no store, water between, no path in range) the straight line stands in."""
        terrain = self.terrain
        if terrain is not None:
            path = self._cached(
                ("route", start, goal),
                lambda: terrain.path_to(start, goal, (), self.settings.route_max_cost),
            )
            if path is not None:
                return [start, *path]
        return straight_line(start[0], start[1], goal[0], goal[1], self.map_width)

    def route_danger(
        self,
        start: Tile,
        goal: Tile,
        turn: int,
        danger_at: Callable[[int, int, int], float] | None = None,
        tiles: Iterable[Tile] | None = None,
    ) -> float:
        """The danger along the walking route: the worst tile, or the sum
        (`settings.route_mode`). `danger_at(x, y, turn)` defaults to the threat map's raw danger;
        0.0 with neither."""
        if danger_at is None:
            if self.threat_map is None:
                return 0.0
            danger_at = self.threat_map.raw_danger_at
        route = self.route_tiles(start, goal) if tiles is None else tiles
        values = [danger_at(x, y, turn) for x, y in route]
        if self.settings.route_mode == "sum":
            return sum(values)
        return max(values, default=0.0)

    def threat_reach(
        self, start: Tile, moves: float, attack_range: int | None = None, naval: bool = False
    ) -> set[Tile]:
        """Tiles a hostile at `start` can attack next turn. `attack_range` is 1 for a melee unit
        (the default, `settings.melee_attack_range`) and the shooting range for a ranged one."""
        rng = self.settings.melee_attack_range if attack_range is None else attack_range
        return attack_tiles(self.terrain, start, moves, rng, naval, self.map_width)
