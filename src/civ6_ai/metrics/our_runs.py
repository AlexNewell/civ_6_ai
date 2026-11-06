"""Read our own turn logs and spend ledger into `RunData`.

A turn log line holds what `TurnRecord` writes today. It may also hold any `Snapshot` field, and
`decisions`, `failed_decisions`, `kills`, `losses`, `rank`, `players` and `score_vs_winner`, which
are read as soon as the orchestrator starts writing them. Counts are only summed when every turn
has one, because a partial sum would pass for a low score.
"""

import json
import re
from dataclasses import fields, replace
from decimal import Decimal
from pathlib import Path

from civ6_ai.metrics.run_data import RunData, Snapshot, Standing

SOURCE = "ours"
NOT_RECORDED = "the turn log doesn't hold it for every turn it needs"
NO_STOP_REASON = "the log ends without one, and a turn limit and a crash look the same"
NO_OUTCOME = "the log's last result doesn't show a win or a defeat"
NO_LEDGER = "no spend ledger was given"
NOT_IN_LEDGER = "the spend ledger has no calls for game {game_id}"
# civ_mcp's end_turn writes "GAME OVER - DEFEAT. <leader> of <civ> won a <Type> victory. ..." or
# "GAME OVER - VICTORY! You won a <Type> victory! ...", and one path puts "Turn N -> M" on a line
# before it, so these are searched for, not matched at the start.
GAME_OVER_REPORT = re.compile(r"GAME OVER\W+(VICTORY|DEFEAT)\b")
VICTORY_TYPE = re.compile(r"\bwon an? ([^.!\n]+?) victory\b")
SNAPSHOT_FIELDS = tuple(field.name for field in fields(Snapshot) if field.name != "turn")
RECORDED_LATER = (
    *SNAPSHOT_FIELDS,
    "decisions",
    "failed_decisions",
    "kills",
    "losses",
    "rank",
    "players",
    "score_vs_winner",
)


def _complete_lines(path: Path) -> list[str]:
    """Return the file's finished lines. Both files can end in a line torn by a crash."""
    raw = path.read_bytes()
    return raw[: raw.rfind(b"\n") + 1].decode("utf-8", errors="replace").splitlines()


def _read_turn_log(path: Path) -> list[dict]:
    records = []
    for number, line in enumerate(_complete_lines(path), start=1):
        try:
            record = json.loads(line)
            len(record["handler_errors"]), record["turn"], record["total_seconds"]
        except (ValueError, KeyError, TypeError) as error:
            raise ValueError(f"Unreadable line {number} in turn log {path.name}") from error
        records.append(record)
    if not records:
        raise ValueError(f"Turn log {path.name} has no finished turns")
    return records


def _sum_every_turn(records: list[dict], key: str) -> int | None:
    values = [record.get(key) for record in records]
    return None if None in values else sum(values)


def _outcome(last: dict) -> tuple[bool | None, str | None]:
    """Read the result from the last turn's detail, which holds the first 300 characters of the
    report. The result is at the start of the report, so only the victory type could be cut off."""
    detail = last.get("detail", "")
    match = GAME_OVER_REPORT.search(detail)
    if not match:
        return None, None
    kind = VICTORY_TYPE.search(detail, match.end())
    return match[1] == "VICTORY", kind[1] if kind else None


def _standing(last: dict) -> Standing | None:
    known = [last.get(key) for key in ("rank", "players", "score_vs_winner")]
    return None if None in known else Standing(*known)


def _snapshot(record: dict) -> Snapshot:
    return Snapshot(record["turn"], **{name: record.get(name) for name in SNAPSHOT_FIELDS})


def load_turn_log(path: Path) -> RunData:
    """Convert a JSONL turn log written by `TurnLog`."""
    records = _read_turn_log(path)
    last = records[-1]
    won, victory_type = _outcome(last)
    return RunData(
        run_id=path.stem,
        source=SOURCE,
        turns_played=len(records),
        total_seconds=sum(record["total_seconds"] for record in records),
        stop_reason=last.get("stop_reason"),
        won=won,
        victory_type=victory_type,
        standing=_standing(last),
        snapshots=tuple(_snapshot(record) for record in records),
        handler_errors=sum(len(record["handler_errors"]) for record in records),
        decisions=_sum_every_turn(records, "decisions"),
        failed_decisions=_sum_every_turn(records, "failed_decisions"),
        kills=_sum_every_turn(records, "kills"),
        losses=_sum_every_turn(records, "losses"),
        missing={
            **dict.fromkeys(RECORDED_LATER, NOT_RECORDED),
            "stop_reason": NO_STOP_REASON,
            "won": NO_OUTCOME,
            "victory_type": NO_OUTCOME,
            "cost_usd": NO_LEDGER,
            "cost_unsettled_usd": NO_LEDGER,
        },
    )


def read_spend(path: Path, game_id: str) -> tuple[Decimal, Decimal] | None:
    """Return a game's total cost and the unsettled part of it, or None if it made no calls.

    Calls that never closed count at their worst case, as they do in `Ledger.committed`. This
    doesn't use `Ledger`, because opening one repairs a torn file, and scoring must not write to a
    ledger that a live game may be using.
    """
    opened: dict[str, Decimal] = {}
    settled = Decimal(0)
    found = False
    for number, line in enumerate(_complete_lines(path), start=1):
        try:
            event = json.loads(line)
            if event["event"] == "open":
                if event["game_id"] == game_id:
                    found = True
                    opened[event["call_id"]] = Decimal(event["worst_case_usd"])
            elif event["call_id"] in opened:
                del opened[event["call_id"]]
                settled += Decimal(event["cost_usd"])
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            raise ValueError(f"Unreadable line {number} in spend ledger {path.name}") from error
    if not found:
        return None
    unsettled = sum(opened.values(), Decimal(0))
    return settled + unsettled, unsettled


def add_spend(run: RunData, path: Path, game_id: str) -> RunData:
    """Return the run with its cost from the ledger, joined on the ledger's game id."""
    spend = read_spend(path, game_id)
    if spend is None:
        reason = NOT_IN_LEDGER.format(game_id=game_id)
        return replace(
            run, missing={**run.missing, "cost_usd": reason, "cost_unsettled_usd": reason}
        )
    cost, unsettled = spend
    return replace(run, cost_usd=float(cost), cost_unsettled_usd=float(unsettled))
