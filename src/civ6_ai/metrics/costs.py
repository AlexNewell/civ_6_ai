"""Tokens and dollars per game from the spend ledger, divided by the turns the turn logs show.

Nothing here connects to the game or the network, and nothing asks a model. Cost is computed here
from billed token counts and `budget/pricing.toml`, never taken from a model's own figure.

**Which sessions are one game.** `scripts/run_game.py` writes one turn log per session, named
`<save>_<UTC stamp>.jsonl`, and a long game is played over many sessions. Sessions of one save,
taken in stamp order, belong to the same game while each session's first turn is later than the
last turn the game has reached. A session that starts at or before that turn was loaded from an
earlier point, so it begins a new game. The game's id is the stem of its first session. Gaps
between sessions are allowed, because turns played by hand leave no log. The one case this can't
see is a second game that starts, by loading a later save, at a turn after the first game's last.

**Turns.** A game's turns are the valid lines across its sessions, so a game resumed from a
mid-game save counts only the turns played. No turns means unknown: per-turn figures are None.

**Ledger.** `Ledger` writes `runs/spend_ledger.jsonl`. A call belongs to the game whose id, or one
of whose session stems, equals the call's `game_id`. Calls under any other id get a game of their
own with unknown turns, so no spend is dropped. A model missing from the price table is counted
as unpriced and never as $0. Calls that never closed, or may still have been billed, count at the
worst case the ledger recorded, as `Ledger.committed` does, and are shown apart.

Corrupt lines, torn final lines and inconsistent events are collected as problems, and the rest of
the file is still read.
"""

import csv
import json
import os
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path

from civ6_ai.budget import Outcome, Pricing, Usage

SCHEMA_VERSION = 1
LEDGER_NAME = "spend_ledger.jsonl"
BUDGET_PATH = Path(".local") / "cost-budget.json"
DEFAULT_FULL_GAME_TURNS = 200  # An assumption for the projection, set with --full-game-turns.
TREND_GAMES = 5
MAX_PROBLEM_SAMPLES = 20
STAMPED_LOG = re.compile(r"^(?P<save>.+)_(?P<stamp>\d{8}T\d{6}Z)\.jsonl$")
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
ZERO = Decimal(0)
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_read_tokens",
)
HISTORY_COLUMNS = (
    "game_id",
    "save",
    "started",
    "turns",
    "sessions",
    "calls",
    "tokens",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "unsettled_usd",
    "unpriced_calls",
    "cost_per_turn_usd",
    "tokens_per_turn",
)


class Problems:
    """Counts every problem and keeps the first few as samples, so a bad file can't flood memory."""

    def __init__(self) -> None:
        self.count = 0
        self.samples: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.count += 1
        if len(self.samples) < MAX_PROBLEM_SAMPLES:
            self.samples.append(f"{where}: {message}")


