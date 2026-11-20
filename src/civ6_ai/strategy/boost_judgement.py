"""Judge each near-horizon tech and civic by how close its boost is (#322).

For one unresearched item with a boost the bot could cause, estimate:

- reach: turns until the boost lands on the current pace. "on track" (the plan already does the
  triggering thing: population is growing, the district or improvement is queued), "cheap to
  cause" (one more improvement, one more unit, a kill the units are offered) or "unlikely".
- fit: whether the triggering action is something the plan wants anyway, or a pure detour.
- value: the boost saves about 40% of the item's cost, which is about 40% of its turns at the
  current pace, minus what waiting costs.

Then one of three decisions:

- research now: the boost is already earned, far, unlikely, or not something the bot can cause.
- delay: the boost lands on its own soon, so research something else for up to `max_delay`
  turns. It is re-checked every turn and given up when the estimate slips (Archery in the human
  game: the Slinger kill never came, and the tech waited until turn 52).
- nudge: a small bounded detour (one improvement, one unit, an offered kill) would earn it and
  is worth more than it costs. Read-only here: `nudges` lists them for a policy to use later.

Pure functions on plain inputs. Nothing here reads the game or changes what the bot does; the
research policy turns the decisions into a score adjustment behind `boost_judgement`.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from civ6_ai.state.models import ResearchOption
from civ6_ai.strategy.boosts import (
    ANY,
    BARBARIAN,
    BARBARIAN_OUTPOST,
    CITY,
    CITY_STATE,
    COASTAL_CITY,
    DIFFERENT,
    LAND_COMBAT,
    PANTHEON,
    SEA_RESOURCE,
    BoostKind,
    BoostSpec,
    parse_boost,
)


class ReachLabel(StrEnum):
    EARNED = "earned"
    ON_TRACK = "on track"
    CHEAP = "cheap to cause"
    UNLIKELY = "unlikely"


class Decision(StrEnum):
    RESEARCH_NOW = "research now"
    DELAY = "delay"
    NUDGE = "nudge"


@dataclass(frozen=True)
class JudgementSettings:
    """Tunables. Each says why it has its value."""

    # Past this many turns a boost is not "within reach": plans change, and the human only
    # waited on boosts a few turns off.
    horizon: int = 20
    # The longest an item is held back for its boost. Archery's wait to turn 52 cost more than
    # the 40% it never got, so the bound is short.
    max_delay: int = 8
    # The estimate may slip this many turns beyond what was promised before we stop waiting.
    slip_tolerance: int = 2
    # A boost saves about 40% of the cost (the `research_boosts` log).
    saving_fraction: float = 0.4
    # Turns lost per turn waited. Less than 1: other research goes on meanwhile, only this
    # item's unlock comes later.
    delay_cost_per_turn: float = 0.5
    # Items already this far along are left alone: the boost matters less and a flip wastes it.
    progress_limit: int = 20
    # The least net saving, in turns, that justifies delaying or nudging.
    min_value: float = 1.0
    # Turns of detour for one nudged action, halved when the action also fits the plan.
    nudge_cost_turns: float = 2.0
    # The most actions a nudge may add beyond what the plan already queues.
    max_nudge_count: int = 1
    # Turns per unit of each kind of action once started (builder, city production, scouting).
    improvement_turns: int = 3
    district_turns: int = 8
    unit_turns: int = 6
    building_turns: int = 8
    meeting_turns: int = 6
    wonder_turns: int = 15
    settle_turns: int = 10
    kill_turns: int = 2
    # Turns assumed for an item with no turn estimate, so its saving can still be sized.
    unknown_item_turns: int = 10


DEFAULT_SETTINGS = JudgementSettings()


@dataclass(frozen=True)
class BoostState:
    """What the game holds now, as counts. Keys are the upper-case names in `boosts.py`."""

    turn: int = 0
    population: int = 0  # All cities together.
    biggest_city_population: int = 0
    cities: int = 0
    districts: Mapping[str, int] = field(
        default_factory=dict
    )  # `CAMPUS: 1`, city centres left out.
    improvements: Mapping[str, int] = field(default_factory=dict)  # `QUARRY: 1`.
    sea_resources_improved: int = 0
    # Improvements built on a tile with a resource, by type. None when not read: boosts that
    # need a resource ("Mine a resource") are then not predicted, as ordinary mines don't count.
    resource_improvements: Mapping[str, int] | None = None
    buildings: Mapping[str, int] = field(default_factory=dict)
    units: Mapping[str, int] = field(default_factory=dict)  # `SLINGER: 2`.
    land_combat_units: int = 0
    kills: Mapping[str, int] = field(default_factory=dict)  # By killer type, plus `BARBARIAN`.
    civilizations_met: int = 0
    city_states_met: int = 0
    has_pantheon: bool = False
    has_coastal_city: bool = False


@dataclass(frozen=True)
class PlanSignals:
    """What the current plan is about to do. Empty means nothing is planned, so nothing is close."""

    # Turns for the cities to grow by one population, or None when none is growing.
    growth_turns_per_pop: float | None = None
    districts_queued: Mapping[str, int] = field(default_factory=dict)
    improvement_jobs: Mapping[str, int] = field(default_factory=dict)  # Builder jobs on offer.
    # Where a nudge could place one, by improvement: `QUARRY: ("the stone near Rome",)`.
    improvement_sites: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # The same two, counting only jobs and sites on a tile with a resource.
    resource_improvement_jobs: Mapping[str, int] = field(default_factory=dict)
    resource_improvement_sites: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    units_queued: Mapping[str, int] = field(default_factory=dict)
    buildings_queued: Mapping[str, int] = field(default_factory=dict)
    kill_offers: Mapping[str, int] = field(default_factory=dict)  # Kills the units could make.
    exploring: bool = False  # A scout is out, so meetings come.
    pantheon_turns: int | None = None  # Turns until enough faith, when one is being earned.
    coastal_city_planned: bool = False
    wonder_queued: bool = False
    research_turns: Mapping[str, int] = field(default_factory=dict)  # Short name to turns.


@dataclass(frozen=True)
class Reach:
    label: ReachLabel
    turns: int | None = None  # None when unlikely.
    source: str = ""  # "population growth", "a queued Campus", ...
    fit: bool = False
    nudge: str | None = None  # The action that would cause it, for a cheap-to-cause boost.
    policy: str | None = None  # Which policy would carry out the nudge.


@dataclass(frozen=True)
class BoostJudgement:
    item_type: str
    name: str
    spec: BoostSpec
    reach: Reach
    value: float  # Net saving in turns. Meaningful for on track and cheap to cause.
    decision: Decision
    reason: str
    gave_up: bool = False


@dataclass(frozen=True)
class DelayRecord:
    """When an item was first held back and the arrival estimate at the time."""

    since_turn: int
    first_eta: int
    gave_up: bool = False


@dataclass(frozen=True)
class Nudge:
    """A small detour that would earn a boost, for a policy to take up. Read-only."""

    item_type: str
    name: str
    action: str
    policy: str
    value: float


def _no(reason: str = "") -> Reach:
    return Reach(ReachLabel.UNLIKELY, source=reason)


def _counted(
    need: int,
    planned: int,
    each_turns: int,
    settings: JudgementSettings,
    source: str,
    policy: str,
    nudge: str | None,
    fit: bool,
) -> Reach:
    """Reach for a boost that needs `need` more of something, `planned` of them already queued."""
    if need <= 0:
        return Reach(ReachLabel.ON_TRACK, 1, source, True)
    eta = max(1, math.ceil(need * each_turns))
    if eta > settings.horizon:
        return _no("too far")
    if planned >= need:
        return Reach(ReachLabel.ON_TRACK, eta, source, True)
    missing = need - planned
    if nudge is not None and missing <= settings.max_nudge_count:
        return Reach(ReachLabel.CHEAP, eta, source, fit, nudge, policy)
    return _no("too many missing")


def _target_name(target: str) -> str:
    return target.lower().replace("_", " ")


def _improvement_reach(
    spec: BoostSpec, state: BoostState, signals: PlanSignals, settings: JudgementSettings
) -> Reach:
    if spec.on_resource:
        if state.resource_improvements is None:
            return _no("resource data not read")
        have = state.resource_improvements.get(spec.target, 0)
        jobs = signals.resource_improvement_jobs.get(spec.target, 0)
        sites = signals.resource_improvement_sites.get(spec.target, ())
    elif spec.target == ANY:
        have = sum(state.improvements.values())
        jobs = sum(signals.improvement_jobs.values())
        sites = tuple(s for group in signals.improvement_sites.values() for s in group)
    elif spec.target == SEA_RESOURCE:
        have = state.sea_resources_improved
        jobs = signals.improvement_jobs.get(SEA_RESOURCE, 0)
        sites = signals.improvement_sites.get(SEA_RESOURCE, ())
    else:
        have = state.improvements.get(spec.target, 0)
        jobs = signals.improvement_jobs.get(spec.target, 0)
        sites = signals.improvement_sites.get(spec.target, ())
    need = spec.count - have
    action = None
    if sites:
        what = "an improvement" if spec.target == ANY else f"a {_target_name(spec.target)}"
        action = f"build {what} on {sites[0]}"
    return _counted(
        need, jobs, settings.improvement_turns, settings, "a builder job", "builder", action, True
    )


def _district_reach(
    spec: BoostSpec, state: BoostState, signals: PlanSignals, settings: JudgementSettings
) -> Reach:
    if spec.target == ANY:
        have, queued = sum(state.districts.values()), sum(signals.districts_queued.values())
    elif spec.target == DIFFERENT:
        built = {t for t, n in state.districts.items() if n > 0}
        have = len(built)
        # A queued type counts only when no completed district already represents it.
        queued = sum(1 for t, n in signals.districts_queued.items() if n > 0 and t not in built)
    else:
        have, queued = (
            state.districts.get(spec.target, 0),
            signals.districts_queued.get(spec.target, 0),
        )
    # A district is a big build, never a nudge: it counts only when already queued.
    return _counted(
        spec.count - have, queued, settings.district_turns, settings, "a queued district",
        "production", None, True,
    )  # fmt: skip


def _reach(
    spec: BoostSpec, state: BoostState, signals: PlanSignals, settings: JudgementSettings
) -> Reach:
    """Reach for one boost spec. Unlikely for anything the signals don't show."""
    kind = spec.kind
    if kind is BoostKind.IMPROVEMENT:
        return _improvement_reach(spec, state, signals, settings)
    if kind is BoostKind.DISTRICT:
        return _district_reach(spec, state, signals, settings)
    if kind is BoostKind.BUILDING:
        have = state.buildings.get(spec.target, 0)
        queued = signals.buildings_queued.get(spec.target, 0)
        return _counted(
            spec.count - have, queued, settings.building_turns, settings, "a queued building",
            "production", f"queue a {_target_name(spec.target)}", False,
        )  # fmt: skip
    if kind is BoostKind.UNIT:
        have = (
            state.land_combat_units
            if spec.target == LAND_COMBAT
            else state.units.get(spec.target, 0)
        )
        queued = signals.units_queued.get(spec.target, 0)
        return _counted(
            spec.count - have, queued, settings.unit_turns, settings, "a queued unit",
            "production", f"build a {_target_name(spec.target)}", False,
        )  # fmt: skip
    if kind is BoostKind.KILL:
        need = spec.count - state.kills.get(spec.target, 0)
        if need <= 0:
            return Reach(ReachLabel.ON_TRACK, 1, "a kill already made", True)
        offers = signals.kill_offers.get(spec.target, 0)
        if offers >= need:
            what = (
                "a barbarian"
                if spec.target in (BARBARIAN, BARBARIAN_OUTPOST)
                else f"a unit with a {_target_name(spec.target)}"
            )
            return Reach(
                ReachLabel.CHEAP, settings.kill_turns, "an offered kill", True,
                f"kill {what}", "military",
            )  # fmt: skip
        return _no("no kill on offer")
    if kind is BoostKind.POPULATION:
        have = state.biggest_city_population if spec.target == CITY else state.population
        need = spec.count - have
        if need <= 0:
            return Reach(ReachLabel.ON_TRACK, 1, "population growth", True)
        rate = signals.growth_turns_per_pop
        if rate is None or rate <= 0:
            return _no("no growth")
        eta = max(1, math.ceil(need * rate))
        if eta > settings.horizon:
            return _no("too far")
        return Reach(ReachLabel.ON_TRACK, eta, "population growth", True)
    return _other_reach(spec, state, signals, settings)


