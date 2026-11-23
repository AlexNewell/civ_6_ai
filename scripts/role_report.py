"""Report what each combat unit was for, from a run's `military_roles` lines (issue 416).

A static program: it reads `<run>_bot.log` and prints. It makes no game or network calls. A run is
given as `runs/<name>` (the turn log's stem or file name, with or without `.jsonl`).

Per run, over the turns kept (`--from N`, `--to N`), one line per turn (the last if a turn logged
two):

- unit-turns by role, and the share that were spare;
- spare unit-turns while the camp plan was short (available below needed), and while any city was
  short, by the threat map;
- combat units per city (unit-turns over city-turns);
- for spare unit-turns with an unmet need, the walking turns to the nearest one (a short city, or
  the camp muster), where the line carries them.

A figure whose source is absent prints "not measured", never 0: a run from before the line, one
played without the threat state, or one with no terrain store has no such source.

    role_report.py runs/<name> [runs/<name2> ...] [--from 30] [--to 60]
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

BOT_LINE = re.compile(r"^\S+ \S+ civ6_ai\.(?P<logger>\S+) (?P<level>[A-Z]+) (?P<msg>.*)$")
NOT_MEASURED = "not measured"


def bot_log(spec: str) -> Path:
    path = Path(spec)
    stem = path.name.removesuffix(".jsonl").removesuffix("_bot.log")
    return path.with_name(f"{stem}_bot.log")


def read_lines(path: Path, first: int | None, last: int | None) -> list[dict]:
    """The `military_roles` payloads in the turn range, one per turn (the last that turn)."""
    by_turn: dict[int, dict] = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = BOT_LINE.match(line)
        if not m or not m["msg"].startswith("military_roles "):
            continue
        try:
            row = json.loads(m["msg"].partition(" ")[2])
        except json.JSONDecodeError:
            continue  # A torn final line.
        turn = row.get("turn")
        if (
            isinstance(turn, int)
            and (first is None or turn >= first)
            and (last is None or turn <= last)
        ):
            by_turn[turn] = row
    return [by_turn[t] for t in sorted(by_turn)]


def plan_short(row: dict) -> bool:
    plan = row.get("plan")
    return plan is not None and plan["have"] < plan["need"]


def summarise(rows: list[dict]) -> dict:
    """The counts. A value is None when its source is absent from every line."""
    units = [(row, u) for row in rows for u in row.get("units", [])]
    spare = [(row, u) for row, u in units if u.get("role") == "spare"]
    city_turns = sum(row.get("cities", 0) for row in rows)
    has_plan = any("plan" in row for row in rows)
    has_short = any("short" in row for row in rows)
    need = [(row, u) for row, u in spare if row.get("short") or plan_short(row)]
    walks = [u["nw"] for _, u in need if "nw" in u]
    return {
        "turns": len(rows),
        "unit_turns": len(units),
        "roles": Counter(u.get("role", "other") for _, u in units),
        "spare": len(spare),
        "spare_plan_short": sum(1 for r, _ in spare if plan_short(r)) if has_plan else None,
        "spare_city_short": sum(1 for r, _ in spare if r.get("short")) if has_short else None,
        "per_city": len(units) / city_turns if city_turns else None,
        "garrison_calm": Counter(
            u["city"].get("rd", "unknown")
            for _, u in units
            if u.get("role") == "garrison" and u.get("city") and not u["city"].get("short")
        ),
        "need": len(need),
        "walks": Counter(walks),
        "any_walk": any("nw" in u for _, u in spare),
    }


def show(value: object) -> str:
    return NOT_MEASURED if value is None else str(value)


def report(spec: str, first: int | None, last: int | None) -> None:
    path = bot_log(spec)
    rows = read_lines(path, first, last)
    print(f"{path.name.removesuffix('_bot.log')}  turns {first or 'first'}-{last or 'last'}")
    if not rows:
        print(f"  no military_roles lines ({NOT_MEASURED})")
        return
    s = summarise(rows)
    total = s["unit_turns"]
    print(f"  turns logged {s['turns']}, unit-turns {total}")
    roles = ", ".join(f"{r} {n}" for r, n in sorted(s["roles"].items(), key=lambda kv: -kv[1]))
    print(f"  by role: {roles or NOT_MEASURED}")
    share = f"{100 * s['spare'] / total:.0f}% ({s['spare']})" if total else None
    print(f"  spare share: {show(share)}")
    print(f"  spare while the camp plan is short: {show(s['spare_plan_short'])}")
    print(f"  spare while any city is short: {show(s['spare_city_short'])}")
    per_city = None if s["per_city"] is None else f"{s['per_city']:.2f}"
    print(f"  units per city: {show(per_city)}")
    if s["garrison_calm"]:
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(s["garrison_calm"].items()))
        print(f"  garrison unit-turns in a city not short, by map reading: {kinds}")
    if not s["need"]:
        print("  walk to the nearest unmet need: no spare unit-turn had an unmet need")
    elif not s["any_walk"]:
        print(f"  walk to the nearest unmet need: {NOT_MEASURED} ({s['need']} unit-turns)")
    else:
        walks = ", ".join(f"{k} turns x{n}" for k, n in sorted(s["walks"].items()))
        missing = s["need"] - sum(s["walks"].values())
        print(f"  walk to the nearest unmet need: {walks}; {NOT_MEASURED} for {missing}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", help="runs/<name>, one or more")
    parser.add_argument("--from", dest="first", type=int, default=None)
    parser.add_argument("--to", dest="last", type=int, default=None)
    args = parser.parse_args(argv)
    for spec in args.runs:
        report(spec, args.first, args.last)
    return 0


if __name__ == "__main__":
    sys.exit(main())
