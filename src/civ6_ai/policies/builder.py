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
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Protocol

from civ6_ai.blockers import action_succeeded, move_succeeded
from civ6_ai.policies.game_data import REPLACES
from civ6_ai.state import BuilderJob, GameReads, Threat, Unit, read_builder_jobs, read_threats
from civ6_ai.state import read_units as _read_units
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance

log = logging.getLogger(__name__)

BUILDER = "UNIT_BUILDER"
# Builders can't reach these, and the advice names "UNKNOWN" for a resource it has no rule for.
_UNBUILDABLE = frozenset({"IMPROVEMENT_FISHING_BOATS"})
_NOW_AT = re.compile(r"now_at:(-?\d+),(-?\d+)")


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


def score_job(job: BuilderJob, distance: int, max_moves: float, weights: BuilderWeights) -> float:
    """Value a job for a builder `distance` tiles away. Only visible fields are read."""
    score = {"urgent": weights.urgent, "high": weights.high}.get(job.priority, weights.normal)
    if job.is_repair:
        score += weights.repair
    elif job.resource_class == "strategic":
        score += weights.strategic
    elif job.resource_class == "luxury":
        score += weights.luxury
    elif job.resource_class == "bonus":
        score += weights.bonus
    return score - weights.turn_cost * math.ceil(distance / max(1.0, max_moves))


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
    decisions: list[BuilderDecision] = field(default_factory=list)
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
            return
        turn = (await game.get_game_overview()).turn
        jobs = [j for j in await read_builder_jobs(game) if self._buildable(j, turn)]
        threats = await read_threats(game)
        self._forget_finished_jobs(jobs, turn)
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

    # ----- bookkeeping -----

    def _forget_gone_units(self, builders: Iterable[Unit]) -> None:
        alive = {u.unit_id for u in builders}  # Including builders with no moves left this turn.
        self._claims = {k: v for k, v in self._claims.items() if k in alive}
        self._travel = {k: v for k, v in self._travel.items() if k in alive}
        self._work = {k: v for k, v in self._work.items() if k in alive}
        self._holds = {k: v for k, v in self._holds.items() if k in alive}

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

    def _job_order(self, job: BuilderJob) -> tuple:
        """Sort key that keeps one job per tile deterministic: the best raw score wins."""
        return -score_job(job, 0, 1.0, self.settings.weights), job.improvement, job.x, job.y

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
        while True:
            best = None
            for unit in free:
                if unit.unit_id in done or attempts[unit.unit_id] >= s.attempts_per_builder:
                    continue
                for tile, job in available.items():
                    if (unit.unit_id, tile) in tried:
                        continue
                    distance = hex_distance(unit.x, unit.y, job.x, job.y, s.map_width)
                    value = score_job(job, distance, unit.max_moves, s.weights)
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
        for unit in free:
            if unit.unit_id not in done:
                reason = "no job on offer" if not available else "every job tried was refused"
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

        arrived = (unit.x, unit.y) == target
        if not arrived:
            if self._turns(self._travel, unit.unit_id, key, turn) > self.settings.max_travel_turns:
                self._give_up(unit, key, turn)
                record("failed", "walked too long without arriving")
                return False
            result = await game.move_unit(unit.unit_index, *target)
            if not move_succeeded(result):
                self._fail(key, turn)
                record("failed", "move refused", result)
                return False
            self._claims[unit.unit_id] = key
            self._holds.pop(unit.unit_id, None)
            reached = _NOW_AT.search(result)
            if not reached or (int(reached.group(1)), int(reached.group(2))) != target:
                record("move", "heading for the job", result)
                return True
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

    def _give_up(self, unit: Unit, key: _Key, turn: int) -> None:
        self._set_aside[key] = turn + self.settings.retry_turns
        if self._claims.get(unit.unit_id) == key:
            del self._claims[unit.unit_id]
        self._travel.pop(unit.unit_id, None)
        self._work.pop(unit.unit_id, None)

    def _record(self, decision: BuilderDecision) -> None:
        self.decisions.append(decision)
        log.info("builder_decision %s", json.dumps(asdict(decision), sort_keys=True))
