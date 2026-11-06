"""Compare two labelled sets of scored runs, without reading more into them than the data holds.

Input is a list of `score()` results per set. Only a few full games a week are possible, so sets of
3 to 8 runs are normal, and many outcomes are ranks or counts rather than bell-shaped numbers. So:

- Location: the Hodges-Lehmann shift (median of all B minus A differences). It needs no normal
  shape, is robust to one wild game, and suits ranks. Shown beside each set's median.
- Effect size: Cliff's delta, P(B > A) - P(B < A), from -1 to 1. It uses order only, so it is valid
  for ranks and ties, and says how often a B run beat an A run.
- Interval: a seeded percentile bootstrap of both. This is approximate, and with few runs it is
  too narrow, so it is withheld below `MIN_FOR_INTERVAL` runs per set and always flagged as rough
  under `ROBUST_N`.
- p-value: a two-sided permutation test on Cliff's delta. It is exact (every relabelling) when
  there are few enough, else a seeded Monte Carlo. Shown only as one more guard against noise:
  there is no correction for the many metrics compared, so the report says so.
- Warnings: a small set, and when even the most extreme split of the pooled runs could not give
  p < 0.05 (the test cannot find anything, so "no difference" would mean nothing).

A metric absent from a run stays absent: each set reports how many runs had it, and only those
runs are compared. Non-numeric metrics (such as the stop reason) are listed as skipped. Wins
(true/false) count as 1/0. Everything is deterministic for a given seed and uses the standard
library only.
"""

import itertools
import math
import random
from collections.abc import Sequence
from statistics import median

DEFAULT_SEED = 0
DEFAULT_RESAMPLES = 2000
CONFIDENCE = 0.95
MIN_FOR_INTERVAL = 3
ROBUST_N = 8
MIN_FOR_NOTE = 5
EXACT_LIMIT = 20000
MONTE_CARLO_SHUFFLES = 10000
ALPHA = 0.05
IDENTITY_KEYS = ("game_speed", "scenario")


def compare(
    label_a: str,
    runs_a: Sequence[dict],
    label_b: str,
    runs_b: Sequence[dict],
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
) -> dict:
    """Compare B against A on every metric and milestone. The result holds only JSON types."""
    series_a, series_b = _series(runs_a), _series(runs_b)
    skipped = sorted(set(_non_numeric(runs_a)) | set(_non_numeric(runs_b)))
    metrics = {}
    for name in sorted(set(series_a) | set(series_b)):
        if name in skipped:
            continue
        a, b = series_a.get(name, []), series_b.get(name, [])
        metrics[name] = _compare_metric(
            a, b, len(runs_a) - len(a), len(runs_b) - len(b), _rng(seed, name), resamples
        )
    return {
        "labels": {"a": label_a, "b": label_b},
        "runs": {"a": len(runs_a), "b": len(runs_b)},
        "seed": seed,
        "resamples": resamples,
        "warnings": _set_warnings(label_a, runs_a, label_b, runs_b),
        "metrics": metrics,
        "skipped_non_numeric": skipped,
    }


def _rng(seed: int, name: str) -> random.Random:
    """One stream per metric, so adding a metric never changes another's numbers."""
    return random.Random(f"{seed}:{name}")


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _non_numeric(runs: Sequence[dict]) -> list[str]:
    return [
        name
        for run in runs
        for name, value in run.get("metrics", {}).items()
        if not isinstance(value, bool) and not _numeric(value)
    ]


def _series(runs: Sequence[dict]) -> dict[str, list[float]]:
    """Values per metric, from the runs that have it. Absent runs add nothing, not a zero."""
    out: dict[str, list[float]] = {}
    for run in runs:
        for name, value in run.get("metrics", {}).items():
            if isinstance(value, bool) or _numeric(value):
                out.setdefault(name, []).append(float(value))
        for turn, fields in run.get("milestones", {}).items():
            for field, value in fields.items():
                if _numeric(value):
                    out.setdefault(f"turn_{turn}_{field}", []).append(float(value))
    return out


def _set_warnings(label_a: str, runs_a: Sequence[dict], label_b: str, runs_b: Sequence[dict]):
    warnings = []
    for label, runs in ((label_a, runs_a), (label_b, runs_b)):
        if not runs:
            warnings.append(f"{label} has no runs, so nothing can be compared")
        for key in IDENTITY_KEYS:
            values = {run.get("run", {}).get(key) for run in runs} - {None}
            if len(values) > 1:
                warnings.append(f"{label} mixes {key} values {sorted(values)}: not like for like")
    for key in IDENTITY_KEYS:
        values = [
            {run.get("run", {}).get(key) for run in runs} - {None} for runs in (runs_a, runs_b)
        ]
        if all(len(v) == 1 for v in values) and values[0] != values[1]:
            warnings.append(f"the sets differ in {key}: {sorted(values[0])} vs {sorted(values[1])}")
    warnings.append(
        "p-values are not corrected for the number of metrics compared: with many metrics, "
        "some will look significant by chance"
    )
    return warnings


