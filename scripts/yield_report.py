"""Report a run's yields over its first 60 turns, and what else could have moved them (issue 400).

A static program: it reads the turn log, bot log and event log of each run and prints. It makes no
game or network calls. A run is given as `runs/<name>` (the turn log's stem or file name, with or
without `.jsonl`), the `_bot.log` and `_events.jsonl` beside it being read when they exist.

Definitions, over turns 1 to 60 (`--last-turn` changes the 60):

- The weighted yield of a city on one turn is science * 1.25 + culture * 1.0 + faith * 0.5 +
  production * 1.0, the weights `ProductionWeights` holds (the same ones `valuation.py` uses to turn
  yields into points). Gold and food are logged but not in this sum.
- A city's yield-turns is that weighted yield summed over the turns it existed.
- Empire yield-turns is the sum of every city's yield-turns.
- Per-city yield-turns, the figure the epic's gate compares, is the empire yield-turns divided by
  the mean number of cities over the turns that logged yields (city-turns / turns). It is not
  divided by the final city count, which would reward a run for founding fewer cities.

Dividing by city count can still favour a run that held back on cities, so the empire total is
printed beside it, and a pair is flagged when the two change in opposite directions.

Several runs are paired in order: baseline, change, baseline, change. With one pair it prints the
two side by side and the % change; with more it adds the mean % change over the pairs.

    yield_report.py runs/<name>
    yield_report.py base=runs/<a> change=runs/<b> [base2=... change2=...] [--last-turn 60]
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from civ6_ai.policies.production import ProductionWeights

LAST_TURN = 60
CHECKPOINTS = (20, 40, 60)
YIELDS = ("food", "production", "gold", "science", "culture", "faith")
# The yields in the sum, named by the weight of `ProductionWeights` that scales each.
SUMMED = ("science", "culture", "faith", "production")
KILLED = re.compile(r"\bYour .*\bwas killed")
BOT_LINE = re.compile(r"^\S+ \S+ civ6_ai\.(?P<logger>\S+) (?P<level>[A-Z]+) (?P<msg>.*)$")


def weights() -> dict[str, float]:
    """The weight of each summed yield, read from `ProductionWeights` so it can't drift."""
    w = ProductionWeights()
    return {kind: float(getattr(w, kind)) for kind in SUMMED}


def weighted(row: dict, w: dict[str, float]) -> float:
    return sum(w[kind] * float(row.get(kind) or 0.0) for kind in SUMMED)


@dataclass
class RunReport:
    label: str
    turns_seen: int = 0
    turns_with_yields: int = 0
    city_turns: int = 0
    empire: float | None = None  # Empire yield-turns, None when no yields were logged.
    per_city: float | None = None
    cities: dict[int, dict] = field(default_factory=dict)  # id -> name, founded, yield_turns
    checkpoints: dict[int, dict[str, float]] = field(default_factory=dict)
    founding_turns: list[int] = field(default_factory=list)
    founding_source: str = "none"  # "settle log", or "city-count rises" when it is absent.
    barb_near_turns: int | None = None
    units_lost: int | None = None  # Falls in combat units between lines: a lower bound.
    builders_lost: int | None = None
    camps_cleared: int | None = None
    cities_lost: int | None = None
    civilians_captured: int | None = None
    recaptures: int | None = None


def resolve(spec: str) -> Path:
    path = Path(spec)
    if path.suffix != ".jsonl":
        path = path.with_name(path.name + ".jsonl")
    return path


def _jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # A torn final line.
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _bot_lines(path: Path) -> dict[str, list[dict]]:
    """The JSON payloads of every `<tag> {...}` bot-log line, by tag."""
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = BOT_LINE.match(line)
        if not m:
            continue
        tag, _, payload = m["msg"].partition(" ")
        try:
            out.setdefault(tag, []).append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return out


def _falls(values: list) -> int | None:
    """The sum of the drops between consecutive counts, None when there are no counts."""
    counts = [v for v in values if isinstance(v, int)]
    if not counts:
        return None
    return sum(max(0, before - after) for before, after in zip(counts, counts[1:], strict=False))