@dataclass
class Stats:
    """Calls, tokens and cost for one slice of a game."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal = ZERO  # Settled calls whose model has a price.
    unsettled_usd: Decimal = ZERO  # Worst case of calls that never closed or may be billed.
    unsettled_calls: int = 0
    unpriced_calls: int = 0
    calls_without_turn: int = 0
    cost_mismatches: int = 0  # Settled calls where the ledger's cost isn't what the table gives.

    @property
    def tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def merge(self, other: "Stats") -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass(frozen=True)
class Session:
    stem: str
    save: str
    started: str  # ISO time from the file name.
    turns: int
    first_turn: int | None
    last_turn: int | None


@dataclass
class Game:
    id: str
    save: str | None  # None for a game known only from the ledger.
    started: str | None
    sessions: list[str] = field(default_factory=list)
    turns: int | None = None  # None means unknown, which is never the same as 0.
    first_turn: int | None = None
    last_turn: int | None = None
    slices: dict[tuple[str, str], Stats] = field(default_factory=dict)  # (player, model)

    def total(self) -> Stats:
        total = Stats()
        for stats in self.slices.values():
            total.merge(stats)
        return total

    def by(self, index: int) -> dict[str, Stats]:
        grouped: dict[str, Stats] = defaultdict(Stats)
        for key, stats in self.slices.items():
            grouped[key[index]].merge(stats)
        return dict(sorted(grouped.items()))


def per_turn(value: Decimal | int, turns: int | None) -> Decimal | None:
    """Divide by turns played, or None if the turn count is missing or zero."""
    if not turns or turns <= 0:
        return None
    return Decimal(value) / turns


# Turn logs and games


def _lines(path: Path, problems: Problems):
    """Yield (number, text) for each finished line. A line with no newline was torn by a crash."""
    raw = path.read_bytes()
    complete = raw[: raw.rfind(b"\n") + 1]
    for number, line in enumerate(complete.decode("utf-8", errors="replace").splitlines(), 1):
        if line.strip():
            yield number, line
    torn = raw[len(complete) :]
    if torn.strip():
        problems.add(path.name, "last line has no newline, so it is torn and not counted")


def read_session(path: Path, problems: Problems) -> Session | None:
    match = STAMPED_LOG.match(path.name)
    if not match:
        return None
    turns: list[int] = []
    for number, line in _lines(path, problems):
        try:
            turn = json.loads(line)["turn"]
        except (ValueError, KeyError, TypeError):
            turn = None
        if not isinstance(turn, int) or isinstance(turn, bool):
            problems.add(f"{path.name} line {number}", "not a turn record, not counted")
            continue
        if turns and turn <= turns[-1]:
            problems.add(f"{path.name} line {number}", f"turn {turn} does not follow {turns[-1]}")
        turns.append(turn)
    started = datetime.strptime(match["stamp"], STAMP_FORMAT).replace(tzinfo=UTC)
    return Session(
        path.stem,
        match["save"],
        started.isoformat(),
        len(turns),
        turns[0] if turns else None,
        turns[-1] if turns else None,
    )


def group_sessions(sessions: list[Session]) -> list[Game]:
    """Group sessions into games by the rule in the module docstring."""
    games: list[Game] = []
    current: dict[str, Game] = {}  # The open game of each save.
    for session in sorted(sessions, key=lambda s: (s.save, s.started, s.stem)):
        game = current.get(session.save)
        starts_over = (
            game is not None
            and session.first_turn is not None
            and game.last_turn is not None
            and session.first_turn <= game.last_turn
        )
        if game is None or starts_over:
            game = Game(session.stem, session.save, session.started, turns=0)
            current[session.save] = game
            games.append(game)
        game.sessions.append(session.stem)
        game.turns += session.turns
        if session.first_turn is not None:
            game.first_turn = session.first_turn if game.first_turn is None else game.first_turn
            game.last_turn = session.last_turn
    return sorted(games, key=lambda g: (g.started or "", g.id))


def game_id_for_new_session(runs: Path, save: str, first_turn: int, stem: str) -> str:
    """The id a session that is about to start at `first_turn` will have, for the ledger's
    `game_id`. The session's own log needn't exist yet. Use it, or the cap resets each session."""
    problems = Problems()
    sessions = [s for p in sorted(runs.glob("*.jsonl")) if (s := read_session(p, problems))]
    stamp = STAMPED_LOG.match(f"{stem}.jsonl")
    if stamp is None:
        raise ValueError(f"{stem!r} is not a <save>_<UTC stamp> session name")
    sessions = [s for s in sessions if s.stem != stem]
    started = datetime.strptime(stamp["stamp"], STAMP_FORMAT).replace(tzinfo=UTC).isoformat()
    sessions.append(Session(stem, save, started, 1, first_turn, first_turn))
    for game in group_sessions(sessions):
        if stem in game.sessions:
            return game.id
    raise AssertionError("unreachable: the session was just added")


def game_id_of_log(runs: Path, log: Path) -> str | None:
    """The id of the game a written turn log belongs to, or None if it has no game yet."""
    problems = Problems()
    sessions = [s for p in sorted(runs.glob("*.jsonl")) if (s := read_session(p, problems))]
    for game in group_sessions(sessions):
        if log.stem in game.sessions:
            return game.id
    return None


# The ledger


def _decimal(value) -> Decimal:
    number = Decimal(value)
    if not number.is_finite() or number < 0:
        raise InvalidOperation(str(value))
    return number


def read_ledger(path: Path, pricing: Pricing, problems: Problems) -> dict[tuple, Stats]:
    """Aggregate the ledger by (game_id, player_id, model). Streams it, keeping only open calls."""
    stats: dict[tuple, Stats] = defaultdict(Stats)
    open_calls: dict[str, dict] = {}
    closed: set[str] = set()
    for number, line in _lines(path, problems):
        where = f"{path.name} line {number}"
        try:
            event = json.loads(line)
            kind = event["event"]
            call_id = event["call_id"]
            if kind == "open":
                if call_id in open_calls or call_id in closed:
                    problems.add(where, f"call {call_id} is opened twice, the repeat is ignored")
                    continue
                key = (str(event["game_id"]), str(event["player_id"]), str(event["model"]))
                open_calls[call_id] = {
                    "key": key,
                    "worst": _decimal(event["worst_case_usd"]),
                    "turn": event.get("turn"),
                }
            elif kind == "close":
                opened = open_calls.pop(call_id, None)
                if opened is None:
                    problems.add(where, f"close for call {call_id} with no open, not counted")
                    continue
                closed.add(call_id)
                _apply_close(stats[opened["key"]], opened, event, pricing, where, problems)
            else:
                problems.add(where, f"unknown event {kind!r}, not counted")
        except (ValueError, KeyError, TypeError, ArithmeticError):
            problems.add(where, "unreadable event, not counted")
    for opened in open_calls.values():  # Opened and never closed: the worst case stands.
        entry = stats[opened["key"]]
        entry.calls += 1
        entry.unsettled_calls += 1
        entry.unsettled_usd += opened["worst"]
        entry.calls_without_turn += opened["turn"] is None
    return stats


def _apply_close(
    entry: Stats, opened: dict, event: dict, pricing: Pricing, where: str, problems: Problems
) -> None:
    entry.calls += 1
    entry.calls_without_turn += opened["turn"] is None
    outcome = Outcome(event["outcome"])
    if outcome is Outcome.UNBILLED:
        return
    if outcome is Outcome.BILLING_UNKNOWN or event.get("usage") is None:
        if outcome is Outcome.SETTLED:
            problems.add(where, "settled call has no usage, counted at its worst case")
        entry.unsettled_calls += 1
        entry.unsettled_usd += opened["worst"]
        return
    usage = Usage(**{name: event["usage"][name] for name in _USAGE_FIELDS})
    entry.input_tokens += usage.input_tokens
    entry.output_tokens += usage.output_tokens
    entry.cache_read_tokens += usage.cache_read_tokens
    entry.cache_write_tokens += usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
    model = opened["key"][2]
    if model not in pricing.models:
        entry.unpriced_calls += 1
        return
    cost = pricing.cost(model, usage)
    entry.cost_usd += cost
    if cost != _decimal(event["cost_usd"]):
        entry.cost_mismatches += 1


# The report


def scan(runs: Path, pricing: Pricing) -> tuple[list[Game], dict, Problems]:
    """Read everything in `runs` and return the games, ledger facts and problems."""
    problems = Problems()
    paths = sorted(runs.glob("*.jsonl")) if runs.is_dir() else []
    sessions = [s for p in paths if (s := read_session(p, problems))]
    games = group_sessions(sessions)
    ledger_path = runs / LEDGER_NAME
    ledger = {"present": ledger_path.is_file(), "calls": 0}
    if ledger["present"]:
        by_id = {game.id: game for game in games}
        by_id.update({stem: game for game in games for stem in game.sessions})
        for (game_id, player, model), stats in read_ledger(ledger_path, pricing, problems).items():
            game = by_id.get(game_id)
            if game is None:
                game = by_id[game_id] = Game(game_id, None, None)
                games.append(game)
                problems.add(LEDGER_NAME, f"calls for game id {game_id!r}, which has no turn log")
            game.slices.setdefault((player, model), Stats()).merge(stats)
            ledger["calls"] += stats.calls
        games.sort(key=lambda g: (g.started or "~", g.id))
    return games, ledger, problems


def pricing_checked() -> str:
    """The date the shipped price table was last checked against its source."""
    raw = resources.files("civ6_ai.budget").joinpath("pricing.toml").read_text(encoding="utf-8")
    return str(tomllib.loads(raw)["checked"])


def load_budget(root: Path, path: Path | None = None) -> Decimal | None:
    """Read the private per-player, per-game budget, or None if there is no file.

    The file is `.local/cost-budget.json`, git-ignored: {"per_player_per_game_usd": "<number>"}.
    """
    path = path or root / BUDGET_PATH
    if not path.is_file():
        return None
    try:
        return _decimal(json.loads(path.read_text(encoding="utf-8"))["per_player_per_game_usd"])
    except (ValueError, KeyError, TypeError, ArithmeticError) as error:
        raise ValueError(
            f'{path.name} must be {{"per_player_per_game_usd": "<number>"}}'
        ) from error


def _text(value: Decimal | None, places: int = 6) -> str | None:
    return None if value is None else str(value.quantize(Decimal(1).scaleb(-places)))


def _stats_dict(stats: Stats, turns: int | None, full_turns: int) -> dict:
    spent = stats.cost_usd + stats.unsettled_usd
    cost_per_turn = per_turn(spent, turns)
    return {
        "calls": stats.calls,
        "tokens": stats.tokens,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "cache_read_tokens": stats.cache_read_tokens,
        "cache_write_tokens": stats.cache_write_tokens,
        "cost_usd": _text(stats.cost_usd),
        "unsettled_usd": _text(stats.unsettled_usd),
        "unsettled_calls": stats.unsettled_calls,
        "unpriced_calls": stats.unpriced_calls,
        "calls_without_turn": stats.calls_without_turn,
        "cost_mismatches": stats.cost_mismatches,
        "tokens_per_turn": _text(per_turn(stats.tokens, turns), 2),
        "cost_per_turn_usd": _text(cost_per_turn),
        "projected_cost_usd": _text(None if cost_per_turn is None else cost_per_turn * full_turns),
    }


def build_report(
    games: list[Game],
    ledger: dict,
    problems: Problems,
    pricing_checked: str,
    full_game_turns: int,
    budget: Decimal | None,
) -> dict:
    """Everything the text report and `--json` show. `turns` of None means unknown."""
    rows = []
    total = Stats()
    known_cost, known_turns = ZERO, 0
    for game in games:
        stats = game.total()
        total.merge(stats)
        turns = game.turns or None
        if turns:
            known_cost += stats.cost_usd + stats.unsettled_usd
            known_turns += turns
        row = {
            "game_id": game.id,
            "save": game.save,
            "started": game.started,
            "sessions": len(game.sessions),
            "turns": turns,
            "first_turn": game.first_turn,
            "last_turn": game.last_turn,
            **_stats_dict(stats, turns, full_game_turns),
            "players": {
                name: _stats_dict(s, turns, full_game_turns) for name, s in game.by(0).items()
            },
            "models": {
                name: _stats_dict(s, turns, full_game_turns) for name, s in game.by(1).items()
            },
        }
        if budget is not None:
            row["budget"] = {
                name: _budget_entry(s, budget, turns, full_game_turns)
                for name, s in game.by(0).items()
            }
        rows.append(row)
    zero = [r for r in rows if r["calls"] == 0 and r["turns"]]
    timed = [r for r in rows if r["turns"] and r["started"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "pricing_checked": pricing_checked,
        "full_game_turns": full_game_turns,
        "ledger": ledger,
        "games": rows,
        "totals": {
            **_stats_dict(total, None, full_game_turns),
            "games": len(rows),
            "games_with_known_turns": sum(1 for r in rows if r["turns"]),
            "turns": known_turns,
            "tokens_per_turn_known_games": _text(
                per_turn(sum(r["tokens"] for r in rows if r["turns"]), known_turns), 2
            ),
            "cost_per_turn_known_games_usd": _text(per_turn(known_cost, known_turns)),
        },
        "zero_call_games": [{"game_id": r["game_id"], "turns": r["turns"]} for r in zero],
        "trend": [
            {k: r[k] for k in ("game_id", "turns", "tokens_per_turn", "cost_per_turn_usd")}
            for r in sorted(timed, key=lambda r: r["started"])[-TREND_GAMES:]
        ],
        "problems": {"count": problems.count, "samples": problems.samples},
        "over_budget": any(
            e["status"] == "over" for r in rows for e in r.get("budget", {}).values()
        ),
    }


def _budget_entry(stats: Stats, budget: Decimal, turns: int | None, full_turns: int) -> dict:
    spent = stats.cost_usd + stats.unsettled_usd
    projected = per_turn(spent, turns)
    projected = None if projected is None else projected * full_turns
    if spent > budget:
        status = "over"
    elif stats.unpriced_calls:
        status = "unknown"  # Some spend is unpriced, so being under is not proven.
    else:
        status = "under"
    return {
        "status": status,
        "spent_usd": _text(spent),
        "budget_usd": _text(budget),
        "projected_over": None if projected is None else projected > budget,
    }


# Output


def money(text: str | None) -> str:
    if text is None:
        return "unknown"
    value = Decimal(text)
    return "$0.00" if value == 0 else f"${value:.4f}"


def _sum(row: dict) -> str:
    return str(Decimal(row["cost_usd"]) + Decimal(row["unsettled_usd"]))


def _row_line(name: str, row: dict, width: int) -> str:
    spent = _sum(row)
    cost = money(spent)
    if row["unpriced_calls"]:
        cost += f" +{row['unpriced_calls']} UNPRICED"
    if row["unsettled_calls"]:
        cost += f" (incl. {money(row['unsettled_usd'])} unsettled)"
    turns = row.get("turns")
    return (
        f"{name:<{width}} {('?' if turns is None else turns):>5} {row['calls']:>6} "
        f"{row['tokens']:>10} {cost:<22} {money(row['cost_per_turn_usd']):>10} "
        f"{(row['tokens_per_turn'] or 'unknown'):>12} {money(row['projected_cost_usd']):>10}"
    )


def format_report(report: dict) -> str:
    out = []
    ledger = report["ledger"]
    if ledger["present"]:
        out.append(f"Spend ledger: {LEDGER_NAME}, {ledger['calls']} calls")
    else:
        out.append(
            f"Spend ledger: {LEDGER_NAME} NOT FOUND. $0.00 below means no calls were recorded,"
            " and it is only evidence of zero calls if every call goes through the Governor."
        )
    out.append(
        f"Prices checked {report['pricing_checked']}. Projection assumes a "
        f"{report['full_game_turns']}-turn game. Per-turn figures divide by turns played."
    )
    games = report["games"]
    width = max([len(r["game_id"]) for r in games] + [4])
    header = (
        f"{'Game':<{width}} {'Turns':>5} {'Calls':>6} {'Tokens':>10} {'Cost':<22} "
        f"{'$/turn':>10} {'Tokens/turn':>12} {'Projected':>10}"
    )
    out += ["", header, "-" * len(header)]
    for row in games:
        out.append(_row_line(row["game_id"], row, width))
        if row["calls"]:
            for label, group in (("player", "players"), ("model", "models")):
                for name, sub in row[group].items():
                    out.append(_row_line(f"  {label} {name}", sub | {"turns": row["turns"]}, width))
    if not games:
        out.append("(no games found)")
    totals = report["totals"]
    out += ["", "Running totals"]
    out.append(
        f"  {totals['games']} games ({totals['games_with_known_turns']} with known turns,"
        f" {totals['turns']} turns), {totals['calls']} calls, {totals['tokens']} tokens,"
        f" {money(_sum(totals))}"
    )
    if totals["unpriced_calls"]:
        out.append(f"  UNPRICED: {totals['unpriced_calls']} calls on models with no price")
    out.append(
        f"  Over games with known turns: {money(totals['cost_per_turn_known_games_usd'])}/turn,"
        f" {totals['tokens_per_turn_known_games'] or 'unknown'} tokens/turn"
    )
    zero = report["zero_call_games"]
    out.append(
        f"  Games with zero LLM calls and at least one turn: {len(zero)}"
        + (f" ({', '.join(f'{z["game_id"]}: {z["turns"]} turns' for z in zero)})" if zero else "")
    )
    if report["trend"]:
        out += ["", f"Most recent {len(report['trend'])} games, oldest first"]
        for entry in report["trend"]:
            out.append(
                f"  {entry['game_id']}: {money(entry['cost_per_turn_usd'])}/turn,"
                f" {entry['tokens_per_turn']} tokens/turn over {entry['turns']} turns"
            )
    budget_lines = [
        f"  {r['game_id']} {player}: {e['status']} (spent {money(e['spent_usd'])} of "
        f"{money(e['budget_usd'])}" + (", projected over" if e["projected_over"] else "") + ")"
        for r in games
        for player, e in r.get("budget", {}).items()
    ]
    if budget_lines:
        out += ["", "Budget per player per game", *budget_lines]
    problems = report["problems"]
    if problems["count"]:
        out += ["", f"PROBLEMS ({problems['count']}; first {len(problems['samples'])} shown)"]
        out += [f"  {sample}" for sample in problems["samples"]]
    return "\n".join(out)


# The history file


def update_history(path: Path, report: dict) -> None:
    """Add this scan's games to a CSV, replacing rows with the same game id, keeping the rest.

    The rows of games whose logs were later deleted stay, so the history outlasts `runs/`.
    Rewriting the file with the same scan gives the same file.
    """
    rows: dict[str, dict] = {}
    if path.is_file():
        with path.open(encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            if tuple(reader.fieldnames or ()) != HISTORY_COLUMNS:
                raise ValueError(f"{path.name} is not a cost history file (unexpected columns)")
            rows = {row["game_id"]: row for row in reader}
    for game in report["games"]:
        row = {
            **{k: game.get(k) for k in HISTORY_COLUMNS},
            "cost_usd": _sum(game),
        }
        rows[game["game_id"]] = {k: "" if v is None else v for k, v in row.items()}
    temp = path.with_suffix(".csv.tmp")
    with temp.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, HISTORY_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(rows.values(), key=lambda r: (r["started"], r["game_id"])))
    os.replace(temp, path)
