"""The one input format for scoring, which each data source is converted into.

None always means "this source doesn't have it", and 0 always means a measured zero. Scoring relies
on that difference, so a converter must never fill a gap with 0.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Snapshot:
    """The scored player at the end of one turn."""

    turn: int
    score: int | None = None
    cities: int | None = None
    science: float | None = None  # Science per turn.
    culture: float | None = None  # Culture per turn.
    gold_per_turn: float | None = None
    faith_per_turn: float | None = None
    food_per_turn: float | None = None
    production_per_turn: float | None = None
    techs_completed: int | None = None


@dataclass(frozen=True)
class Standing:
    """Where the player finished, by the rule in docs/civbench-baselines.md."""

    rank: int
    players: int
    score_vs_winner: float


@dataclass(frozen=True)
class RunData:
    run_id: str
    source: str
    turns_played: int | None = None
    total_seconds: float | None = None
    stop_reason: str | None = None
    won: bool | None = None
    victory_type: str | None = None
    standing: Standing | None = None
    snapshots: tuple[Snapshot, ...] = ()  # In turn order, and the last one is the final turn.
    handler_errors: int | None = None
    decisions: int | None = None  # Calls on the game: queries, actions and ending turns.
    failed_decisions: int | None = None
    kills: int | None = None
    losses: int | None = None
    cost_usd: float | None = None  # Includes the worst case of calls that never closed.
    cost_unsettled_usd: float | None = None  # How much of cost_usd is that worst case.
    info: dict[str, str | bool] = field(default_factory=dict)  # Describes the run. Not a score.
    missing: dict[str, str] = field(default_factory=dict)  # Metric name to why it can't be known.
