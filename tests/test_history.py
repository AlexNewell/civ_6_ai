"""The score history: rows from turn logs, checkpoints, normalisation and the backfilled CSV."""

import json
from pathlib import Path

from civ6_ai.metrics.history import (
    CHECKPOINTS,
    COLUMNS,
    Point,
    add_row,
    at_checkpoint,
    format_history,
    judge,
    merge_logs,
    outcome_of,
    read_history,
    row_from_records,
    trends,
    write_history,
)
from score_history import main

HISTORY = Path(__file__).resolve().parent.parent / "docs" / "score-history.csv"


def record(turn, score=None, rivals=None, **extra):
    base = {"turn": turn, "handler_errors": {}, "total_seconds": 1.0, "stop_reason": None}
    if score is not None:
        base |= {"score": score, "rival_scores": rivals or {}}
    return base | extra


def write_log(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_checkpoint_share_and_rank():
    records = [record(t, 10 * t, {"1": 12 * t, "2": 5 * t}) for t in range(1, 101)]
    row = row_from_records("Run X", records, "S")
    assert row["score_50"] == 500
    assert row["share_50"] == round(500 / 600, 3)
    assert row["rank_50"] == 2
    assert row["score_100"] == 1000
    assert "score_150" not in row  # Not reached: left blank, not zero.
    assert row["last_score_turn"] == 100 and row["last_score"] == 1000
    assert row["outcome"] == "unfinished"


def test_leading_run_has_share_one_and_rank_one():
    point = Point(50, 100, (80, 20))
    assert point.share == 1.0 and point.rank == 1


def test_no_rival_scores_leaves_share_and_rank_blank():
    row = row_from_records("Run X", [record(t, t) for t in range(1, 52)], "S")
    assert row["score_50"] == 50
    assert row["share_50"] is None and row["rank_50"] is None


def test_checkpoint_uses_a_record_just_before_a_missing_turn():
    series = [Point(48, 1), Point(49, 2), Point(53, 3)]
    assert at_checkpoint(series, 50, 53).turn == 49
    assert at_checkpoint([Point(40, 1)], 50, 60) is None


def test_run_that_ended_before_a_checkpoint_has_none():
    row = row_from_records("Run X", [record(t, t) for t in range(1, 49)], "S")
    assert "score_50" not in row and row["last_score"] == 48


def test_logs_without_scores_still_give_an_outcome_row():
    row = row_from_records("Run X", [record(1), record(2)], "S")
    assert row["final_turn"] == 2 and "last_score" not in row


def test_score_per_turn_is_the_mean_over_the_span():
    row = row_from_records("Run X", [record(1, 0), record(3, 20)], "S")
    assert row["score_per_turn"] == 10.0


def test_outcome_from_the_game_report():
    lost = {"detail": "GAME OVER - DEFEAT. Frederick of Germany won a Science victory. x"}
    assert outcome_of(lost) == ("lost", "Germany", "Science")
    won = {"detail": "GAME OVER - VICTORY! You won a Culture victory!"}
    assert outcome_of(won) == ("won", "", "Culture")
    assert outcome_of({"detail": "x", "stop_reason": "game_over"})[0] == "unknown"
    assert outcome_of({"detail": "x", "stop_reason": "turn_limit"})[0] == "unfinished"


def test_merge_logs_later_session_wins_a_shared_turn(tmp_path):
    first = write_log(tmp_path / "a.jsonl", [record(1, 5), record(2, 6)])
    second = write_log(tmp_path / "b.jsonl", [record(2, 7), record(3, 8)])
    assert [r["score"] for r in merge_logs([first, second])] == [5, 7, 8]


def test_judge_uses_a_band_and_a_floor():
    assert judge(104, 100) == "flat"  # Inside 10 points.
    assert judge(630, 600) == "flat"  # Inside 5%.
    assert judge(700, 600) == "better"
    assert judge(500, 600) == "worse"


def test_trends_skip_missing_checkpoints_and_compare_to_previous_and_best():
    rows = [
        {"run": "A", "score_100": "200"},
        {"run": "B", "score_100": "100", "score_150": "300"},
        {"run": "C", "score_100": "150"},
    ]
    found = {t["run"]: t["verdicts"] for t in trends(rows)}
    assert found["A"] == {100: (None, None)}
    assert found["B"] == {100: ("worse", "worse"), 150: (None, None)}
    assert found["C"] == {100: ("better", "worse")}
    assert 200 not in found["C"]


def test_add_row_replaces_a_run_in_place():
    rows = [{"run": "A", "x": 1}, {"run": "B"}]
    assert add_row(rows, {"run": "A", "x": 2}) == [{"run": "A", "x": 2}, {"run": "B"}]
    assert add_row(rows, {"run": "C"})[-1] == {"run": "C"}


def test_csv_round_trip_and_blank_cells(tmp_path):
    path = tmp_path / "h.csv"
    write_history(path, [row_from_records("R", [record(t, t) for t in range(1, 60)], "S")])
    (row,) = read_history(path)
    assert row["score_50"] == "50" and row["score_100"] == ""
    assert tuple(row) == COLUMNS


def test_command_adds_then_reports(tmp_path, capsys):
    log = write_log(tmp_path / "S_1.jsonl", [record(t, t, {"1": 2 * t}) for t in range(1, 61)])
    history = tmp_path / "h.csv"
    assert main(["--history", str(history), "add", "Run 1", str(log)]) == 0
    assert main(["--history", str(history), "report"]) == 0
    out = capsys.readouterr().out
    assert "Run 1" in out and "unfinished" in out and "60" in out


def test_command_fails_on_an_unreadable_log(tmp_path, capsys):
    bad = tmp_path / "S_1.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    assert main(["--history", str(tmp_path / "h.csv"), "add", "R", str(bad)]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_committed_history_backfill():
    rows = {r["run"]: r for r in read_history(HISTORY)}
    assert tuple(next(iter(rows.values()))) == COLUMNS
    assert rows["Run 9"]["score_100"] == "153" and rows["Run 9"]["score_250"] == "623"
    assert rows["Run 8"]["score_200"] == "432" and rows["Run 8"]["score_300"] == ""
    assert rows["Run 4"]["winner"] == "Sumeria" and rows["Run 4"]["victory_type"] == "culture"
    assert rows["Run 7"]["final_turn"] == "309"
    assert all(r["source"] == "manual" for r in rows.values())
    # Older runs have outcomes only: no invented scores.
    for name in ("Run 3", "Run 4", "Run 5", "Run 6", "Run 7"):
        assert all(rows[name][f"score_{c}"] == "" for c in CHECKPOINTS)


def test_report_text_on_the_backfill():
    text = format_history(read_history(HISTORY))
    assert "Run 9" in text and "b/b" in text
    assert format_history([]) == "No runs in the history."
