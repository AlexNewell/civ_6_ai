"""The camp mission (issue 398): one object per barbarian camp, carried across turns.

Ten PRs fixed one failure each of the camp work (the need swinging, garrisons never released,
settlers outbidding camp units, a camp never located, a target that flipped, units advancing one
at a time, a muster chosen by hex distance, cover and the force pulling the same units apart,
holding winning ties, orders sent across water) and none was gated on a camp being cleared.
This module holds the state those fixes lacked and the rules that move it:

    DISCOVER -> GATHER -> APPROACH -> ASSAULT -> DONE        (ABORT from APPROACH or ASSAULT)

- DISCOVER: a camp is inferred from where barbarian units keep arriving (the threat map's
  suspected camp, issue 293) but nobody has seen it. One unit is sent to look, chosen by a
  weighted score (a scout is cheap and quick; a soldier is slower and risks more).
- GATHER: the force assembles at a muster tile on the walking route and commits once the
  rallied force beats the need by enough, against the walk still ahead and the risk on the way.
- APPROACH / ASSAULT: the committed force walks in, then fights. It is called off when the
  strength margin, less the walk, the risk and the losses, falls under a floor.
- ABORT: units are released. The mission reopens when the facts improve (more force, a smaller
  need), never on a timer.

Everything here is a pure function or plain data. The military policy feeds it facts and
carries out what it decides. Every threshold is a weight in `MissionSettings`, not a filter.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from enum import StrEnum

Tile = tuple[int, int]


class Phase(StrEnum):
    DISCOVER = "discover"
    GATHER = "gather"
    APPROACH = "approach"
    ASSAULT = "assault"
    DONE = "done"
    ABORT = "abort"


COMMITTED = (Phase.APPROACH, Phase.ASSAULT)
OPEN = (Phase.DISCOVER, Phase.GATHER, Phase.APPROACH, Phase.ASSAULT, Phase.ABORT)


@dataclass(frozen=True)
class MissionSettings:
    """Weights of the mission's decisions (issue 398). A score is gain less cost; a decision is
    taken when it reaches its floor, so a big margin can outweigh a long walk and the reverse."""

    # Commit: margin of the rallied force over the need (0 = just enough, capped at +/-1) times
    # this, less the walk still ahead (turns of the slowest unit to the camp) and the risk to the
    # rallied units (hostile units, not the camp's own guards, that reach their tiles).
    margin_weight: float = 4.0
    walk_weight: float = 0.4  # Per turn of walking still to do.
    risk_weight: float = 3.0  # Per unit of risk, 0 to 1 (the share of the force exposed).
    loss_weight: float = 1.5  # Per unit lost on this camp (abort score only).
    # Per setback: evidence that the mission got worse (the need rose, a defender reinforced the
    # camp, a unit was lost, a unit's way to the camp closed). A force waiting for a reinforcement
    # that is in production has none, so it is never called off for waiting. Progress (the mean
    # walk fell, a unit rallied or joined, a hit landed) clears them. Issue 398, live: a 20-turn
    # cutoff ended [55,7] at T72 (need 9, available 9) and [62,17] at T47 (need 8, available 12),
    # both forces that were not worse off, only slow. At 1.0 three setbacks call off a force with
    # no margin, five one with a margin of +0.5 and a short walk.
    setback_weight: float = 1.0
    # Muster capacity (live STD_T1_03 T56-66: the first unit rallied in a one-tile corridor and
    # stopped, the two behind it could not pass and never counted). A muster tile that cannot
    # hold the force costs this per unit it has no room for.
    muster_capacity_weight: float = 1.5
    # A unit this near (tiles of route) behind a rallied unit, in an unbroken line of our own
    # units, is as close to the camp as the ground allows and counts as rallied. The corridor
    # units stood one tile apart ((61,8), (60,8), (59,7)).
    queue_gap: int = 2
    # A melee unit the capture needs weighs this against leaving the force to fight (its attack
    # option scores camp_attack_gain 4 and a sure kill 2 more on the same scale).
    melee_stay_weight: float = 3.0
    # Commit when the score is at least this. Below zero, so a force just big enough that is a
    # turn or two from the camp may go in; a thin margin with a long walk or hostiles about may not.
    commit_floor: float = -1.5
    abort_floor: float = -3.0  # Called off when the abort score is below this.
    reopen_gain: float = (
        1.0  # An aborted mission reopens when its score is this far above the floor.
    )
    # Discovery: a scout scores `scout_bonus` for being quick and cheap; the walk and the risk at
    # the tile it would stand on cost; a soldier also costs its strength (it is a defender).
    scout_bonus: float = 2.0
    discover_walk_weight: float = 0.5  # Per turn of walking to the look-out tile.
    discover_risk_weight: float = 0.5  # Per point of threat-map strength on the tile it stops on.
    discover_strength_weight: float = 0.05  # Per point of the unit's strength (a soldier's cost).
    discover_floor: float = -6.0  # Nobody goes while the best score is below this.
    # Muster tile (lower is better): the mean walk of the force to it, its distance on to the camp,
    # hostile units that can strike it. The old tile is kept while within `muster_keep_margin`.
    muster_walk_weight: float = 1.0
    muster_near_weight: float = 0.6
    muster_exposure_weight: float = 2.0
    muster_keep_margin: float = 0.75
    confirm_radius: int = 5  # A camp seen this near the inferred spot is the one the look found.
    muster_slack: float = 2.0  # Walking cost past `camp_muster_walk` a muster tile may lie.


# ----- facts and verdicts -----


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def margin(have: float, need: float) -> float:
    """How far the force is over (positive) or under (negative) what the camp needs, as a share
    of the need. No need (an empty camp) is a full margin."""
    if need <= 0:
        return 1.0
    return have / need - 1.0


@dataclass(frozen=True)
class Verdict:
    go: bool
    score: float
    reason: str


def commit_verdict(
    have: float, need: float, walk_turns: float, risk: float, s: MissionSettings
) -> Verdict:
    """Commit the force to the assault? `have` and `need` are on one scale (attacks in the strike
    window); `walk_turns` is what the slowest unit still walks to the camp; `risk` is 0 to 1."""
    m = margin(have, need)
    score = s.margin_weight * clamp(m) - s.walk_weight * walk_turns - s.risk_weight * risk
    go = score >= s.commit_floor
    why = (
        f"margin {m:+.2f}, walk {walk_turns:g} turns, risk {risk:.2f}: "
        f"score {score:.1f} {'meets' if go else 'is under'} {s.commit_floor:g}"
    )
    return Verdict(go, score, why)


def abort_score(
    have: float,
    need: float,
    walk_turns: float,
    risk: float,
    losses: int,
    s: MissionSettings,
    setbacks: int = 0,
    supplying: bool = False,
) -> float:
    """`setbacks` counts evidence of getting worse. `supplying` is a force still gathering, whose
    shortfall production is building: a short margin is then no reason to call it off (floor 0),
    only a setback is."""
    m = clamp(margin(have, need))
    if supplying:
        m = max(m, 0.0)
    return (
        s.margin_weight * m
        - s.walk_weight * walk_turns
        - s.risk_weight * risk
        - s.loss_weight * losses
        - s.setback_weight * setbacks
    )


def abort_verdict(
    have: float,
    need: float,
    walk_turns: float,
    risk: float,
    losses: int,
    s: MissionSettings,
    setbacks: int = 0,
    supplying: bool = False,
) -> Verdict:
    """Call off the force? `go` is True to abort."""
    score = abort_score(have, need, walk_turns, risk, losses, s, setbacks, supplying)
    stop = score < s.abort_floor
    why = (
        f"margin {margin(have, need):+.2f}, walk {walk_turns:g} turns, risk {risk:.2f}, "
        f"{losses} lost, {setbacks} setbacks: score {score:.1f} "
        f"{'is under' if stop else 'holds above'} {s.abort_floor:g}"
    )
    return Verdict(stop, score, why)


def should_reopen(
    have: float, need: float, walk_turns: float, risk: float, losses: int, s: MissionSettings
) -> Verdict:
    """An aborted mission reopens once its abort score has recovered by `reopen_gain` over the
    floor: more force, a smaller need, fewer losses. A change of the facts, not a timer."""
    score = abort_score(have, need, walk_turns, risk, losses, s)
    go = score >= s.abort_floor + s.reopen_gain
    return Verdict(go, score, f"score {score:.1f}, reopens at {s.abort_floor + s.reopen_gain:g}")


def progressed(
    mean_cost: float,
    last_cost: float | None,
    rallied: int,
    last_rallied: int,
    units: int,
    last_units: int,
    hit: bool,
    phase_changed: bool,
    eps: float = 0.5,
) -> bool:
    """Did the force get anywhere since the last check? Its mean walk to the camp fell, more
    units are rallied or have joined, a hit landed on the camp, or the phase just changed."""
    return (
        last_cost is None
        or mean_cost < last_cost - eps
        or rallied > last_rallied
        or units > last_units
        or hit
        or phase_changed
    )


def setback_events(
    last_need: float | None,
    need: float,
    last_losses: int,
    losses: int,
    last_open: bool,
    ways_open: bool,
    eps: float = 0.5,
) -> int:
    """Evidence the mission got worse since the last check: the need rose (a defender joined the
    camp or was seen), a unit was lost on it, or a unit's way to the camp closed."""
    return (
        (1 if last_need is not None and need > last_need + eps else 0)
        + max(0, losses - last_losses)
        + (1 if last_open and not ways_open else 0)
    )


