"""Compare two labelled sets of scored runs, with effect sizes, intervals and sample-size warnings.

Each input is a JSON file from scripts/score_run.py. Nothing here connects to the game or the
network. The methods and their limits are in src/civ6_ai/metrics/report.py.

    compare_runs.py --label-a baseline --a a1.json a2.json --label-b tuned --b b1.json b2.json
"""

import argparse
import json
import sys
from pathlib import Path

from civ6_ai.metrics.report import DEFAULT_RESAMPLES, DEFAULT_SEED, compare, format_report


def load_scores(paths: list[Path]) -> list[dict]:
    scores = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path} is not valid JSON: {error}") from error
        if not isinstance(data, dict) or not isinstance(data.get("metrics"), dict):
            raise ValueError(f"{path} is not a score_run.py result (no 'metrics' object)")
        scores.append(data)
    return scores


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label-a", required=True, help="name of the first (reference) set")
    parser.add_argument("--a", type=Path, nargs="+", required=True, help="score files of set A")
    parser.add_argument("--label-b", required=True, help="name of the second set")
    parser.add_argument("--b", type=Path, nargs="+", required=True, help="score files of set B")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap seed")
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--out", type=Path, help="write the report here instead of printing it")
    args = parser.parse_args(argv)
    if args.resamples < 1:
        parser.error("--resamples must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        comparison = compare(
            args.label_a,
            load_scores(args.a),
            args.label_b,
            load_scores(args.b),
            seed=args.seed,
            resamples=args.resamples,
        )
    except (ValueError, OSError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    report = json.dumps(comparison, indent=2) if args.json else format_report(comparison)
    if args.out:
        args.out.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
