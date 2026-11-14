"""Progress of successive live runs of one scenario, on a scale that is fair to short runs.

Most runs end in a rival's victory, on turn 50 or turn 330, so the final score alone says little.
Each run becomes one row, with these choices:

- **Score at fixed checkpoint turns** (50, 100, ... 300), blank when the run didn't reach one. Two
  runs are only ever compared at a turn both reached, so length can't flatter or punish a run.
- **Share of the leader**: our score over the highest score among us and the rivals we've met, and
  **rank** (1 + met rivals scoring strictly more). Both ignore how fast scores grow as a game
  goes on, so they hold up across runs. Unmet rivals aren't in the data, so the leader is the best
  *met* one, and the share is an upper bound. Blank where rival scores weren't recorded (old logs).
- **score_per_turn** is the mean score over the turns played. It is the single summary asked for,
  but a long run has more late, high turns, so it is biased by length. **mean_share** (the mean of
  our share of the leader over the run) has no such bias and is the better single number.
- **Outcome**: won, lost (with winner and victory type where the log names them), unfinished (the
  run stopped for another reason) or unknown (the game ended and nothing says who won).

One run is one sample, so differences are only called better or worse when they are larger than
`NOISE_FRACTION` of the score (or `NOISE_POINTS`, whichever is greater). That band is a judgement,
not a measurement: Runs 8 and 9 differ by up to 9% on checkpoints with only small code changes
between them. Tune it with `--noise` once there are more runs.

Nothing here reads the game or the network.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from civ6_ai.metrics.our_runs import GAME_OVER_REPORT, VICTORY_TYPE, _read_turn_log

CHECKPOINTS = (50, 100, 150, 200, 250, 300)
CHECKPOINT_WINDOW = 2  # A checkpoint may use a record up to this many turns before it.
NOISE_FRACTION = 0.05
NOISE_POINTS = 10
COLUMNS = (
    "run",
    "scenario",
    "source",
    "date",
    "outcome",
    "winner",
    "victory_type",
    "final_turn",
    "last_score_turn",
    "last_score",
    "score_per_turn",
    "mean_share",
    *(f"score_{c}" for c in CHECKPOINTS),
    *(f"share_{c}" for c in CHECKPOINTS),
    *(f"rank_{c}" for c in CHECKPOINTS),
    "notes",
)
WINNER = re.compile(r"DEFEAT\W+(.+?) of (.+?) won an?\b")


@dataclass(frozen=True)
class Point:
    turn: int
    score: int
    rivals: tuple[int, ...] = ()  # The met rivals' scores. Empty when not recorded.

    @property
    def share(self) -> float | None:
        return self.score / max(self.score, *self.rivals) if self.rivals and self.score else None

    @property
    def rank(self) -> int | None:
        return 1 + sum(r > self.score for r in self.rivals) if self.rivals else None


def merge_logs(paths: list[Path]) -> list[dict]:
    """Join the session logs of one run by turn. A later log wins a turn that two both hold."""
    by_turn: dict[int, dict] = {}
    for path in paths:
        for record in _read_turn_log(path):
            by_turn[record["turn"]] = record
    return [by_turn[turn] for turn in sorted(by_turn)]


def points(records: list[dict]) -> list[Point]:
    return [
        Point(r["turn"], r["score"], tuple((r.get("rival_scores") or {}).values()))
        for r in records
        if isinstance(r.get("score"), int)
    ]


def at_checkpoint(series: list[Point], checkpoint: int, final_turn: int) -> Point | None:
    """The newest point on the checkpoint turn, or up to CHECKPOINT_WINDOW turns before it.

    The window only covers a turn with no record. A run that ended before the checkpoint never
    reached it, so it gets none."""
    if final_turn < checkpoint:
        return None
    near = [p for p in series if checkpoint - CHECKPOINT_WINDOW <= p.turn <= checkpoint]
    return max(near, key=lambda p: p.turn) if near else None


def _mean_over_turns(series: list[Point], value) -> float | None:
    """Mean of `value` across the span of the series (trapezoids between recorded turns)."""
    pairs = [(p.turn, v) for p in series if (v := value(p)) is not None]
    if not pairs:
        return None
    if len(pairs) == 1:
        return pairs[0][1]
    area = sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(pairs, pairs[1:], strict=False))
    return area / (pairs[-1][0] - pairs[0][0])


def outcome_of(last: dict) -> tuple[str, str, str]:
    """Return (outcome, winner, victory type) from the last turn's recorded game report."""
    detail = last.get("detail", "")
    match = GAME_OVER_REPORT.search(detail)
    if not match:
        reason = last.get("stop_reason")
        return ("unknown" if reason == "game_over" else "unfinished"), "", ""
    kind = VICTORY_TYPE.search(detail, match.end())
    victory = kind[1] if kind else ""
    if match[1] == "VICTORY":
        return "won", "", victory
    who = WINNER.search(detail)
    return "lost", (who[2] if who else ""), victory


def _round(value: float | None, places: int) -> float | None:
    return None if value is None else round(value, places)


