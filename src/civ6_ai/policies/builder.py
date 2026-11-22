"""Builder automation: improve tiles and repair pillaged ones, with no model calls.

`BuilderPolicy` is a turn handler (see `orchestrator.Handler`). Each turn it reads the upstream
builder advice (`read_builder_jobs`), scores every job for every builder that can move, and
assigns them greedily, best pair first. A job goes to one builder only, and a builder keeps its
job from turn to turn until it is done or set aside. The same state and weights always give the
same assignment.

The advice is only a heuristic: it names the improvement by resource or by terrain, and cannot
see whether the builder can really build it. So every refusal is counted, and a job that keeps
failing, or keeps a builder walking or working for too long, is set aside for a while. Nothing
retries without limit and no builder can be stuck on a job for good.

`BuilderWeights` is a plain input the Phase 2 strategist can change. The defaults put strategic
resources and repairs first, then luxuries, then bonus resources, then everything else.
"""

import json
import logging
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from civ6_ai.blockers import action_succeeded, move_succeeded
from civ6_ai.movement import Movement, MovementSettings
from civ6_ai.policies.game_data import REPLACES
from civ6_ai.state import BuilderJob, GameReads, Threat, Unit, read_builder_jobs, read_threats
from civ6_ai.state import read_units as _read_units
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance
from civ6_ai.strategy.boost_inputs import job_key, job_matches
from civ6_ai.strategy.boost_judgement import Nudge

log = logging.getLogger(__name__)

BUILDER = "UNIT_BUILDER"
# Builders can't reach these, and the advice names "UNKNOWN" for a resource it has no rule for.
_UNBUILDABLE = frozenset({"IMPROVEMENT_FISHING_BOATS"})
_NOW_AT = re.compile(r"now_at:(-?\d+),(-?\d+)")


@dataclass
class BuilderDemand:
    """What the builders left undone last turn, which production reads to size the builder corps.

    Run 8 had builders idle on 74% of builder-turns. `idle` counts builders that found no job on
    offer, and `spare_jobs` counts jobs no builder took. `spare_jobs` is None when no builder was
    free to ask, so the jobs weren't read and nothing is known.
    """

    idle: int = 0
    spare_jobs: int | None = None


def is_builder(unit_type: str) -> bool:
    """A builder, or a unique unit that replaces one."""
    return REPLACES.get(unit_type, unit_type) == BUILDER


@dataclass(frozen=True)
class BuilderWeights:
    """How much each kind of job is worth. Higher scores go first.

    A job scores its upstream priority, plus its class bonus, minus a cost for each turn the
    builder needs to reach it. The defaults give strategic resource > repair > luxury > bonus
    resource > plain tile improvement for jobs at the same distance.
    """

    urgent: float = 100.0  # Upstream gives this to strategic resources and repairs.
    high: float = 60.0  # Luxury and bonus resources.
    normal: float = 20.0  # Plain tile improvements.
    strategic: float = 40.0
    repair: float = 35.0
    luxury: float = 30.0
    bonus: float = 10.0
    turn_cost: float = 4.0  # Score given up for each turn spent walking to the job.
    sticky: float = 15.0  # A builder already on a job is kept there unless something beats this.
    # Added to a job a boost nudge asks for (#344). Flat, never summed over several nudges, and
    # sized so a plain job (20) rises to 60, level with a bonus-resource job (60) but under any
    # luxury (90), strategic resource (140) or repair (135) job: a boost can't starve the economy.
    nudge_bonus: float = 40.0
    # Score given up per unit of raw danger (#389: a builder was captured on a walk the score
    # called safe and short). The danger is the worst on the job tile and the straight line to
    # it, our cover ignored, as for a Trader route (#287). Weighted, never a ban: one unit of
    # danger costs one and a half turns of walking, so a rich job still beats a poor safe one.
    danger_cost: float = 6.0