def _other_reach(
    spec: BoostSpec, state: BoostState, signals: PlanSignals, settings: JudgementSettings
) -> Reach:
    kind = spec.kind
    if kind is BoostKind.MEETING:
        met = state.city_states_met if spec.target == CITY_STATE else state.civilizations_met
        need = spec.count - met
        if need <= 0:
            return Reach(ReachLabel.ON_TRACK, 1, "a meeting already made", True)
        eta = need * settings.meeting_turns
        if signals.exploring and eta <= settings.horizon:
            return Reach(ReachLabel.ON_TRACK, eta, "exploring", True)
        return _no("not exploring")
    if kind is BoostKind.FOUND:
        if spec.target == PANTHEON:
            if state.has_pantheon:
                return Reach(ReachLabel.ON_TRACK, 1, "the pantheon", True)
            turns = signals.pantheon_turns
            if turns is not None and turns <= settings.horizon:
                return Reach(ReachLabel.ON_TRACK, max(1, turns), "faith for a pantheon", True)
        if spec.target == COASTAL_CITY:
            if state.has_coastal_city:
                return Reach(ReachLabel.ON_TRACK, 1, "a coastal city", True)
            if signals.coastal_city_planned:
                return Reach(ReachLabel.ON_TRACK, settings.settle_turns, "a planned city", True)
        return _no("not planned")
    if kind is BoostKind.WONDER:
        if signals.wonder_queued:
            return Reach(ReachLabel.ON_TRACK, settings.wonder_turns, "a queued wonder", True)
        return _no("no wonder queued")
    if kind is BoostKind.ADOPT:
        turns = signals.research_turns.get(spec.target)
        if turns is not None and 0 < turns <= settings.horizon:
            return Reach(ReachLabel.ON_TRACK, turns, "research already under way", True)
        return _no("not being researched")
    return _no("not a boost the bot can cause")


