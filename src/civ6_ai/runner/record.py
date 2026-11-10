"""The record of one game attempt, written as JSON when it ends."""

import json
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from civ6_ai.metrics.speed import summarise
from civ6_ai.orchestrator import StopReason, TurnRecord

# civ6-mcp's game-over reports: "GAME OVER — VICTORY! You won a science victory!" and
# "GAME OVER — DEFEAT. <leader> of <civ> won a domination victory."
_VICTORY = re.compile(r"VICTORY!\s*You won an? (?P<kind>.+?) victory", re.IGNORECASE)
_DEFEAT = re.compile(r"DEFEAT\..*?won an? (?P<kind>.+?) victory", re.IGNORECASE)


@dataclass(frozen=True)
class PlayOutcome:
    """What the play step reports when the turn loop stops."""

    stop_reason: str  # A `StopReason` value.
    turn_records: Sequence[TurnRecord] = ()
    log_paths: Sequence[str] = ()
    detail: str = ""  # The last turn's report text.


@dataclass(frozen=True)
class Result:
    kind: str  # victory, defeat, game_over, turn_limit or failed.
    victory_type: str | None = None
    reason: str = ""


def classify(outcome: PlayOutcome) -> Result:
    """Turn the loop's stop reason into a result. Blocked, hung and aborted games are failures."""
    reason = outcome.stop_reason
    if reason == StopReason.TURN_LIMIT:
        return Result("turn_limit")
    if reason == StopReason.GAME_OVER:
        if match := _VICTORY.search(outcome.detail):
            return Result("victory", match["kind"].lower())
        if match := _DEFEAT.search(outcome.detail):
            return Result("defeat", match["kind"].lower())
        # A defeat found by the defeat check has no report text to read.
        return Result("game_over", reason="game over, victory type unknown")
    return Result("failed", reason=f"{reason}: {outcome.detail}".strip(": ")[:300])


@dataclass
class RunRecord:
    item_id: str
    save: str
    attempt: int
    started_at: str
    ended_at: str
    result: Result
    turns_played: int = 0
    first_turn: int | None = None
    last_turn: int | None = None
    timing: dict | None = None  # The speed summary, plus the slowest turn.
    log_paths: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result.kind != "failed"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"

    def write(self, path: Path) -> None:
        """Write atomically, so a crash never leaves half a record."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        os.replace(tmp, path)


def timing_summary(records: Sequence[TurnRecord]) -> dict | None:
    """The PR 100 speed figures for these turns, as plain data."""
    speed = summarise((r.total_seconds, r.end_turn_seconds) for r in records)
    if speed is None:
        return None
    data = asdict(speed)
    data["over_dev_bar"] = speed.over_dev_bar
    data["over_goal"] = speed.over_goal
    data["slowest_turn_seconds"] = max(r.total_seconds for r in records)
    data["total_seconds"] = sum(r.total_seconds for r in records)
    return data