def analyse(label: str, turn_log: Path, last_turn: int = LAST_TURN) -> RunReport:
    """Everything the report prints for one run. Missing fields and files leave None or 0."""
    w = weights()
    report = RunReport(label)
    rows = [r for r in _jsonl(turn_log) if isinstance(r.get("turn"), int)]
    rows = [r for r in rows if r["turn"] <= last_turn]
    by_turn: dict[int, dict] = {}
    for row in rows:  # A resumed log can repeat a turn: the later row wins.
        by_turn[row["turn"]] = row
    report.turns_seen = len(by_turn)
    empire = 0.0
    last_count = 0
    for turn in sorted(by_turn):
        row = by_turn[turn]
        count = row.get("cities")
        if isinstance(count, int):
            if count > last_count:
                report.founding_turns += [turn] * (count - last_count)
            last_count = count
        city_rows = row.get("city_yields")
        if not isinstance(city_rows, list) or not city_rows:
            continue
        report.turns_with_yields += 1
        report.city_turns += len(city_rows)
        for city in city_rows:
            entry = report.cities.setdefault(
                city.get("city_id"),
                {"name": city.get("name", "?"), "founded": turn, "yield_turns": 0.0},
            )
            value = weighted(city, w)
            entry["yield_turns"] += value
            empire += value
    if report.turns_with_yields:
        report.empire = empire
        if report.city_turns:
            report.per_city = empire / (report.city_turns / report.turns_with_yields)
    for mark in CHECKPOINTS:
        if mark > last_turn or not by_turn or max(by_turn) < mark:
            continue  # The run never got that far.
        at = [t for t in by_turn if t <= mark and isinstance(by_turn[t].get("yields"), dict)]
        if at:
            report.checkpoints[mark] = {"turn": max(at), **by_turn[max(at)]["yields"]}
    rises = report.founding_turns  # From the city count: also counts captured or gifted cities.
    settle = turn_log.with_name(f"{turn_log.stem}_settle.jsonl")
    if settle.exists():
        report.founding_source = "settle log"
        report.founding_turns = sorted(
            d["turn"]
            for d in _jsonl(settle)
            if d.get("action") == "found"
            and isinstance(d.get("turn"), int)
            and d["turn"] <= last_turn
            and str(d.get("result", "FOUNDED")).startswith("FOUNDED")
        )
    elif rises:
        report.founding_source = "city-count rises"
    report.cities_lost = _falls([by_turn[t].get("cities") for t in sorted(by_turn)])
    bot = turn_log.with_name(f"{turn_log.stem}_bot.log")
    lines = _bot_lines(bot)
    offense = [d for d in lines.get("military_offense", []) if d.get("turn", 0) <= last_turn]
    if offense:
        offense.sort(key=lambda d: d["turn"])
        report.barb_near_turns = len({d["turn"] for d in offense if (d.get("barb_near") or 0) > 0})
        # The counters reset after each line, so a total is the sum of the lines.
        report.civilians_captured = sum(d.get("civilians_captured") or 0 for d in offense)
        report.recaptures = sum(d.get("recaptures") or 0 for d in offense)
        # A lower bound: a unit trained or bought the same turn hides a loss, and a turn with no
        # combat unit writes no line.
        report.units_lost = _falls([d.get("combat_units") for d in offense])
    # A zero is only believed when the policy that writes the line logged something that run.
    if "military_offense" in lines:
        report.camps_cleared = sum(
            1
            for d in lines.get("military_camp_gone", [])
            if d.get("by") == "us" and (d.get("turn") or 0) <= last_turn
        )
    if "builder_decision" in lines or "builder_lost" in lines:
        report.builders_lost = sum(
            1 for d in lines.get("builder_lost", []) if (d.get("turn") or 0) <= last_turn
        )
    return report


def pct_change(base: float | None, new: float | None) -> float | None:
    if base is None or new is None or base == 0:
        return None
    return (new - base) / abs(base) * 100.0


def _num(value: float | int | None, spec: str = ".0f") -> str:
    return "not measured" if value is None else format(value, spec)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def format_single(report: RunReport, last_turn: int = LAST_TURN) -> list[str]:
    w = weights()
    lines = [
        f"== {report.label} ==",
        f"weights: {', '.join(f'{k} {v:g}' for k, v in w.items())} (ProductionWeights)",
        f"turns read {report.turns_seen}, with yields {report.turns_with_yields} (T1-T{last_turn})",
        f"empire yield-turns: {_num(report.empire)}",
        f"per-city yield-turns (empire / mean cities): {_num(report.per_city)}",
    ]
    if report.cities:
        lines.append("per city (founded turn, yield-turns):")
        for entry in sorted(report.cities.values(), key=lambda e: e["founded"]):
            lines.append(f"  {entry['name']}: T{entry['founded']}, {entry['yield_turns']:.0f}")
    for mark, row in sorted(report.checkpoints.items()):
        parts = " ".join(f"{k} {row[k]:g}" for k in YIELDS if k in row)
        lines.append(f"empire at T{mark} (turn {row['turn']}): {parts}")
    lines += _confounders(report)
    return lines