def _compare_metric(
    a: list[float], b: list[float], absent_a: int, absent_b: int, rng: random.Random, resamples: int
) -> dict:
    result: dict = {
        "n": {"a": len(a), "b": len(b)},
        "absent": {"a": absent_a, "b": absent_b},
        "median": {"a": _median(a), "b": _median(b)},
        "shift": None,
        "cliffs_delta": None,
        "p_value": None,
        "p_method": None,
        "warnings": _metric_warnings(a, b, absent_a, absent_b),
    }
    if not a or not b:
        return result
    shift, delta = _hodges_lehmann(a, b), _cliffs_delta(a, b)
    result["shift"] = {"estimate": shift, "interval": None}
    result["cliffs_delta"] = {"estimate": delta, "interval": None}
    if min(len(a), len(b)) >= MIN_FOR_INTERVAL:
        shifts, deltas = _bootstrap(a, b, rng, resamples)
        result["shift"]["interval"] = _percentile(shifts)
        result["cliffs_delta"]["interval"] = _percentile(deltas)
    result["p_value"], result["p_method"] = _permutation_p(a, b, delta, rng)
    return result


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _metric_warnings(a: list[float], b: list[float], absent_a: int, absent_b: int) -> list[str]:
    warnings = []
    for tag, values, absent in (("A", a, absent_a), ("B", b, absent_b)):
        if absent:
            warnings.append(f"{absent} run(s) in {tag} lack this metric and are left out")
        if not values:
            warnings.append(f"no run in {tag} has this metric: no comparison")
    if not a or not b:
        return warnings
    smallest = min(len(a), len(b))
    if smallest < MIN_FOR_INTERVAL:
        warnings.append(
            f"only {smallest} run(s) in the smaller set: no interval is given, "
            "treat the estimate as an anecdote"
        )
    elif smallest < MIN_FOR_NOTE:
        warnings.append(
            f"small sets (n={len(a)} vs {len(b)}): the interval is rough and probably too narrow"
        )
    elif smallest < ROBUST_N:
        warnings.append(f"modest sets (n={len(a)} vs {len(b)}): the interval is approximate")
    floor = 2 / math.comb(len(a) + len(b), len(a))
    if floor > ALPHA:
        warnings.append(
            f"with {len(a)} vs {len(b)} runs the smallest possible two-sided p is {floor:.3f}: "
            "this comparison cannot reach p < 0.05 however large the difference"
        )
    return warnings


def _hodges_lehmann(a: Sequence[float], b: Sequence[float]) -> float:
    return median(y - x for x in a for y in b)


def _cliffs_delta(a: Sequence[float], b: Sequence[float]) -> float:
    more = sum(y > x for x in a for y in b)
    fewer = sum(y < x for x in a for y in b)
    return (more - fewer) / (len(a) * len(b))


def _bootstrap(a, b, rng: random.Random, resamples: int) -> tuple[list[float], list[float]]:
    shifts, deltas = [], []
    for _ in range(resamples):
        ra = rng.choices(a, k=len(a))
        rb = rng.choices(b, k=len(b))
        shifts.append(_hodges_lehmann(ra, rb))
        deltas.append(_cliffs_delta(ra, rb))
    return shifts, deltas


def _percentile(values: list[float]) -> list[float]:
    ordered = sorted(values)
    tail = (1 - CONFIDENCE) / 2
    low = ordered[int(tail * (len(ordered) - 1))]
    high = ordered[math.ceil((1 - tail) * (len(ordered) - 1))]
    return [low, high]


def _permutation_p(a, b, observed: float, rng: random.Random) -> tuple[float, str]:
    """Two-sided p for |delta|, exact when every relabelling can be tried."""
    pooled = [*a, *b]
    size, na = len(pooled), len(a)
    slack = 1e-12

    def extreme(chosen: Sequence[int]) -> bool:
        picked = set(chosen)
        left = [pooled[i] for i in picked]
        right = [pooled[i] for i in range(size) if i not in picked]
        return abs(_cliffs_delta(left, right)) >= abs(observed) - slack

    if math.comb(size, na) <= EXACT_LIMIT:
        hits = sum(extreme(c) for c in itertools.combinations(range(size), na))
        return hits / math.comb(size, na), "exact permutation"
    hits = sum(extreme(rng.sample(range(size), na)) for _ in range(MONTE_CARLO_SHUFFLES))
    return (hits + 1) / (MONTE_CARLO_SHUFFLES + 1), "Monte Carlo permutation"


def format_report(comparison: dict) -> str:
    """Plain text for a person. The JSON from `compare` is the full record."""
    label_a, label_b = comparison["labels"]["a"], comparison["labels"]["b"]
    runs = comparison["runs"]
    lines = [
        f"A = {label_a} ({runs['a']} runs)   B = {label_b} ({runs['b']} runs)",
        f"Shift and delta are B relative to A. Intervals are {CONFIDENCE:.0%} bootstrap "
        f"({comparison['resamples']} resamples, seed {comparison['seed']}).",
        "",
    ]
    lines += [f"WARNING: {text}" for text in comparison["warnings"]]
    for name, m in comparison["metrics"].items():
        lines += ["", name, _counts_line(m)]
        if m["shift"]:
            lines.append(f"  shift {_estimate(m['shift'])}   delta {_estimate(m['cliffs_delta'])}")
            lines.append(f"  p = {m['p_value']:.3f} ({m['p_method']})")
        lines += [f"  warning: {text}" for text in m["warnings"]]
    if comparison["skipped_non_numeric"]:
        lines += ["", "Not compared (not numeric): " + ", ".join(comparison["skipped_non_numeric"])]
    return "\n".join(lines)


def _counts_line(m: dict) -> str:
    n, absent, med = m["n"], m["absent"], m["median"]
    return (
        f"  A: n={n['a']} (absent {absent['a']}), median {_num(med['a'])}"
        f" | B: n={n['b']} (absent {absent['b']}), median {_num(med['b'])}"
    )


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4g}"


def _estimate(entry: dict) -> str:
    text = _num(entry["estimate"])
    interval = entry["interval"]
    return text + (f" [{_num(interval[0])}, {_num(interval[1])}]" if interval else " [no interval]")