@dataclass(frozen=True)
class BuilderSettings:
    """Bounds on safety, retrying and waiting, so nothing stalls for good."""

    weights: BuilderWeights = field(default_factory=BuilderWeights)
    threat_radius: int = 2  # A job this close to a hostile unit is skipped for the turn.
    attempts_per_builder: int = (
        3  # Jobs tried for one builder in one turn, as each refusal costs a call.
    )
    max_failures: int = 2  # Refusals of one job before it is set aside.
    retry_turns: int = 10  # How long a job that failed is set aside.
    max_travel_turns: int = 20  # Turns walking to one job before it is given up.
    max_work_turns: int = 15  # Turns spent on one job before it is given up.
    max_hold_turns: int = 5  # Turns an idle builder keeps its claim while the job is unsafe.
    map_width: int | None = STANDARD_MAP_WIDTH
    # A builder that vanishes away from its job is taken as lost (#389). The area around its last
    # tile is given this much danger for `lost_turns` turns, so the next builder is not sent
    # straight back. The unit is the threat map's: a Trader route is refused above 5.
    lost_danger: float = 5.0
    lost_radius: int = 3
    lost_turns: int = 15
    danger_cap: float = 10.0  # Danger counted per job at most, so one camp can't hide every job.
    # Take up the research policy's boost nudges (#344). Off, so nothing changes until a run
    # shows it pays. Needs `ResearchWeights.boost_judgement` on, which is what makes nudges.
    use_boost_nudges: bool = False


def score_job(
    job: BuilderJob,
    distance: int,
    max_moves: float,
    weights: BuilderWeights,
    danger: float = 0.0,
    walk_turns: float | None = None,
) -> float:
    """Value a job for a builder `distance` tiles away. Only visible fields are read.

    `walk_turns` is the real walk from the terrain, used over the hex distance when known, and
    `danger` is the worst raw danger on the job and the way to it (#389).
    """
    score = {"urgent": weights.urgent, "high": weights.high}.get(job.priority, weights.normal)
    if job.is_repair:
        score += weights.repair
    elif job.resource_class == "strategic":
        score += weights.strategic
    elif job.resource_class == "luxury":
        score += weights.luxury
    elif job.resource_class == "bonus":
        score += weights.bonus
    if walk_turns is None:
        walk_turns = math.ceil(distance / max(1.0, max_moves))
    return score - weights.turn_cost * walk_turns - weights.danger_cost * max(0.0, danger)


@dataclass(frozen=True)
class BuilderDecision:
    """One builder's turn, with the evidence for it."""

    turn: int
    unit_id: int
    x: int
    y: int
    action: str  # move, improve, repair, remove_feature, failed, idle
    reason: str
    target: tuple[int, int] | None = None
    improvement: str | None = None
    score: float | None = None
    result: str = ""


class BuilderGame(GameReads, Protocol):
    """The reads plus the four actions this policy takes."""

    async def move_unit(self, unit_index: int, target_x: int, target_y: int) -> str: ...

    async def improve_tile(self, unit_index: int, improvement_name: str) -> str: ...

    async def repair_improvement(self, unit_index: int) -> str: ...

    async def remove_feature(self, unit_index: int) -> str: ...


# A job is named by its tile and what is built there.
_Key = tuple[int, int, str]


def _key(job: BuilderJob) -> _Key:
    return job.x, job.y, job.improvement


