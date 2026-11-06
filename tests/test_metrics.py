import json
from decimal import Decimal

import pytest

from civ6_ai.metrics import RunData, Snapshot, Standing, add_spend, load_turn_log, score
from civ6_ai.metrics.scoring import METRICS


def turn_line(turn, errors=None, seconds=2.0, **extra):
    return json.dumps(
        {
            "turn": turn,
            "handler_seconds": {},
            "handler_errors": errors or {},
            "end_turn_seconds": 1.0,
            "total_seconds": seconds,
            "stop_reason": None,
            "detail": "",
            **extra,
        }
    )


def write_log(path, lines, torn=""):
    path.write_text("\n".join(lines) + "\n" + torn, encoding="utf-8")
    return path


def test_every_metric_is_present_or_absent_never_both():
    result = score(RunData(run_id="x", source="ours"))
    assert set(result["metrics"]) | set(result["absent"]) == set(METRICS)
    assert not set(result["metrics"]) & set(result["absent"])
    assert json.loads(json.dumps(result)) == result


def test_empty_run_has_no_metrics_so_nothing_reads_as_zero():
    assert score(RunData(run_id="x", source="ours"))["metrics"] == {}


def test_our_log_reports_only_what_it_holds(tmp_path):
    log = write_log(
        tmp_path / "run1.jsonl",
        [
            turn_line(1),
            turn_line(2, {"a": "boom"}, seconds=4.0),
            turn_line(3, {"a": "x", "b": "y"}),
        ],
    )
    result = score(load_turn_log(log))
    assert result["run"] == {"run_id": "run1", "source": "ours"}
    assert result["metrics"]["turns_played"] == 3
    assert result["metrics"]["handler_errors"] == 3
    assert result["metrics"]["handler_errors_per_turn"] == 1.0
    assert result["metrics"]["seconds_per_turn"] == pytest.approx(8.0 / 3)
    for name in (
        "rank",
        "cities",
        "science",
        "techs_per_turn",
        "kill_ratio",
        "cost_per_decision_usd",
    ):
        assert name in result["absent"]
        assert name not in result["metrics"]


def test_zero_errors_is_a_measured_zero(tmp_path):
    result = score(load_turn_log(write_log(tmp_path / "r.jsonl", [turn_line(1)])))
    assert result["metrics"]["handler_errors"] == 0


def test_torn_last_line_is_ignored(tmp_path):
    log = write_log(tmp_path / "r.jsonl", [turn_line(1), turn_line(2)], torn='{"turn": 3, "han')
    assert load_turn_log(log).turns_played == 2


def test_unreadable_or_empty_log_fails(tmp_path):
    with pytest.raises(ValueError, match="Unreadable line 1"):
        load_turn_log(write_log(tmp_path / "a.jsonl", ["not json"]))
    with pytest.raises(ValueError, match="no finished turns"):
        load_turn_log(
            (tmp_path / "b.jsonl").write_text("", encoding="utf-8") or tmp_path / "b.jsonl"
        )


def test_counts_are_summed_only_when_every_turn_has_one(tmp_path):
    full = write_log(
        tmp_path / "a.jsonl",
        [
            turn_line(1, decisions=10, failed_decisions=1),
            turn_line(2, decisions=20, failed_decisions=2),
        ],
    )
    metrics = score(load_turn_log(full))["metrics"]
    assert metrics["decisions"] == 30
    assert metrics["errors_per_decision"] == pytest.approx(0.1)

    partial = write_log(tmp_path / "b.jsonl", [turn_line(1, decisions=10), turn_line(2)])
    result = score(load_turn_log(partial))
    assert "decisions" in result["absent"]


def test_victory_report_gives_outcome(tmp_path):
    detail = "GAME OVER - VICTORY! You won a Science victory."
    log = write_log(
        tmp_path / "r.jsonl", [turn_line(1), turn_line(2, stop_reason="game_over", detail=detail)]
    )
    metrics = score(load_turn_log(log))["metrics"]
    assert metrics["won"] is True
    assert metrics["victory_type"] == "Science"
    assert metrics["stop_reason"] == "game_over"