def column_rallied(
    rallied: Collection[int],
    positions: Mapping[int, Tile],
    routes: Mapping[int, list[Tile]],
    gap: int,
) -> set[int]:
    """Who counts as rallied: those at the muster, and those in an unbroken line of our own units
    on their route behind one of them. A unit queued within `gap` route tiles behind a rallied
    unit (or behind a queued one) is as near the camp as the ground allows, a one-tile corridor
    taking one unit at a time. `routes` are each unit's route to the camp, own tile first."""
    counted = set(rallied)
    spots = dict(positions)
    changed = True
    while changed:
        changed = False
        for uid, route in routes.items():
            if uid in counted:
                continue
            ahead = route[1 : gap + 1]
            if any(spots[o] in ahead for o in counted if o in spots and o != uid):
                counted.add(uid)
                changed = True
    return counted


def muster_capacity(
    tile: Tile,
    cost: Mapping[Tile, float],
    limit: float,
    usable: Callable[[Tile], bool],
    around: Callable[[Tile], Iterable[Tile]],
) -> int:
    """How many units can stand rallied at and beside the tile: the tile and its neighbours that
    are usable and within `limit` walking cost of the camp. A one-tile corridor holds two or
    three, open ground six or seven."""
    spots = [tile, *around(tile)]
    return sum(1 for p in spots if p in cost and cost[p] <= limit and usable(p))


