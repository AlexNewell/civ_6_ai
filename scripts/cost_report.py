"""Print tokens and dollars per game, and per turn played, from the logs in runs/.

A static program: it reads `runs/spend_ledger.jsonl` and the turn logs, prices the tokens from
`src/civ6_ai/budget/pricing.toml`, and prints. It makes no model or network calls and never
connects to the game, and running it again on the same files gives the same answer. How to read the
output, and how sessions are grouped into games, is in docs/cost-tracking.md.

    cost_report.py [--runs runs] [--json] [--history runs/cost_history.csv]
                   [--full-game-turns 200] [--fail-over-budget] [--strict]
"""

import argparse
import json
import sys
from pathlib import Path

from civ6_ai.budget import load_pricing
from civ6_ai.metrics.costs import (
    DEFAULT_FULL_GAME_TURNS,
    build_report,
    format_report,
    load_budget,
    pricing_checked,
    scan,
    update_history,
)

ROOT = Path(__file__).parent.parent


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=ROOT / "runs", help="folder of run logs")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument(
        "--history", type=Path, help="CSV to add each game's row to, kept over time"
    )
    parser.add_argument(
        "--full-game-turns",
        type=int,
        default=DEFAULT_FULL_GAME_TURNS,
        help="game length for the projection (default: %(default)s, an assumption)",
    )
    parser.add_argument("--budget-file", type=Path, help="default: .local/cost-budget.json")
    parser.add_argument(
        "--fail-over-budget", action="store_true", help="exit 1 if any player is over budget"
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 on unreadable lines or unpriced models"
    )
    args = parser.parse_args(argv)
    if args.full_game_turns < 1:
        parser.error("--full-game-turns must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        pricing = load_pricing()
        budget = load_budget(ROOT, args.budget_file)
        games, ledger, problems = scan(args.runs, pricing)
        report = build_report(
            games, ledger, problems, pricing_checked(), args.full_game_turns, budget
        )
        if args.history:
            update_history(args.history, report)
    except (ValueError, OSError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    if args.fail_over_budget and report["over_budget"]:
        return 1
    if args.strict and (problems.count or report["totals"]["unpriced_calls"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