def test_standing_from_log_when_recorded(tmp_path):
    log = write_log(
        tmp_path / "r.jsonl",
        [turn_line(1, rank=3, players=6, score_vs_winner=0.5)],
    )
    metrics = score(load_turn_log(log))["metrics"]
    assert (metrics["rank"], metrics["players"], metrics["score_vs_winner"]) == (3, 6, 0.5)


def test_snapshot_fields_and_milestones(tmp_path):
    log = write_log(
        tmp_path / "r.jsonl",
        [
            turn_line(50, cities=2, techs_completed=10, score=90),
            turn_line(100, cities=4, techs_completed=30, score=200),
        ],
    )
    result = score(load_turn_log(log))
    assert result["metrics"]["cities"] == 4
    assert result["metrics"]["techs_per_turn"] == pytest.approx(0.4)
    assert result["milestones"]["50"] == {"score": 90, "cities": 2, "techs_completed": 10}
    assert "science" not in result["milestones"]["50"]


def test_tech_pace_needs_two_distinct_turns():
    one = RunData(run_id="x", source="s", snapshots=(Snapshot(5, techs_completed=3),))
    assert "techs_per_turn" in score(one)["absent"]


def test_ratios_with_zero_denominator_are_absent_with_reason():
    run = RunData(run_id="x", source="s", kills=4, losses=0, decisions=0, cost_usd=1.0)
    absent = score(run)["absent"]
    assert "losses is 0" in absent["kill_ratio"]
    assert "decisions is 0" in absent["cost_per_decision_usd"]


def test_kill_ratio_and_cost_per_decision():
    run = RunData(run_id="x", source="s", kills=6, losses=3, decisions=100, cost_usd=5.0)
    metrics = score(run)["metrics"]
    assert metrics["kill_ratio"] == 2.0
    assert metrics["cost_per_decision_usd"] == 0.05


def test_standing_dataclass_feeds_rank():
    run = RunData(run_id="x", source="s", standing=Standing(2, 6, 0.9))
    assert score(run)["metrics"]["rank"] == 2


def write_ledger(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def open_event(call_id, game, worst):
    return {"event": "open", "call_id": call_id, "game_id": game, "worst_case_usd": worst}


def test_spend_counts_unclosed_calls_at_worst_case(tmp_path):
    ledger = write_ledger(
        tmp_path / "ledger.jsonl",
        [
            open_event("a", "g1", "1.00"),
            {"event": "close", "call_id": "a", "cost_usd": "0.25"},
            open_event("b", "g1", "0.50"),
            open_event("c", "g2", "9.00"),
        ],
    )
    log = write_log(tmp_path / "r.jsonl", [turn_line(1, decisions=10, failed_decisions=0)])
    run = add_spend(load_turn_log(log), ledger, "g1")
    assert Decimal(str(run.cost_usd)) == Decimal("0.75")
    assert run.cost_unsettled_usd == 0.5
    metrics = score(run)["metrics"]
    assert metrics["cost_per_decision_usd"] == pytest.approx(0.075)


def test_spend_for_unknown_game_is_absent_not_zero(tmp_path):
    ledger = write_ledger(tmp_path / "l.jsonl", [open_event("a", "g1", "1")])
    log = write_log(tmp_path / "r.jsonl", [turn_line(1)])
    result = score(add_spend(load_turn_log(log), ledger, "nope"))
    assert "cost_usd" in result["absent"]
    assert "nope" in result["absent"]["cost_usd"]


def test_spend_reading_does_not_modify_a_torn_ledger(tmp_path):
    ledger = tmp_path / "l.jsonl"
    ledger.write_text(json.dumps(open_event("a", "g1", "1")) + '\n{"event": "cl', encoding="utf-8")
    before = ledger.read_bytes()
    add_spend(RunData(run_id="x", source="ours"), ledger, "g1")
    assert ledger.read_bytes() == before
