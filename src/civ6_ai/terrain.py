"""What the ground is like, remembered (issue 238, stage 1b).

Every area read returns terrain, feature, hills, river and coast for each tile the player has
revealed, fogged ones included. Terrain does not change, so it is legal to remember (`state/fog.py`
blanks only the live owner, improvement, district and route of a fogged tile). Policies used to
read an area, use it and drop it. This store keeps it, so anything that needs to know how far a
unit can walk, or whether a ship can sail somewhere, can ask without another read.

A tile never read is *unknown*, not guessed. For a walker an unknown tile is treated as slow, so a
path through the fog does not look free; a ship does not enter one.

The costs follow the game's rules in outline (flat land 1; hills, woods, jungle and marsh 2;
mountains blocked; a river costs one more to enter). They are an approximation, kept in one place
to be corrected from the game's own numbers where they differ. For our own units, the game's
`get_pathing_estimate` is exact and should be preferred.
"""

import heapq
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from civ6_ai.state.models import Tile
from civ6_ai.state.reader import STANDARD_MAP_HEIGHT, STANDARD_MAP_WIDTH, hex_distance

_EVEN_ROW = ((1, 0), (-1, 0), (0, -1), (-1, -1), (0, 1), (-1, 1))
_ODD_ROW = ((1, 0), (-1, 0), (0, -1), (1, -1), (0, 1), (1, 1))  # Same as settling's neighbours.
WATER_TERRAINS = ("TERRAIN_COAST", "TERRAIN_OCEAN", "TERRAIN_LAKE")
SLOW_FEATURES = ("FEATURE_FOREST", "FEATURE_JUNGLE", "FEATURE_MARSH")


def neighbours(x: int, y: int, map_width: int | None = STANDARD_MAP_WIDTH) -> list[tuple[int, int]]:
    offsets = _ODD_ROW if y & 1 else _EVEN_ROW
    found = [(x + dx, y + dy) for dx, dy in offsets]
    return [(nx % map_width if map_width else nx, ny) for nx, ny in found if ny >= 0]


class Ground(NamedTuple):
    terrain: str
    feature: str | None
    hills: bool
    river: bool
    coastal: bool

    @property
    def water(self) -> bool:
        return self.terrain in WATER_TERRAINS

    @property
    def mountain(self) -> bool:
        return self.terrain.endswith("_MOUNTAIN") or self.feature == "FEATURE_ICE"

    @property
    def code(self) -> str:
        """One or two letters for the dump and the picture: w water, m mountain, h hills,
        f forest, j jungle, s marsh, g flat land; a trailing r means a river."""
        if self.water:
            return "w"
        if self.mountain:
            return "m"
        base = "g"
        if self.hills:
            base = "h"
        elif self.feature == "FEATURE_FOREST":
            base = "f"
        elif self.feature == "FEATURE_JUNGLE":
            base = "j"
        elif self.feature == "FEATURE_MARSH":
            base = "s"
        return base + ("r" if self.river else "")