def _judge_one(
    option: ResearchOption,
    state: BoostState,
    signals: PlanSignals,
    settings: JudgementSettings,
    record: DelayRecord | None,
) -> BoostJudgement:
    spec = parse_boost(option.boost_desc)

    def done(reach: Reach, reason: str, gave_up: bool = False) -> BoostJudgement:
        return BoostJudgement(
            option.item_type, option.name, spec, reach, 0.0, Decision.RESEARCH_NOW, reason, gave_up
        )

    if option.boosted:
        return done(Reach(ReachLabel.EARNED), f"researching {option.name}: boost already earned")
    if spec.kind is BoostKind.OTHER:
        return done(
            _no("not a boost the bot can cause"), f"researching {option.name}: no boost to chase"
        )
    if record is not None and record.gave_up:
        return done(
            _no("gave up"), f"researching {option.name}: gave up waiting for the boost", True
        )
    reach = _reach(spec, state, signals, settings)
    if reach.label is ReachLabel.UNLIKELY:
        return done(reach, f"researching {option.name}: boost {reach.source or 'unlikely'}")
    if option.progress_pct > settings.progress_limit:
        return done(reach, f"researching {option.name}: already {option.progress_pct}% done")

    turns = option.turns if option.turns > 0 else settings.unknown_item_turns
    saved = settings.saving_fraction * turns
    eta = reach.turns or 1

    if reach.label is ReachLabel.ON_TRACK:
        value = saved - settings.delay_cost_per_turn * eta
        if record is not None:
            waited = max(0, state.turn - record.since_turn)
            if waited >= settings.max_delay:
                return done(reach, f"researching {option.name}: waited {waited} turns", True)
            if eta > record.first_eta - waited + settings.slip_tolerance:
                return done(
                    reach,
                    f"researching {option.name}: boost slipped to about {eta} turns",
                    True,
                )
        if eta > settings.max_delay:
            return done(reach, f"researching {option.name}: boost in about {eta} turns is too far")
        if value < settings.min_value:
            return done(reach, f"researching {option.name}: waiting saves too little")
        return BoostJudgement(
            option.item_type,
            option.name,
            spec,
            reach,
            value,
            Decision.DELAY,
            f"delaying {option.name}: boost in about {eta} turns from {reach.source}",
        )

    # Cheap to cause: a detour of `missing` actions, cheaper when it fits the plan anyway.
    detour = settings.nudge_cost_turns * (0.5 if reach.fit else 1.0)
    value = saved - detour
    if value < settings.min_value:
        return done(reach, f"researching {option.name}: the detour costs more than the boost saves")
    return BoostJudgement(
        option.item_type,
        option.name,
        spec,
        reach,
        value,
        Decision.NUDGE,
        f"nudging for {option.name}: {reach.nudge} would earn the boost",
    )