@dataclass
class BuilderPolicy:
    """Moves builders to jobs and has them work. Run it before the blocker resolver."""

    settings: BuilderSettings = field(default_factory=BuilderSettings)
    name: str = "builder"
    demand: BuilderDemand = field(default_factory=BuilderDemand)
    decisions: list[BuilderDecision] = field(default_factory=list)
    # Where the boost nudges come from (a `NudgeBoard.current`-style callable taking the turn).
    # With none, or `use_boost_nudges` off, no job gets the bonus.
    nudges: Callable[[int], Sequence[Nudge]] | None = None
    # For the boost judgement (#344): improvements this policy has set a builder to, by type
    # without the prefix, and those on a resource tile. Counted once per tile and improvement.
    built: dict[str, int] = field(default_factory=dict)
    built_on_resource: dict[str, int] = field(default_factory=dict)
    # The buildable jobs read on `jobs_turn` and the keys builders held at the end of that turn.
    open_jobs: list[BuilderJob] = field(default_factory=list)
    claimed: frozenset = field(default_factory=frozenset)
    jobs_turn: int | None = None
    # The threat map and terrain store, set by the bot (#389). With none, a job is scored by hex
    # distance and no danger, as before.
    threat_map: Any = field(default=None, repr=False)
    terrain: Any = field(default=None, repr=False)
    # The shared movement service (#399): walking turns, route danger and trip logging. With none,
    # one is made over `terrain` and `threat_map` on first use.
    movement: Movement | None = field(default=None, repr=False)
    # Where builders were lost: (x, y, turn).
    lost: list[tuple[int, int, int]] = field(default_factory=list)
    _seen: dict[int, tuple[int, int, tuple[int, int] | None]] = field(
        default_factory=dict, init=False, repr=False
    )
    _last_turn: int = field(default=0, init=False, repr=False)
    # Where a builder stood after this turn's move, as the move reported it (#389).
    _moved: dict[int, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _built_keys: set = field(default_factory=set, init=False, repr=False)
    _nudged: set = field(default_factory=set, init=False, repr=False)
    _claims: dict[int, _Key] = field(default_factory=dict, init=False, repr=False)
    _failures: dict[_Key, int] = field(default_factory=dict, init=False, repr=False)
    _set_aside: dict[_Key, int] = field(default_factory=dict, init=False, repr=False)
    # Per builder: the job, how many turns spent on it, and the last turn counted.
    _travel: dict[int, tuple[_Key, int, int]] = field(default_factory=dict, init=False, repr=False)
    _work: dict[int, tuple[_Key, int, int]] = field(default_factory=dict, init=False, repr=False)
    _holds: dict[int, int] = field(default_factory=dict, init=False, repr=False)

    async def run(self, game: BuilderGame) -> None:
        units = await _read_units(game, self.settings.map_width)
        builders = [u for u in units if is_builder(u.unit_type)]
        self._forget_gone_units(builders)
        free = sorted((u for u in builders if u.can_move), key=lambda u: u.unit_id)
        if not free:
            self.demand.idle, self.demand.spare_jobs = 0, None  # Busy, or none: nothing to infer.
            self._snapshot(builders)
            return
        turn = (await game.get_game_overview()).turn
        self._last_turn = turn
        jobs = [j for j in await read_builder_jobs(game) if self._buildable(j, turn)]
        threats = await read_threats(game)
        self._forget_finished_jobs(jobs, turn)
        self.open_jobs, self.jobs_turn = list(jobs), turn
        self._nudged = self._nudged_jobs(jobs, turn)
        # A builder that is mid-job but out of moves still holds its job.
        free_ids = {u.unit_id for u in free}
        held = {key[:2] for uid, key in self._claims.items() if uid not in free_ids}
        available: dict[tuple[int, int], BuilderJob] = {}
        unsafe: set[_Key] = set()
        for job in sorted(jobs, key=self._job_order):  # One job per tile: the best one.
            if self._unsafe(job, threats):
                unsafe.add(_key(job))  # Skipped for now, but a claim on it is kept for a while.
            elif (job.x, job.y) not in held:
                available.setdefault((job.x, job.y), job)
        await self._assign(game, free, available, turn, unsafe)
        self._snapshot(builders)
        # A job worked this turn is counted in `built`, so it is no longer on offer or held: the
        # judgement would count it twice, as built and as a job under way.
        self.open_jobs = [j for j in self.open_jobs if _key(j) not in self._built_keys]
        self.claimed = frozenset(self._claims.values()) - self._built_keys

    # ----- bookkeeping -----

    def _forget_gone_units(self, builders: Iterable[Unit]) -> None:
        builders = list(builders)
        alive = {u.unit_id for u in builders}  # Including builders with no moves left this turn.
        self._note_lost(alive)
        self._claims = {k: v for k, v in self._claims.items() if k in alive}
        self._travel = {k: v for k, v in self._travel.items() if k in alive}
        self._work = {k: v for k, v in self._work.items() if k in alive}
        self._holds = {k: v for k, v in self._holds.items() if k in alive}

    def _snapshot(self, builders: Iterable[Unit]) -> None:
        """Remember each builder where it ended its turn, which is where a loss is placed."""
        self._seen = {
            u.unit_id: (*self._moved.get(u.unit_id, (u.x, u.y)), self._claim_tile(u.unit_id))
            for u in builders
        }
        self._moved.clear()

    def _claim_tile(self, unit_id: int) -> tuple[int, int] | None:
        claim = self._claims.get(unit_id)
        return None if claim is None else claim[:2]

    def _note_lost(self, alive: set[int]) -> None:
        """A builder that vanished away from its job was lost (one finishing a job on its last
        charge is gone at its job tile, which is no loss). Log it and remember the place."""
        self._movement().trips.sweep(alive, self._last_turn, "builder")
        for uid, (x, y, claim) in self._seen.items():
            if uid in alive or claim == (x, y):
                continue
            self.lost.append((x, y, self._last_turn))
            log.info(
                "builder_lost %s",
                json.dumps(
                    {"turn": self._last_turn, "unit_id": uid, "x": x, "y": y, "job": claim},
                    sort_keys=True,
                ),
            )

    def danger_at(self, x: int, y: int, turn: int) -> float:
        """Raw danger for a builder on a tile: the threat map's, and the memory of a builder lost
        near here in the last `lost_turns` turns (#389)."""
        s = self.settings
        danger = 0.0
        if self.threat_map is not None:
            danger = self.threat_map.raw_danger_at(x, y, turn)
        for lx, ly, lturn in self.lost:
            if (
                turn - lturn <= s.lost_turns
                and hex_distance(x, y, lx, ly, s.map_width) <= s.lost_radius
            ):
                danger = max(danger, s.lost_danger)
        return danger

    def _movement(self) -> Movement:
        """The shared service, or one over this policy's terrain and threat map (#399)."""
        if self.movement is None:
            self.movement = Movement(
                self.terrain, self.threat_map, MovementSettings(map_width=self.settings.map_width)
            )
        return self.movement

    def _route_danger(self, unit: Unit, job: BuilderJob, turn: int) -> float:
        """The worst danger on the job tile and the walking route to it, up to `danger_cap`."""
        danger = self._movement().route_danger(
            (unit.x, unit.y), (job.x, job.y), turn, danger_at=self.danger_at
        )
        return min(self.settings.danger_cap, danger)

    def _reach(self, unit: Unit) -> dict | None:
        """Turns to walk to each tile the builder can reach (the game's rule: a unit may enter a
        tile while it has any movement left), None with no terrain store."""
        found = self._movement().turn_field((unit.x, unit.y), max(1.0, unit.max_moves))
        return None if found is None else {tile: float(t) for tile, t in found.items()}

    def _walk(self, reached: dict | None, tile: tuple[int, int]) -> float | None:
        """Turns to walk to a tile; None (use hex distance) with no store. A tile the walk can't
        reach within the horizon is worse than any that it can."""
        if reached is None:
            return None
        return reached.get(tile, self.settings.max_travel_turns + 1.0)

    def _forget_finished_jobs(self, jobs: list[BuilderJob], turn: int) -> None:
        live = {_key(j) for j in jobs}
        self._claims = {k: v for k, v in self._claims.items() if v in live}
        self._failures = {k: v for k, v in self._failures.items() if k in live}
        self._set_aside = {k: v for k, v in self._set_aside.items() if v >= turn}

    def _buildable(self, job: BuilderJob, turn: int) -> bool:
        if not job.improvement.startswith("IMPROVEMENT_") or job.improvement in _UNBUILDABLE:
            return False
        return self._set_aside.get(_key(job), -1) < turn

    def _unsafe(self, job: BuilderJob, threats: Iterable[Threat]) -> bool:
        return any(
            t.hostile
            and hex_distance(job.x, job.y, t.x, t.y, self.settings.map_width)
            <= self.settings.threat_radius
            for t in threats
        )

    def _nudged_jobs(self, jobs: Iterable[BuilderJob], turn: int) -> set[_Key]:
        """Jobs the current boost nudges mean. Empty unless the setting is on."""
        if not self.settings.use_boost_nudges or self.nudges is None:
            return set()
        wanted = [n for n in self.nudges(turn) if n.policy == "builder" and n.site]
        nudged: set[_Key] = set()
        # A job a builder already holds is the plan, not an addition, so a nudge skips it.
        held = set(self._claims.values())
        for nudge in wanted:
            # One nudge is one detour: the first matching job in a stable order, not every
            # job that reads the same ("a tile near Rome").
            matching = sorted(
                (
                    j
                    for j in jobs
                    if _key(j) not in held and job_matches(j, nudge.target, nudge.site)
                ),
                key=lambda j: (j.x, j.y, j.improvement),
            )
            if matching:
                nudged.add(_key(matching[0]))
        return nudged

    def _bonus(self, job: BuilderJob) -> float:
        return self.settings.weights.nudge_bonus if _key(job) in self._nudged else 0.0

    def _job_order(self, job: BuilderJob) -> tuple:
        """Sort key that keeps one job per tile deterministic: the best raw score wins."""
        raw = score_job(job, 0, 1.0, self.settings.weights) + self._bonus(job)
        return -raw, job.improvement, job.x, job.y

    # ----- assignment -----

    async def _assign(
        self,
        game: BuilderGame,
        free: list[Unit],
        available: dict[tuple[int, int], BuilderJob],
        turn: int,
        unsafe: set[_Key],
    ) -> None:
        s = self.settings
        attempts = {u.unit_id: 0 for u in free}
        done: set[int] = set()
        tried: set[tuple[int, tuple[int, int]]] = set()
        reaches: dict[int, dict | None] = {}
        costs: dict[tuple[int, tuple[int, int]], tuple[float | None, float]] = {}
        while True:
            best = None
            for unit in free:
                if unit.unit_id in done or attempts[unit.unit_id] >= s.attempts_per_builder:
                    continue
                for tile, job in available.items():
                    if (unit.unit_id, tile) in tried:
                        continue
                    distance = hex_distance(unit.x, unit.y, job.x, job.y, s.map_width)
                    if (unit.unit_id, tile) not in costs:
                        if unit.unit_id not in reaches:
                            reaches[unit.unit_id] = self._reach(unit)
                        reached = reaches[unit.unit_id]
                        costs[unit.unit_id, tile] = (
                            self._walk(reached, tile),
                            self._route_danger(unit, job, turn),
                        )
                    walk, danger = costs[unit.unit_id, tile]
                    value = score_job(job, distance, unit.max_moves, s.weights, danger, walk)
                    value += self._bonus(job)
                    if self._claims.get(unit.unit_id) == _key(job):
                        value += s.weights.sticky
                    rank = (-value, job.x, job.y, unit.unit_id)
                    if best is None or rank < best[0]:
                        best = (rank, unit, job, value)
            if best is None:
                break
            _, unit, job, value = best
            tile = (job.x, job.y)
            if await self._work_on(game, unit, job, value, turn):
                done.add(unit.unit_id)
                del available[tile]
            else:
                tried.add((unit.unit_id, tile))
                attempts[unit.unit_id] += 1
                if self._set_aside.get(_key(job), -1) >= turn:
                    del available[tile]  # Refused for every builder, not just this one.
        self.demand.idle = 0
        self.demand.spare_jobs = len(available)
        for unit in free:
            if unit.unit_id not in done:
                reason = "no job on offer" if not available else "every job tried was refused"
                self.demand.idle += reason == "no job on offer"
                claim = self._claims.get(unit.unit_id)
                holds = self._holds.get(unit.unit_id, 0) + 1
                if claim in unsafe and holds <= s.max_hold_turns:
                    self._holds[unit.unit_id] = (
                        holds  # A hostile unit passing must not cost the walk.
                    )
                    reason = "job is near a hostile unit, holding the claim"
                else:
                    self._claims.pop(unit.unit_id, None)
                    self._holds.pop(unit.unit_id, None)
                    self._movement().trips.abandon(unit.unit_id, turn, "idle")
                self._record(BuilderDecision(turn, unit.unit_id, unit.x, unit.y, "idle", reason))

    async def _work_on(
        self, game: BuilderGame, unit: Unit, job: BuilderJob, value: float, turn: int
    ) -> bool:
        """Walk towards the job and work on it if the builder is there. False means refused."""
        key = _key(job)
        target = (job.x, job.y)

        def record(action: str, reason: str, result: str = "") -> None:
            self._record(
                BuilderDecision(
                    turn, unit.unit_id, unit.x, unit.y, action, reason, target,
                    job.improvement, value, result,
                )
            )  # fmt: skip

        trips = self._movement().trips
        arrived = (unit.x, unit.y) == target
        if arrived:
            trips.observe(unit.unit_id, target, turn)
        if not arrived:
            if self._turns(self._travel, unit.unit_id, key, turn) > self.settings.max_travel_turns:
                self._give_up(unit, key, turn)
                record("failed", "walked too long without arriving")
                return False
            self._open_trip(unit, target, turn)
            result = await game.move_unit(unit.unit_index, *target)
            if not move_succeeded(result):
                reason = "territory" if "territory" in result.lower() else "refused"
                trips.refuse(unit.unit_id, target, reason, turn)
                trips.abandon(unit.unit_id, turn, "refused")
                self._fail(key, turn)
                record("failed", "move refused", result)
                return False
            self._claims[unit.unit_id] = key
            self._holds.pop(unit.unit_id, None)
            reached = _NOW_AT.search(result)
            if reached:
                self._moved[unit.unit_id] = (int(reached.group(1)), int(reached.group(2)))
            if not reached or (int(reached.group(1)), int(reached.group(2))) != target:
                record("move", "heading for the job", result)
                return True
            trips.arrived(unit.unit_id, turn)
            self._travel.pop(unit.unit_id, None)
            # The move may have used the builder's moves up, which `_build` tells apart.
        elif (
            unit.valid_improvements
            and not job.is_repair
            and job.improvement not in unit.valid_improvements
        ):
            self._set_aside[key] = turn + self.settings.retry_turns  # True for any builder.
            if self._claims.get(unit.unit_id) == key:
                del self._claims[unit.unit_id]
            record("failed", "the game does not offer this improvement here")
            return False
        return await self._build(game, unit, job, record, key, turn)

    async def _build(self, game, unit, job, record, key, turn) -> bool:
        if self._turns(self._work, unit.unit_id, key, turn) > self.settings.max_work_turns:
            self._give_up(unit, key, turn)
            record("failed", "worked too long without finishing")
            return False
        if job.is_repair:
            action, result = "repair", await game.repair_improvement(unit.unit_index)
        else:
            action, result = "improve", await game.improve_tile(unit.unit_index, job.improvement)
        if action_succeeded(result):
            self._claims[unit.unit_id] = key
            self._holds.pop(unit.unit_id, None)
            self._failures.pop(key, None)
            if action == "improve":
                self._count_built(job, key)
            record(action, "working on the job", result)
            return True
        if "no moves remaining" in result.lower():
            # Walked here with the last of its moves. It starts work next turn.
            self._claims[unit.unit_id] = key
            record("move", "arrived with no moves left", result)
            return True
        if job.resource and "remove_feature" in result:
            removed = await game.remove_feature(unit.unit_index)
            if action_succeeded(removed):
                self._claims[unit.unit_id] = key
                record("remove_feature", "a feature blocks the improvement", removed)
                return True
            result = f"{result} / {removed}"
        elif "remove_feature first" in result:
            # A feature stands on the tile (forest or jungle) and the job is not a resource we
            # would clear for. Pick another tile now instead of retrying this one.
            self._give_up(unit, key, turn)
            record("failed", "a feature blocks the improvement, tile set aside", result)
            return False
        self._fail(key, turn)
        record("failed", "improvement refused", result)
        return False

    def _count_built(self, job: BuilderJob, key: _Key) -> None:
        """Count an improvement once per tile and type: builders keep working a job over turns."""
        if key in self._built_keys:
            return
        self._built_keys.add(key)
        kind = job_key(job)
        self.built[kind] = self.built.get(kind, 0) + 1
        if job.resource:
            self.built_on_resource[kind] = self.built_on_resource.get(kind, 0) + 1

    def _turns(self, table: dict, unit_id: int, key: _Key, turn: int) -> int:
        """Count the turns a builder has spent on a job, once per turn however often it is asked."""
        previous = table.get(unit_id)
        if previous is None or previous[0] != key:
            table[unit_id] = (key, 1, turn)
        elif previous[2] != turn:
            table[unit_id] = (key, previous[1] + 1, turn)
        return table[unit_id][1]

    def _fail(self, key: _Key, turn: int) -> None:
        count = self._failures.get(key, 0) + 1
        if count >= self.settings.max_failures:
            self._failures.pop(key, None)
            self._set_aside[key] = turn + self.settings.retry_turns
        else:
            self._failures[key] = count

    def _open_trip(self, unit: Unit, target: tuple[int, int], turn: int) -> None:
        """Start logging the walk to a job (#399), unless this unit is already on it."""
        trips = self._movement().trips
        open_trip = trips.trip(unit.unit_id)
        if open_trip is not None and open_trip.goal == target:
            return
        estimate, basis = self._movement().estimate(
            (unit.x, unit.y), target, max(1.0, unit.max_moves)
        )
        trips.start(unit.unit_id, "builder", (unit.x, unit.y), target, turn, estimate, basis)

    def _give_up(self, unit: Unit, key: _Key, turn: int) -> None:
        self._movement().trips.abandon(unit.unit_id, turn, "gave_up")
        self._set_aside[key] = turn + self.settings.retry_turns
        if self._claims.get(unit.unit_id) == key:
            del self._claims[unit.unit_id]
        self._travel.pop(unit.unit_id, None)
        self._work.pop(unit.unit_id, None)

    def _record(self, decision: BuilderDecision) -> None:
        self.decisions.append(decision)
        log.info("builder_decision %s", json.dumps(asdict(decision), sort_keys=True))
