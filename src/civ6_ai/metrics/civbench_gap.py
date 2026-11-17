"""Our score against the leader and the CivBench medians at fixed turns.

The CivBench numbers are not copied here: `load_baselines` reads them from the table in
docs/civbench-baselines.md (which scripts/summarise_civbench.py generates), so a regenerated page
is picked up and nothing is typed in twice. The comparison is a reference level, not a pass mark:
the CivBench scenario (Ground Control, 8 players, Standard map) is not our standard game, and the
medians use only games that reached each turn. See that page for the caveats.

The leader is the best *met* rival, as in history.py, so the gap to it is a lower bound on the
true gap. A gap is our score minus theirs: negative means we are behind. Nothing here reads the
game or the network.
"""

import re
from pathlib import Path

from civ6_ai.metrics.history import at_checkpoint, points

GAP_TURNS = (100, 150, 200, 250)
SCENARIO = "Ground Control"
BASELINES_DOC = Path(__file__).resolve().parents[3] / "docs" / "civbench-baselines.md"
ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(LLM|Built-in AI)\s*\|\s*\d+\s*\|\s*(\d+(?:\.\d+)?)\s*\|")
PLAYER_KEY = {"LLM": "llm", "Built-in AI": "ai"}


def load_baselines(
    path: Path = BASELINES_DOC, scenario: str = SCENARIO
) -> dict[int, dict[str, float]]:
    """{turn: {'llm': median score, 'ai': built-in AI median}} from the page's progress table."""
    text = path.read_text(encoding="utf-8")
    start = re.search(rf"^### {re.escape(scenario)}\s*$", text, re.MULTILINE)
    if not start:
        raise ValueError(f"{path.name} has no '### {scenario}' progress table")
    section = text[start.end() :].split("\n#", 1)[0]
    result: dict[int, dict[str, float]] = {}
    for line in section.splitlines():
        if match := ROW.match(line):
            result.setdefault(int(match[1]), {})[PLAYER_KEY[match[2]]] = float(match[3])
    return result


def gaps(records: list[dict], baselines: dict[int, dict[str, float]]) -> list[dict]:
    """One entry per gap turn the run reached: our score and the gaps (None where unknown)."""
    series = points(records)
    final_turn = records[-1]["turn"]
    entries = []
    for turn in GAP_TURNS:
        point = at_checkpoint(series, turn, final_turn)
        if point is None:
            continue
        base = baselines.get(turn, {})
        entries.append(
            {
                "turn": turn,
                "score": point.score,
                "to_leader": point.score - max(point.rivals) if point.rivals else None,
                "to_llm": point.score - base["llm"] if "llm" in base else None,
                "to_ai": point.score - base["ai"] if "ai" in base else None,
            }
        )
    return entries


def _signed(value: float | None) -> str:
    return "-" if value is None else f"{value:+.0f}"


def gap_lines(records: list[dict], baselines: dict[int, dict[str, float]]) -> list[str]:
    """The gap table for one run, or [] when it reached no gap turn."""
    entries = gaps(records, baselines)
    if not entries:
        return []
    lines = [
        f"Score gap (ours minus theirs; CivBench {SCENARIO} medians):",
        f"{'turn':>6}{'score':>7}{'leader':>8}{'LLM med':>9}{'AI med':>8}",
    ]
    lines += [
        f"{e['turn']:>6}{e['score']:>7}{_signed(e['to_leader']):>8}"
        f"{_signed(e['to_llm']):>9}{_signed(e['to_ai']):>8}"
        for e in entries
    ]
    return lines


def compare_lines(
    sets: list[tuple[str, list[tuple[str, list[dict]]]]], baselines: dict[int, dict[str, float]]
) -> list[str]:
    """Gap rows for several labelled sets of runs, each set's runs given newest first.

    Newest first because recent runs reflect the current code. Runs that reached no gap turn are
    listed with a note, so a short run is not mistaken for a missing one."""
    lines = [
        f"Score gap at turns {', '.join(map(str, GAP_TURNS))} (ours minus theirs; "
        f"CivBench {SCENARIO} medians; runs newest first)",
        f"{'set':<14}{'run':<22}{'turn':>5}{'score':>7}{'leader':>8}{'LLM med':>9}{'AI med':>8}",
    ]
    for label, runs in sets:
        for name, records in runs:
            entries = gaps(records, baselines)
            if not entries:
                lines.append(f"{label:<14}{name:<22}  (ended before turn {GAP_TURNS[0]})")
            lines += [
                f"{label:<14}{name:<22}{e['turn']:>5}{e['score']:>7}"
                f"{_signed(e['to_leader']):>8}{_signed(e['to_llm']):>9}{_signed(e['to_ai']):>8}"
                for e in entries
            ]
    return lines
