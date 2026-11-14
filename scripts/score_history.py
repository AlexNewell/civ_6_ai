"""The score history of successive live runs of one scenario: add a run, or print the trend.

    score_history.py add "Run 10" runs/STD_T1_01_A.jsonl [runs/STD_T1_01_B.jsonl ...]
    score_history.py report

`add` reads one run's turn logs (several, if the run took more than one session), builds its row
and writes it into docs/score-history.csv, replacing a row of the same name. The columns and the
normalisation are explained in src/civ6_ai/metrics/history.py. Nothing here touches the game.
"""

import argparse
import sys
from pathlib import Path

from civ6_ai.metrics.history import (
    NOISE_FRACTION,
    add_row,
    format_history,
    merge_logs,
    read_history,
    row_from_records,
    write_history,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HISTORY = ROOT / "docs" / "score-history.csv"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="add or replace one run's row from its turn logs")
    add.add_argument("run", help='the name in the history, for example "Run 10"')
    add.add_argument("logs", type=Path, nargs="+", help="the run's turn logs, oldest first")
    add.add_argument("--scenario", help="defaults to the log file name's scenario prefix")
    add.add_argument("--date", default="", help="the run's date, YYYY-MM-DD")
    report = commands.add_parser("report", help="print the history and its trends")
    report.add_argument(
        "--noise", type=float, default=NOISE_FRACTION, help="noise band, as a share"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "add":
            records = merge_logs(args.logs)
            scenario = args.scenario or args.logs[0].name.rsplit("_", 1)[0]
            row = row_from_records(args.run, records, scenario, args.date)
            rows = read_history(args.history) if args.history.exists() else []
            write_history(args.history, add_row(rows, row))
            print(f"Added {args.run}: {row['outcome']}, turn {row['final_turn']}")
        else:
            print(format_history(read_history(args.history), args.noise))
    except (ValueError, OSError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
