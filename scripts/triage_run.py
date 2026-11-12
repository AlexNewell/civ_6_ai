"""Summarise a run that stopped early or had handler errors, from the logs in `runs/`.

A static program: it reads the turn log, the bot log and the screenshot folder, and prints the stop,
the handler errors, the end of the bot log, and whether a screenshot is worth reading. It makes no
model or game calls. The rules and the procedure are in docs/triage.md.

    triage_run.py [runs/<save>_<stamp>.jsonl]    (default: the newest turn log)
"""

import argparse
import sys
from pathlib import Path

from civ6_ai.triage import format_report, newest_turn_log, triage

RUNS = Path(__file__).parent.parent / "runs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?", type=Path, help="the turn log (default: the newest)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Game names carry non-ASCII.
    log = args.log or newest_turn_log(RUNS)
    if log is None or not log.exists():
        print("No turn log found in runs/")
        return 1
    print(format_report(triage(log)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
