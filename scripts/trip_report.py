"""Judge the movement service's trip estimates from a run's `movement_trip` log lines (#399).

Each move-to-goal trip logs the turns it was estimated to take and the turns it took. This prints,
per unit kind, how many trips arrived, the share whose estimate was within one turn of what
happened, the mean absolute error, and the mean signed error (actual minus estimated: positive
means the unit was slower than estimated). Trips that did not arrive (abandoned, lost, refused)
are counted apart and left out of the error figures.

    trip_report.py runs/<save>_<stamp>_bot.log   (or the turn log .jsonl beside it, or a folder)
"""

import argparse
import json
import sys
from pathlib import Path

MARK = "movement_trip "
WITHIN = 1  # An estimate this many turns out, either way, counts as right (the acceptance bar).


def log_files(path: Path) -> list[Path]:
    """The bot log(s) for a run: the file itself, the `_bot.log` beside a turn log, or every
    `_bot.log` in a folder."""
    if path.is_dir():
        return sorted(path.glob("*_bot.log"))
    if path.suffix == ".jsonl":
        return [path.with_name(f"{path.stem}_bot.log")]
    return [path]


def read_trips(files: list[Path]) -> list[dict]:
    trips = []
    for file in files:
        if not file.exists():
            continue
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            _, found, rest = line.partition(MARK)
            if not found:
                continue
            try:
                trips.append(json.loads(rest))
            except json.JSONDecodeError:
                continue
    return trips


def summarise(trips: list[dict]) -> dict[str, dict]:
    """Per kind: trips, arrived, graded (arrived with an estimate), within, mean_abs, mean_signed,
    refused (trips ended by a refused order), other (abandoned, gone, timed out), and by basis."""
    kinds: dict[str, dict] = {}
    for trip in trips:
        row = kinds.setdefault(
            trip["kind"],
            {"trips": 0, "arrived": 0, "graded": 0, "within": 0, "abs": 0.0, "signed": 0.0,
             "refused": 0, "other": 0, "hex_basis": 0},
        )  # fmt: skip
        row["trips"] += 1
        if trip.get("outcome") == "arrived":
            row["arrived"] += 1
            if trip.get("estimated") is not None:
                error = trip["actual"] - trip["estimated"]
                row["graded"] += 1
                row["within"] += abs(error) <= WITHIN
                row["abs"] += abs(error)
                row["signed"] += error
                row["hex_basis"] += trip.get("basis") == "hex"
        elif trip.get("outcome") == "refused":
            row["refused"] += 1
        else:
            row["other"] += 1
    for row in kinds.values():
        n = row["graded"]
        row["share_within"] = row["within"] / n if n else None
        row["mean_abs"] = row["abs"] / n if n else None
        row["mean_signed"] = row["signed"] / n if n else None
    return kinds


def format_report(kinds: dict[str, dict]) -> str:
    if not kinds:
        return "No movement_trip lines found."
    lines = []
    for kind in sorted(kinds):
        row = kinds[kind]
        if row["graded"]:
            grade = (
                f"within +-{WITHIN} turn {row['share_within']:.0%} "
                f"({row['within']}/{row['graded']})"
                f", mean abs error {row['mean_abs']:.2f}, mean signed {row['mean_signed']:+.2f}"
                f", hex-basis {row['hex_basis']}"
            )
        else:
            grade = "no arrived trip with an estimate"
        lines.append(
            f"{kind}: {row['trips']} trips, {row['arrived']} arrived, {row['refused']} refused, "
            f"{row['other']} abandoned or lost; {grade}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="a bot log, the turn log beside it, or a folder")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    files = log_files(args.run)
    if not any(f.exists() for f in files):
        print(f"No bot log found for {args.run}")
        return 1
    print(format_report(summarise(read_trips(files))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
