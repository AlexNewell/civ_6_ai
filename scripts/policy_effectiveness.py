"""Print whether each policy achieved its purpose in a run, from its logs. No game or model calls.

    policy_effectiveness.py [--label NAME] [--json] LOG [LOG ...]

Each LOG is a session turn log in `runs/`. Give every session of one game to get the whole run; the
bot, event and settle logs beside each are read too. Exits 1 if any check fails, so it can gate.
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from civ6_ai.effectiveness import FAIL, format_report, load_run, run_checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path, help="session turn logs of one run")
    parser.add_argument("--label", default=None, help="name for the report (default: first log)")
    parser.add_argument("--json", action="store_true", help="print the checks as JSON")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Game names carry non-ASCII.
    missing = [p for p in args.logs if not p.exists()]
    if missing:
        print(f"No such turn log: {missing[0]}")
        return 2
    run = load_run(args.logs)
    checks = run_checks(run)
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        print(format_report(args.label or args.logs[0].stem, run, checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
