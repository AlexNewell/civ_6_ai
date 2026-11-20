"""Compare two labelled sets of scored runs, with effect sizes, intervals and sample-size warnings.

Each input is a JSON file from scripts/score_run.py. Nothing here connects to the game or the
network. The methods and their limits are in src/civ6_ai/metrics/report.py.

    compare_runs.py --label-a baseline --a a1.json a2.json --label-b tuned --b b1.json b2.json
"""

import argparse
import json
import sys
from pathlib import Path

from civ6_ai.metrics.civbench_gap import compare_lines, load_baselines
from civ6_ai.metrics.history import merge_logs
from civ6_ai.metrics.report import DEFAULT_RESAMPLES, DEFAULT_SEED, compare, format_report
from civ6_ai.site_quality import mean_site_score


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


def gap_section(label_a: str, logs_a: list[Path], label_b: str, logs_b: list[Path]) -> str:
    """The score-gap table for the runs whose turn logs were given, newest log first."""

    def runs(paths: list[Path]) -> list[tuple[str, list[dict]]]:
        newest_first = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
        return [(p.stem, merge_logs([p])) for p in newest_first]

    given = ((label_a, logs_a), (label_b, logs_b))
    sets = [(label, runs(paths)) for label, paths in given if paths]
    return "\n".join(compare_lines(sets, load_baselines()))


def site_section(label_a: str, logs_a: list[Path], label_b: str, logs_b: list[Path]) -> str:
    """Mean site score per founded city for each set, from the runs' settle logs (issue 324)."""
    lines = []
    for label, logs in ((label_a, logs_a), (label_b, logs_b)):
        found = [
            r
            for p in logs
            if (r := mean_site_score(p.with_name(f"{p.stem.removesuffix('_turns')}_settle.jsonl")))
        ]
        if found:
            cities = sum(n for n, _ in found)
            mean = sum(n * m for n, m in found) / cities
            lines.append(f"{label}: mean site score {mean:.1f} over {cities} founded cities")
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label-a", required=True, help="name of the first (reference) set")
    parser.add_argument("--a", type=Path, nargs="+", required=True, help="score files of set A")
    parser.add_argument("--label-b", required=True, help="name of the second set")
    parser.add_argument("--b", type=Path, nargs="+", required=True, help="score files of set B")
    parser.add_argument(
        "--logs-a", type=Path, nargs="*", default=[], help="turn logs of set A, one per run"
    )
    parser.add_argument(
        "--logs-b", type=Path, nargs="*", default=[], help="turn logs of set B, one per run"
    )
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
        gap = gap_section(args.label_a, args.logs_a, args.label_b, args.logs_b)
        sites = site_section(args.label_a, args.logs_a, args.label_b, args.logs_b)
        gap = "\n".join(x for x in (gap, sites) if x)
    except (ValueError, OSError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    if args.json:
        if gap:
            comparison["score_gap"] = gap.splitlines()
        report = json.dumps(comparison, indent=2)
    else:
        report = format_report(comparison) + (f"\n\n{gap}" if gap else "")
    if args.out:
        args.out.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