def facts_improved(
    have: float, need: float, risk: float, then: tuple[float, float, float], eps: float = 0.5
) -> bool:
    """After a call-off for lack of progress: more force, a smaller need or less risk about than
    when it was called off. The same facts reopen nothing."""
    return have > then[0] + eps or need < then[1] - eps or risk < then[2] - eps


# ----- discovery -----


@dataclass(frozen=True)
class Discoverer:
    """A unit that could go and look at the suspected camp, and the tile it would stop on."""

    unit_id: int
    scout: bool
    walk_turns: float
    threat: float  # Threat-map strength on the stop tile.
    strength: float  # Its combat strength (a soldier's cost; 0 for a scout).
    stop: Tile


def discover_score(c: Discoverer, s: MissionSettings) -> float:
    return (
        (s.scout_bonus if c.scout else 0.0)
        - s.discover_walk_weight * c.walk_turns
        - s.discover_risk_weight * c.threat
        - (0.0 if c.scout else s.discover_strength_weight * c.strength)
    )


def choose_discoverer(
    candidates: Iterable[Discoverer], s: MissionSettings, current: int | None = None
) -> tuple[Discoverer | None, str]:
    """The unit to send and why, or None and why not. The one already walking out goes on unless
    another scores clearly better (a tie keeps it, then the lower id)."""
    ranked = sorted(
        ((discover_score(c, s), c) for c in candidates),
        key=lambda sc: (-sc[0], sc[1].unit_id != current, sc[1].unit_id),
    )
    if not ranked:
        return None, "no unit available to look"
    score, best = ranked[0]
    if score < s.discover_floor:
        return None, f"best look-out scores {score:.1f}, under {s.discover_floor:g}: too risky"
    kind = "scout" if best.scout else "soldier"
    return best, f"{kind} {best.unit_id} goes to look (score {score:.1f})"


# ----- the muster tile -----


def muster_score(
    walks: Iterable[float],
    camp_cost: float,
    exposed: int,
    s: MissionSettings,
    no_room: int = 0,
) -> float:
    """What a muster tile costs, lower being better: the mean turns the force takes to reach it,
    its walking cost on to the camp, the hostile units that can strike it next turn, and the
    units of the force that have no room to stand rallied there (`no_room`)."""
    turns = list(walks)
    mean = sum(turns) / len(turns) if turns else 0.0
    return (
        s.muster_walk_weight * mean
        + s.muster_near_weight * camp_cost
        + s.muster_exposure_weight * exposed
        + s.muster_capacity_weight * no_room
    )