def _confounders(report: RunReport) -> list[str]:
    return [
        "confounders:",
        f"  city founding turns ({report.founding_source}): {report.founding_turns or 'none'}",
        f"  barbarian-near turns: {_num(report.barb_near_turns)}",
        f"  combat units lost (lower bound): {_num(report.units_lost, spec='.0f')}, "
        f"builders lost: {_num(report.builders_lost)}",
        f"  cities lost (falls in the city count): {_num(report.cities_lost)}, "
        f"camps cleared by us: {_num(report.camps_cleared)}",
        f"  civilians captured: {_num(report.civilians_captured)}, "
        f"recaptures: {_num(report.recaptures)}",
    ]


def format_pairs(reports: list[RunReport], last_turn: int = LAST_TURN) -> list[str]:
    """Side by side for each baseline/change pair, with the mean change over several pairs."""
    lines: list[str] = []
    per_city_changes: list[float] = []
    empire_changes: list[float] = []
    for i in range(0, len(reports), 2):
        a, b = reports[i], reports[i + 1]
        per_city = pct_change(a.per_city, b.per_city)
        total = pct_change(a.empire, b.empire)
        lines.append(f"== pair {i // 2 + 1}: {a.label} -> {b.label} (T1-T{last_turn}) ==")
        lines.append(f"{'':34}{a.label:>14}{b.label:>14}{'change':>10}")

        def row(name: str, x: float | int | None, y: float | int | None) -> None:
            lines.append(f"{name:34}{_num(x):>14}{_num(y):>14}{_pct(pct_change(x, y)):>10}")

        row("per-city yield-turns", a.per_city, b.per_city)
        row("empire yield-turns", a.empire, b.empire)
        row("city-turns", a.city_turns, b.city_turns)
        row("turns with yields", a.turns_with_yields, b.turns_with_yields)
        for mark in CHECKPOINTS:
            for kind in YIELDS:
                x = a.checkpoints.get(mark, {}).get(kind)
                y = b.checkpoints.get(mark, {}).get(kind)
                if x is not None or y is not None:
                    row(f"T{mark} empire {kind}", x, y)
        row("barbarian-near turns", a.barb_near_turns, b.barb_near_turns)
        row("combat units lost (lower bound)", a.units_lost, b.units_lost)
        row("builders lost", a.builders_lost, b.builders_lost)
        row("cities lost (count falls)", a.cities_lost, b.cities_lost)
        row("camps cleared by us", a.camps_cleared, b.camps_cleared)
        row("civilians captured", a.civilians_captured, b.civilians_captured)
        row("recaptures", a.recaptures, b.recaptures)
        lines.append(
            f"city founding turns: {a.label} {a.founding_turns} ({a.founding_source}); "
            f"{b.label} {b.founding_turns} ({b.founding_source})"
        )
        if per_city is not None and total is not None and (per_city > 0) != (total > 0):
            lines.append(
                f"FLAG: per-city ({_pct(per_city)}) and empire ({_pct(total)}) disagree in sign; "
                "the per-city figure may reflect a different number of cities, not better yields"
            )
        if per_city is not None:
            per_city_changes.append(per_city)
        if total is not None:
            empire_changes.append(total)
    if len(reports) > 2:
        pairs = len(reports) // 2
        lines.append(
            f"mean change over {pairs} pairs: per-city "
            f"{_pct(_mean(per_city_changes))} ({len(per_city_changes)} with data), "
            f"empire {_pct(_mean(empire_changes))} ({len(empire_changes)} with data)"
        )
    return lines


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def parse_specs(specs: list[str]) -> list[tuple[str, Path]]:
    out = []
    for spec in specs:
        label, sep, spec_path = spec.partition("=")
        path = resolve(spec_path if sep else spec)
        out.append((label if sep else path.stem.removesuffix("_turns"), path))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("runs", nargs="+", help="runs/<name> or label=runs/<name>")
    parser.add_argument("--last-turn", type=int, default=LAST_TURN)
    args = parser.parse_args(argv)
    specs = parse_specs(args.runs)
    if len(specs) > 1 and len(specs) % 2:
        parser.error("give runs in baseline/change pairs (an even number)")
    for _, path in specs:
        if not path.exists():
            parser.error(f"no turn log at {path}")
    reports = [analyse(label, path, args.last_turn) for label, path in specs]
    if len(reports) == 1:
        lines = format_single(reports[0], args.last_turn)
    else:
        lines = format_pairs(reports, args.last_turn)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