def row_from_records(run: str, records: list[dict], scenario: str, date: str = "") -> dict:
    series = points(records)
    outcome, winner, victory = outcome_of(records[-1])
    row: dict = {
        "run": run,
        "scenario": scenario,
        "source": "log",
        "date": date,
        "outcome": outcome,
        "winner": winner,
        "victory_type": victory,
        "final_turn": records[-1]["turn"],
    }
    if series:
        row["last_score_turn"], row["last_score"] = series[-1].turn, series[-1].score
        row["score_per_turn"] = _round(_mean_over_turns(series, lambda p: p.score), 1)
        row["mean_share"] = _round(_mean_over_turns(series, lambda p: p.share), 3)
    for checkpoint in CHECKPOINTS:
        if point := at_checkpoint(series, checkpoint, records[-1]["turn"]):
            row[f"score_{checkpoint}"] = point.score
            row[f"share_{checkpoint}"] = _round(point.share, 3)
            row[f"rank_{checkpoint}"] = point.rank
    return row


def read_history(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write_history(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, COLUMNS, restval="", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def add_row(rows: list[dict], row: dict) -> list[dict]:
    """Return the rows with this one added. A row of the same run is replaced in place."""
    if any(r["run"] == row["run"] for r in rows):
        return [row if r["run"] == row["run"] else r for r in rows]
    return [*rows, row]


def _number(row: dict, column: str) -> float | None:
    value = row.get(column, "")
    return float(value) if value not in ("", None) else None


def judge(value: float, reference: float, noise_fraction: float = NOISE_FRACTION) -> str:
    """'better', 'worse' or 'flat' (within noise) for a score against a reference score."""
    band = max(noise_fraction * abs(reference), NOISE_POINTS)
    if abs(value - reference) <= band:
        return "flat"
    return "better" if value > reference else "worse"


def trends(rows: list[dict], noise_fraction: float = NOISE_FRACTION) -> list[dict]:
    """For each run and checkpoint it reached: its verdict against the previous run that reached
    the checkpoint, and against the best earlier run. None where there is nothing to compare."""
    result = []
    for index, row in enumerate(rows):
        verdicts: dict[int, tuple[str | None, str | None]] = {}
        for checkpoint in CHECKPOINTS:
            value = _number(row, f"score_{checkpoint}")
            if value is None:
                continue
            earlier = [
                s for r in rows[:index] if (s := _number(r, f"score_{checkpoint}")) is not None
            ]
            if not earlier:
                verdicts[checkpoint] = (None, None)
                continue
            verdicts[checkpoint] = (
                judge(value, earlier[-1], noise_fraction),
                judge(value, max(earlier), noise_fraction),
            )
        result.append({"run": row["run"], "verdicts": verdicts})
    return result


def _cell(row: dict, column: str, width: int, fmt: str = "{:.0f}") -> str:
    value = _number(row, column)
    return (fmt.format(value) if value is not None else "-").rjust(width)


def format_history(rows: list[dict], noise_fraction: float = NOISE_FRACTION) -> str:
    """A plain-text table, then each run's trend against the run before and the best before."""
    if not rows:
        return "No runs in the history."
    lines = [
        "Score at each checkpoint turn ('-' = not reached or not recorded)",
        "",
        f"{'run':<10}{'outcome':<12}{'end':>5}"
        + "".join(f"{c:>7}" for c in CHECKPOINTS)
        + f"{'sh@200':>8}{'rk@200':>8}{'mean sh':>9}",
    ]
    for row in rows:
        lines.append(
            f"{row['run']:<10}{row['outcome']:<12}{_cell(row, 'final_turn', 5)}"
            + "".join(_cell(row, f"score_{c}", 7) for c in CHECKPOINTS)
            + _cell(row, "share_200", 8, "{:.2f}")
            + _cell(row, "rank_200", 8)
            + _cell(row, "mean_share", 9, "{:.2f}")
        )
    lines += [
        "",
        f"Trend, band {noise_fraction:.0%} or {NOISE_POINTS} points. Each cell is the verdict "
        "against the previous run that reached the turn / against the best earlier run:",
        "b = better, w = worse, = = flat (within noise), . = nothing to compare.",
        "",
        f"{'run':<10}" + "".join(f"{c:>7}" for c in CHECKPOINTS),
    ]
    mark = {"better": "b", "worse": "w", "flat": "=", None: "."}
    for entry in trends(rows, noise_fraction):
        cells = []
        for c in CHECKPOINTS:
            if c in entry["verdicts"]:
                previous, best = entry["verdicts"][c]
                cells.append(f"{mark[previous]}/{mark[best]}")
            else:
                cells.append("-")
        lines.append(f"{entry['run']:<10}" + "".join(f"{cell:>7}" for cell in cells))
    lines += [
        "",
        "One run each is one sample: read '=' as no evidence of change, and a single 'b' or 'w'",
        "as a lead to confirm in the next run, not a result.",
    ]
    return "\n".join(lines)