def choose_muster(
    scores: Mapping[Tile, float], previous: Tile | None, keep_margin: float
) -> Tile | None:
    """The lowest scoring tile (ties to the lowest coordinates), but the previous tile while it
    scores within `keep_margin` of it, so the force does not chase a tile that moves each turn."""
    if not scores:
        return None
    best = min(scores, key=lambda p: (scores[p], p))
    if previous in scores and scores[previous] <= scores[best] + keep_margin:
        return previous
    return best


def muster_candidates(
    route: Iterable[Tile],
    camp_cost: Mapping[Tile, float],
    walk: float,
    slack: float,
    usable: Callable[[Tile], bool],
) -> list[Tile]:
    """Tiles of the walking route inside the muster walk of the camp: the route to a camp is
    where the force really passes, so a shore across a bay is never a candidate."""
    return [p for p in route if p in camp_cost and 0 < camp_cost[p] <= walk + slack and usable(p)]


# ----- one role per unit -----


def assign_roles(
    held: Collection[int],
    eligible: Mapping[int, bool],
    taken: Mapping[int, str],
    short_city: Collection[int],
    reachable: Collection[int],
    wanted: Collection[int] = (),
) -> tuple[dict[int, str], dict[int, str]]:
    """Decide who the mission holds this turn, before any other policy moves a unit.

    `held` are the units it had; `eligible` maps a unit to whether it is healthy and not needed
    by the city it stands in or beside; `taken` are units another policy has already given a
    role this turn (heal, retreat, flee); `short_city` are units whose city the threat map calls
    short (they go back to it, they are not the camp's); `reachable` are units within the camp's
    reach; `wanted` are free units to add. Returns (units with their role, released with why).
    A unit is held by one role only, and the cover step cannot take it back (issue 397: the two
    swapped one unit between them for ten turns)."""
    units: dict[int, str] = {}
    released: dict[int, str] = {}
    for uid in sorted({*held, *wanted}):
        if uid in taken:
            released[uid] = f"given the role {taken[uid]} this turn"
        elif uid in short_city:
            released[uid] = "its city is short by the threat map"
        elif not eligible.get(uid, False):
            released[uid] = "not healthy or not free"
        elif uid not in reachable:
            released[uid] = "out of the camp's reach"
        else:
            units[uid] = "march"
    return units, released


# ----- the object -----


@dataclass
class CampMission:
    camp: Tile  # The camp, or the suspected spot while it is only inferred.
    phase: Phase = Phase.GATHER
    confirmed: bool = True  # The camp has been seen (False while DISCOVER).
    since: int = 0  # Turn the phase began.
    born: int = 0
    units: dict[int, str] = field(default_factory=dict)  # Unit id to its one role this turn.
    muster: Tile | None = None
    walk_turns: int = 0  # The slowest unit's walk to the muster tile (or to the camp, committed).
    needed: float = 0.0
    available: float = 0.0
    shortfall: int = 0  # Units asked of production.
    reason: str = ""
    score: float | None = None
    outcome: str = ""  # cleared / by_other / aborted / no_camp, once DONE.
    aborted_at: tuple[float, float] | None = None  # (have, need) when it was called off.
    losses: int = 0
    # Progress evidence (issue 398): the force's mean walk to the camp at the last check, how many
    # were rallied and in the force, and the checks since any progress.
    last_cost: float | None = None
    last_rallied: int = 0
    last_units: int = 0
    setbacks: int = 0
    last_need: float | None = None
    last_losses: int = 0
    last_open: bool = True
    aborted_for_setback: bool = False
    aborted_risk: float = 0.0

    def move_to(self, phase: Phase, turn: int, reason: str = "") -> bool:
        """Change phase. True if it changed. The reason is kept for the log line."""
        changed = phase != self.phase
        if changed:
            self.phase = phase
            self.since = turn
        if reason:
            self.reason = reason
        return changed

    @property
    def committed(self) -> bool:
        return self.phase in COMMITTED

    def as_row(self, turn: int) -> dict:
        return {
            "turn": turn,
            "camp": list(self.camp),
            "phase": self.phase.value,
            "phase_turns": turn - self.since,
            "confirmed": self.confirmed,
            "units": {str(k): v for k, v in sorted(self.units.items())},
            "muster": None if self.muster is None else list(self.muster),
            "walk_turns": self.walk_turns,
            "needed": round(self.needed, 1),
            "available": round(self.available, 1),
            "shortfall": self.shortfall,
            "score": None if self.score is None else round(self.score, 2),
            "outcome": self.outcome,
            "setbacks": self.setbacks,
            "reason": self.reason,
        }

    def log_line(self, turn: int) -> str:
        return "camp_mission " + json.dumps(self.as_row(turn), sort_keys=True)