@dataclass
class TerrainStore:
    map_width: int | None = STANDARD_MAP_WIDTH
    map_height: int | None = STANDARD_MAP_HEIGHT  # Rows past it are off the map; None: no bound.
    unknown_land_cost: float = 2.0  # A walker's guess for a tile never read.
    river_extra: float = 1.0  # Added to the cost of entering a river tile.
    _ground: dict[tuple[int, int], Ground] = field(default_factory=dict)
    _tried: dict[tuple[int, int], int] = field(default_factory=dict)
    _fresh: dict[tuple[int, int], str] = field(default_factory=dict)
    revision: int = 0  # Goes up whenever any remembered ground is added or changed.

    # ----- learning -----

    def learn(self, tiles: Iterable[Tile]) -> int:
        """Remember each tile's ground. Returns how many tiles were new."""
        new = 0
        for t in tiles:
            ground = Ground(t.terrain, t.feature, t.hills, t.river, t.coastal)
            if self._ground.get((t.x, t.y)) != ground:
                if (t.x, t.y) not in self._ground:
                    new += 1
                self._ground[t.x, t.y] = ground
                self.revision += 1
                self._fresh[t.x, t.y] = ground.code
        return new

    def take_new(self) -> dict[tuple[int, int], str]:
        """Tiles learned since the last call, as {(x, y): code}, for the dump."""
        fresh, self._fresh = self._fresh, {}
        return fresh

    def get(self, x: int, y: int) -> Ground | None:
        return self._ground.get((x, y))

    def __len__(self) -> int:
        return len(self._ground)

    # ----- reading attempts: don't re-read an area that revealed nothing new -----

    def mark_tried(self, x: int, y: int, turn: int) -> None:
        self._tried[x, y] = turn

    def tried_within(self, x: int, y: int, turn: int, turns: int) -> bool:
        last = self._tried.get((x, y))
        return last is not None and turn - last < turns

    def unknown_near(self, x: int, y: int, radius: int) -> int:
        """Tiles within `radius` of (x, y) that have never been read."""
        count = 0
        for ty in range(y - radius, y + radius + 1):
            for tx in range(x - radius - 1, x + radius + 2):
                if hex_distance(x, y, tx, ty, self.map_width) <= radius and (
                    (tx % self.map_width if self.map_width else tx, ty) not in self._ground
                ):
                    count += 1
        return count

    # ----- movement -----

    def step_cost(self, x: int, y: int, naval: bool) -> float | None:
        """Cost to enter a tile, or None if the unit cannot."""
        ground = self._ground.get((x, y))
        if naval:
            return 1.0 if ground is not None and ground.water else None
        if ground is None:
            return self.unknown_land_cost
        if ground.water or ground.mountain:
            return None
        cost = 2.0 if ground.hills or ground.feature in SLOW_FEATURES else 1.0
        return cost + (self.river_extra if ground.river else 0.0)

    def path_to(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        avoid: Iterable[tuple[int, int]] = (),
        max_cost: float = 80.0,
    ) -> list[tuple[int, int]] | None:
        """Cheapest walking path from `start` to `goal`, as the tiles after `start` up to and
        including `goal`. Water, mountains and `avoid` tiles are never entered. None if there is
        no path within `max_cost`."""
        if start == goal:
            return []
        blocked = set(avoid)
        best: dict[tuple[int, int], float] = {start: 0.0}
        came: dict[tuple[int, int], tuple[int, int]] = {}
        queue: list[tuple[float, int, int]] = [(0.0, start[0], start[1])]
        while queue:
            cost, cx, cy = heapq.heappop(queue)
            if cost > best.get((cx, cy), float("inf")):
                continue
            if (cx, cy) == goal:
                path = [goal]
                while path[-1] in came and came[path[-1]] != start:
                    path.append(came[path[-1]])
                return path[::-1]
            for nxt in neighbours(cx, cy, self.map_width):
                step = self.step_cost(nxt[0], nxt[1], naval=False)
                if (
                    step is None
                    or nxt in blocked
                    or (self.map_height is not None and nxt[1] >= self.map_height)
                ):
                    continue
                total = cost + step
                if total <= max_cost and total < best.get(nxt, float("inf")):
                    best[nxt] = total
                    came[nxt] = (cx, cy)
                    heapq.heappush(queue, (total, nxt[0], nxt[1]))
        return None

    def step_toward(
        self,
        start: tuple[int, int],
        goal: tuple[int, int],
        moves: float,
        avoid: Iterable[tuple[int, int]] = (),
        stop_short: bool = False,
        occupied: Iterable[tuple[int, int]] = (),
    ) -> tuple[int, int] | None:
        """The farthest tile along the cheapest path that `moves` movement points reach (at least
        one step, never more steps than whole moves). With `stop_short` the goal itself is left
        out, so the unit ends beside it. Tiles in `occupied` can be crossed but not ended on; the
        next-farthest free tile is used, None when every reachable tile is taken. None also if no
        path is known; callers fall back to a straight line."""
        occupied = set(occupied)
        avoid = set(avoid)
        path = self.path_to(start, goal, avoid)
        if stop_short and path:
            path = path[:-1]
        if not path:
            return None
        spent = 0.0
        reached = None
        for i, tile in enumerate(path[: max(1, int(moves))]):
            spent += self.step_cost(tile[0], tile[1], naval=False) or 0.0
            if i and spent > moves:
                break
            if tile not in occupied:
                reached = tile
        if reached is not None or not occupied:
            return reached
        return self._free_step(start, goal, moves, avoid, occupied, stop_short)

    def _free_step(self, start, goal, moves, avoid, occupied, stop_short):
        """Issue 377: the farthest path tile is taken, so pick among every tile reachable this turn
        (not water, a mountain or refused) the one with the least terrain cost left to the goal,
        then the nearer by hex distance, then the lowest coordinates. A tile that is no nearer
        than where the unit stands is skipped. None when nothing qualifies."""
        limit = max(1, int(moves))
        best: dict[tuple[int, int], float] = {start: 0.0}
        steps = {start: 0}
        queue: list[tuple[float, int, int]] = [(0.0, start[0], start[1])]
        while queue:
            cost, cx, cy = heapq.heappop(queue)
            if cost > best.get((cx, cy), float("inf")) or steps[cx, cy] >= limit:
                continue
            for nxt in neighbours(cx, cy, self.map_width):
                step = self.step_cost(nxt[0], nxt[1], naval=False)
                if (
                    step is None
                    or nxt in avoid
                    or (self.map_height is not None and nxt[1] >= self.map_height)
                ):
                    continue
                total = cost + step
                if (steps[cx, cy] and total > moves) or total >= best.get(nxt, float("inf")):
                    continue
                best[nxt] = total
                steps[nxt] = steps[cx, cy] + 1
                heapq.heappush(queue, (total, nxt[0], nxt[1]))
        here = self._cost_left(start, goal, avoid)
        ranked = []
        for tile in best:
            if tile == start or tile in occupied or (stop_short and tile == goal):
                continue
            left = self._cost_left(tile, goal, avoid)
            if left is None or (here is not None and left >= here and tile != goal):
                continue
            ranked.append(
                (left, hex_distance(tile[0], tile[1], *goal, self.map_width), tile[0], tile[1])
            )
        return min(ranked)[2:] if ranked else None

    def _cost_left(self, tile, goal, avoid) -> float | None:
        path = self.path_to(tile, goal, avoid)
        if path is None:
            return None
        return sum(self.step_cost(x, y, naval=False) or 0.0 for x, y in path)

    def reach(
        self,
        x: int,
        y: int,
        moves: float,
        naval: bool,
        horizon_turns: float,
        reach_range: int = 0,
    ) -> dict[tuple[int, int], float]:
        """Turns for a unit with `moves` per turn to reach each tile within `horizon_turns`.

        A ship reaches water only; the land beside water it can reach counts one more step (a
        landing or a shot), and nothing further inland. `reach_range` adds the tiles a ranged
        unit can shoot at from where it can stand, at the same turns.
        """
        limit = moves * horizon_turns
        best: dict[tuple[int, int], float] = {(x, y): 0.0}
        queue: list[tuple[float, int, int]] = [(0.0, x, y)]
        while queue:
            cost, cx, cy = heapq.heappop(queue)
            if cost > best.get((cx, cy), float("inf")):
                continue
            for nx, ny in neighbours(cx, cy, self.map_width):
                step = self.step_cost(nx, ny, naval)
                if step is None or cost + step > limit:
                    continue
                if cost + step < best.get((nx, ny), float("inf")):
                    best[nx, ny] = cost + step
                    heapq.heappush(queue, (cost + step, nx, ny))
        turns = {p: c / moves for p, c in best.items()}
        if naval:
            for (wx, wy), t in list(turns.items()):
                for nx, ny in neighbours(wx, wy, self.map_width):
                    ground = self._ground.get((nx, ny))
                    if ground is not None and not ground.water:
                        turns[nx, ny] = min(turns.get((nx, ny), float("inf")), t + 1 / moves)
        for _ in range(reach_range):
            for (px, py), t in list(turns.items()):
                for nx, ny in neighbours(px, py, self.map_width):
                    if t < turns.get((nx, ny), float("inf")):
                        turns[nx, ny] = t
        return turns

    def walk_field(
        self, goal: tuple[int, int], max_cost: float = 40.0
    ) -> dict[tuple[int, int], float]:
        """Walking cost from each tile to `goal` (issue 392): what a land unit standing there
        spends entering the tiles on its cheapest way, `goal` included. One search from the goal
        answers it for every tile, so a force's walk to a camp is read off, not guessed from hex
        distance. Water and mountains are never entered, so a tile across a bay from the goal
        costs the way round. Tiles past `max_cost` are left out; the goal costs 0."""
        best: dict[tuple[int, int], float] = {goal: 0.0}
        queue: list[tuple[float, int, int]] = [(0.0, goal[0], goal[1])]
        while queue:
            cost, cx, cy = heapq.heappop(queue)
            if cost > best.get((cx, cy), float("inf")):
                continue
            enter = self.step_cost(cx, cy, naval=False)
            if enter is None:
                continue  # Nobody walks through it; only the goal itself may sit on such a tile.
            for nxt in neighbours(cx, cy, self.map_width):
                if self.map_height is not None and nxt[1] >= self.map_height:
                    continue
                if self.step_cost(nxt[0], nxt[1], naval=False) is None:
                    continue  # A walker never stands on water or a mountain.
                total = cost + enter
                if total <= max_cost and total < best.get(nxt, float("inf")):
                    best[nxt] = total
                    heapq.heappush(queue, (total, nxt[0], nxt[1]))
        return best
