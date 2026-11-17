"""The score gap against the leader and the CivBench medians."""

import json
import os
from pathlib import Path

import pytest

from civ6_ai.metrics.civbench_gap import (
    BASELINES_DOC,
    GAP_TURNS,
    compare_lines,
    gap_lines,
    gaps,
    load_baselines,
)
from compare_runs import main as compare_main

BASE = {100: {"llm": 239.0, "ai": 254.0}, 150: {"llm": 369.0, "ai": 417.0}}


def record(turn, score, rivals=None):
    base = {"turn": turn, "handler_errors": {}, "total_seconds": 1.0, "stop_reason": None}
    return base | {"score": score, "rival_scores": rivals or {}}


def run(last, rivals=None):
    return [record(t, t * 2, rivals) for t in range(1, last + 1)]


def test_baselines_are_read_from_the_page():
    text = BASELINES_DOC.read_text(encoding="utf-8")
    baselines = load_baselines()
    assert set(GAP_TURNS) <= set(baselines)
    ground = text.split("### Ground Control")[1].split("### Snowflake")[0]
    for turn, base in baselines.items():
        assert f"| {turn} | LLM | " in ground and f"| {turn} | Built-in AI | " in ground
        assert set(base) == {"llm", "ai"}
    # The documented Ground Control turn-100 medians.
    assert baselines[100] == {"llm": 239.0, "ai": 254.0}


def test_other_scenario_and_missing_scenario(tmp_path):
    assert load_baselines(scenario="Snowflake")[100]["llm"] == 152.0
    page = tmp_path / "page.md"
    page.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Ground Control"):
        load_baselines(page)


def test_gaps_to_leader_and_medians():
    (entry,) = gaps(run(100, {"1": 300, "2": 150}), BASE)
    assert entry == {"turn": 100, "score": 200, "to_leader": -100, "to_llm": -39, "to_ai": -54}


def test_no_rivals_gives_no_leader_gap_and_unreached_turns_are_skipped():
    entries = gaps(run(120), BASE)
    assert [e["turn"] for e in entries] == [100]
    assert entries[0]["to_leader"] is None
    assert gaps(run(99), BASE) == []
    assert gap_lines(run(99), BASE) == []


def test_turn_without_a_baseline_has_blank_median_gaps():
    entry = gaps(run(250), {})[-1]
    assert entry["turn"] == 250 and entry["to_llm"] is None and entry["to_ai"] is None


def test_gap_lines_print_only_reached_checkpoints():
    lines = gap_lines(run(160, {"1": 400}), BASE)
    text = "\n".join(lines)
    assert "100" in lines[2] and "150" in lines[3] and len(lines) == 4
    assert "-200" in lines[2]  # 200 - 400
    assert "200" not in text.split("\n", 2)[2].split()[0:1]


def test_compare_lines_list_newest_first_and_note_short_runs():
    lines = compare_lines([("tuned", [("new", run(100)), ("short", run(50))])], BASE)
    assert "newest first" in lines[0]
    assert lines[2].split()[1] == "new"
    assert "ended before turn 100" in lines[3]


def test_compare_runs_cli_orders_logs_newest_first(tmp_path, capsys):
    score = tmp_path / "s.json"
    score.write_text(json.dumps({"metrics": {"score": 1}}), encoding="utf-8")
    old, new = tmp_path / "old_run.jsonl", tmp_path / "new_run.jsonl"
    for path, last in ((old, 100), (new, 150)):
        path.write_text("".join(json.dumps(r) + "\n" for r in run(last)), encoding="utf-8")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    code = compare_main(
        [
            "--label-a",
            "A",
            "--a",
            str(score),
            "--label-b",
            "B",
            "--b",
            str(score),
            "--logs-a",
            str(old),
            str(new),
            "--resamples",
            "10",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert out.index("new_run") < out.index("old_run")
    assert Path(BASELINES_DOC).exists()