def judge_boosts(
    options: Iterable[ResearchOption],
    state: BoostState,
    signals: PlanSignals,
    settings: JudgementSettings = DEFAULT_SETTINGS,
    history: Mapping[str, DelayRecord] | None = None,
) -> list[BoostJudgement]:
    """One judgement per option, in the order given."""
    history = history or {}
    return [_judge_one(o, state, signals, settings, history.get(o.item_type)) for o in options]


def next_history(
    history: Mapping[str, DelayRecord], judgements: Iterable[BoostJudgement], turn: int
) -> dict[str, DelayRecord]:
    """The delay records for next turn. Items no longer offered are dropped."""
    out: dict[str, DelayRecord] = {}
    for j in judgements:
        old = history.get(j.item_type)
        if j.gave_up or (old is not None and old.gave_up):
            out[j.item_type] = DelayRecord(
                old.since_turn if old else turn, old.first_eta if old else 0, True
            )
        elif j.decision is Decision.DELAY:
            out[j.item_type] = old or DelayRecord(turn, j.reach.turns or 1)
    return out


def nudges(judgements: Iterable[BoostJudgement]) -> tuple[Nudge, ...]:
    """The nudge decisions, biggest saving first, as suggestions a policy could take up."""
    found = [
        Nudge(j.item_type, j.name, j.reach.nudge or "", j.reach.policy or "", j.value)
        for j in judgements
        if j.decision is Decision.NUDGE
    ]
    return tuple(sorted(found, key=lambda n: (-n.value, n.item_type)))
