"""Turn a `RunData` into a machine-readable score.

A metric the run can't support is left out of `metrics` and listed in `absent` with the reason, so
a later average can't mistake it for zero. Every name in `METRICS` lands in exactly one of the two.

`milestones` holds the columns of the baseline page at its turns, for direct comparison. A turn
with no data has no entry. Runs only compare at the same game speed, which `run` shows if known.
"""

from civ6_ai.metrics.run_data import RunData, Snapshot

SCHEMA_VERSION = 1
MILESTONE_TURNS = (50, 100, 150, 200, 250)
MILESTONE_FIELDS = ("score", "cities", "science", "culture", "techs_completed")
SNAPSHOT_FIELDS = (
    "score",
    "cities",
    "science",
    "culture",
    "gold_per_turn",
    "faith_per_turn",
    "food_per_turn",
    "production_per_turn",
    "techs_completed",
)
RUN_FIELDS = (
    "turns_played",
    "total_seconds",
    "stop_reason",
    "won",
    "victory_type",
    "handler_errors",
    "decisions",
    "failed_decisions",
    "kills",
    "losses",
    "cost_usd",
    "cost_unsettled_usd",
)
STANDING_FIELDS = ("rank", "players", "score_vs_winner")
RATIOS = {
    "seconds_per_turn": ("total_seconds", "turns_played"),
    "handler_errors_per_turn": ("handler_errors", "turns_played"),
    "decisions_per_turn": ("decisions", "turns_played"),
    "errors_per_decision": ("failed_decisions", "decisions"),
    "cost_per_decision_usd": ("cost_usd", "decisions"),
    "kill_ratio": ("kills", "losses"),
}
TECH_PACE = "techs_per_turn"
METRICS = (*RUN_FIELDS, *STANDING_FIELDS, *SNAPSHOT_FIELDS, *RATIOS, TECH_PACE)

UNKNOWN_REASON = "this source doesn't provide it"
TECH_PACE_REASON = "needs tech counts from two different turns"


def score(run: RunData) -> dict:
    """Score one run. The result holds only JSON types."""
    values = _values(run)
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {"run_id": run.run_id, "source": run.source, **run.info},
        "metrics": {name: value for name, value in values.items() if value is not None},
        "absent": {
            name: _absent_reason(name, values, run)
            for name, value in values.items()
            if value is None
        },
        "milestones": _milestones(run.snapshots),
    }


def _values(run: RunData) -> dict:
    final = run.snapshots[-1] if run.snapshots else None
    values = {name: getattr(run, name) for name in RUN_FIELDS}
    values |= {name: getattr(run.standing, name, None) for name in STANDING_FIELDS}
    values |= {name: getattr(final, name, None) for name in SNAPSHOT_FIELDS}
    values |= {name: _ratio(values[top], values[bottom]) for name, (top, bottom) in RATIOS.items()}
    values[TECH_PACE] = _tech_pace(run.snapshots)
    return values


def _ratio(top: float | None, bottom: float | None) -> float | None:
    return None if top is None or not bottom else top / bottom


def _tech_pace(snapshots: tuple[Snapshot, ...]) -> float | None:
    """Techs gained per turn over the turns with data, so a mid-game start or a gap is fair."""
    known = [snapshot for snapshot in snapshots if snapshot.techs_completed is not None]
    if len(known) < 2 or known[0].turn == known[-1].turn:
        return None
    gained = known[-1].techs_completed - known[0].techs_completed
    return gained / (known[-1].turn - known[0].turn)


def _absent_reason(name: str, values: dict, run: RunData) -> str:
    if name in run.missing:
        return run.missing[name]
    for source in RATIOS.get(name, ()):
        if values[source] is None:
            return f"needs {source}: {_absent_reason(source, values, run)}"
        if values[source] == 0:
            return f"{source} is 0, so the ratio is undefined"
    return TECH_PACE_REASON if name == TECH_PACE else UNKNOWN_REASON


def _milestones(snapshots: tuple[Snapshot, ...]) -> dict[str, dict]:
    milestones = {}
    for snapshot in snapshots:
        if snapshot.turn in MILESTONE_TURNS:
            known = {name: getattr(snapshot, name) for name in MILESTONE_FIELDS}
            known = {name: value for name, value in known.items() if value is not None}
            if known:
                milestones[str(snapshot.turn)] = known
    return milestones