@dataclass
class MissionBook:
    """Every mission, by camp tile. Closed missions are dropped after their last line."""

    missions: dict[Tile, CampMission] = field(default_factory=dict)

    def open(self, camp: Tile, turn: int, confirmed: bool = True) -> CampMission:
        found = self.missions.get(camp)
        if found is not None:
            return found
        phase = Phase.GATHER if confirmed else Phase.DISCOVER
        mission = CampMission(camp, phase, confirmed, since=turn, born=turn)
        self.missions[camp] = mission
        return mission

    def discover(self, spot: Tile, turn: int) -> CampMission:
        """The one DISCOVER mission, at `spot`. The guess moves as arrivals add up, so an open one
        is moved to the new spot (kept, with its phase and units) rather than a second opened."""
        for camp, mission in list(self.missions.items()):
            if mission.phase is Phase.DISCOVER and not mission.confirmed:
                if camp != spot:
                    del self.missions[camp]
                    mission.camp = spot
                    self.missions[spot] = mission
                return mission
        return self.open(spot, turn, confirmed=False)

    def discover_of(self) -> CampMission | None:
        return next(
            (m for m in self.missions.values() if m.phase is Phase.DISCOVER and not m.confirmed),
            None,
        )

    def get(self, camp: Tile) -> CampMission | None:
        return self.missions.get(camp)

    def live(self) -> list[CampMission]:
        return [m for m in self.missions.values() if m.phase in OPEN]

    def committed(self, camp: Tile) -> bool:
        m = self.missions.get(camp)
        return m is not None and m.committed

    def role_of(self, unit_id: int) -> tuple[CampMission, str] | None:
        for m in self.missions.values():
            if m.phase in OPEN and unit_id in m.units:
                return m, m.units[unit_id]
        return None

    def confirm(self, spot: Tile, camp: Tile, turn: int) -> CampMission | None:
        """The look found the camp: the DISCOVER mission at `spot` moves to the camp tile."""
        mission = self.missions.pop(spot, None)
        if mission is None or mission.phase is not Phase.DISCOVER:
            if mission is not None:
                self.missions[spot] = mission
            return self.missions.get(camp)
        mission.camp = camp
        mission.confirmed = True
        # The look is done: its look-out is free to join the force (or any other order).
        mission.units = {u: r for u, r in mission.units.items() if r != "discover"}
        mission.move_to(Phase.GATHER, turn, "camp seen: gathering the force")
        self.missions[camp] = mission
        return mission

    def close(self, camp: Tile, turn: int, outcome: str, reason: str = "") -> CampMission | None:
        mission = self.missions.get(camp)
        if mission is None:
            return None
        mission.outcome = outcome
        mission.units = {}
        mission.move_to(Phase.DONE, turn, reason or outcome)
        return mission

    def sweep(self) -> None:
        """Drop DONE missions (the line for the turn they closed is already written)."""
        self.missions = {c: m for c, m in self.missions.items() if m.phase is not Phase.DONE}


class CommittedView(MutableMapping):
    """`{camp: turn committed}` over the book, for the code that asks whether a force is going in.

    Setting commits the camp's mission (APPROACH); deleting sends it back to GATHER."""

    def __init__(self, book: MissionBook) -> None:
        self.book = book
        self.turn_of: dict[Tile, int] = {}
        self.now = 0  # The current turn, kept up by the policy, for a mission sent back.

    def __getitem__(self, camp: Tile) -> int:
        if not self.book.committed(camp):
            raise KeyError(camp)
        return self.turn_of.get(camp, 0)

    def __setitem__(self, camp: Tile, turn: int) -> None:
        mission = self.book.open(camp, turn)
        self.turn_of[camp] = turn
        if not mission.committed:
            mission.move_to(Phase.APPROACH, turn, "committed: the rallied force meets the need")

    def __delitem__(self, camp: Tile) -> None:
        mission = self.book.get(camp)
        if mission is None or not mission.committed:
            raise KeyError(camp)
        self.turn_of.pop(camp, None)
        mission.move_to(Phase.GATHER, self.now, "regrouping")

    def __iter__(self) -> Iterator[Tile]:
        return iter([c for c, m in self.book.missions.items() if m.committed])

    def __len__(self) -> int:
        return sum(1 for m in self.book.missions.values() if m.committed)
