"""CivBench scoring and the command, on small synthetic rows that follow the documented schema.

The real dataset is not needed, so nothing here reads or downloads it.
"""

import json

import pytest

from civ6_ai.metrics import score
from score_run import agent_snapshots, city_yields, civbench_run, main


def player(pid, civ, turn, score_, is_agent=False, **extra):
    return {
        "runId": "r",
        "pid": pid,
        "civ": civ,
        "is_agent": is_agent,
        "turn": turn,
        "score": score_,
        "cities": 3,
        "science": 12.0,
        "culture": 6.0,
        "techs_completed": 10 + turn // 10,
        "gold_per_turn": 5.0,
        "faith_per_turn": 1.0,
        "game_speed": "GAMESPEED_STANDARD",
        **extra,
    }


def game(outcome=None):
    return {
        "runId": "r",
        "agentModel": "gpt-5.4",
        "scenarioId": "ground_control",
        "admissible": True,
        "outcome": json.dumps(
            outcome
            or {"result": "defeat", "victoryType": "Culture", "winnerCiv": "CIVILIZATION_JAPAN"}
        ),
    }


def rows():
    out = []
    for turn in (50, 100):
        out += [
            player(0, "Babylon", turn, 100 + turn, is_agent=True),
            player(1, "Japan", turn, 300 + turn),
            player(2, "Korea", turn, 50 + turn),
        ]
    return out


def calls():
    return [
        {
            "runId": "r",
            "type": "tool_call",
            "tool": "end_turn",
            "category": "turn",
            "success": True,
        },
        {
            "runId": "r",
            "type": "tool_call",
            "tool": "unit_action",
            "category": "action",
            "success": False,
        },
        {
            "runId": "r",
            "type": "tool_call",
            "tool": "get_units",
            "category": "query",
            "success": True,
        },
        {
            "runId": "r",
            "type": "tool_call",
            "tool": "get_units",
            "category": "query",
            "success": True,
        },
        {"runId": "r", "type": "error", "tool": "", "category": "", "success": False},
    ]


def cities():
    return [
        {"pid": 0, "city_id": 1, "turn": 100, "food": 5.0, "production": 3.0},
        {"pid": 0, "city_id": 2, "turn": 100, "food": 4.0, "production": 2.0},
        {"pid": 1, "city_id": 9, "turn": 100, "food": 99.0, "production": 99.0},
    ]


def test_civbench_run_scores_rank_and_progress():
    result = score(civbench_run(game(), rows(), calls(), cities()))
    metrics = result["metrics"]
    assert metrics["rank"] == 2
    assert metrics["players"] == 3
    assert metrics["score_vs_winner"] == pytest.approx(200 / 400)
    assert metrics["won"] is False
    assert metrics["turns_played"] == 100
    assert metrics["decisions"] == 4
    assert metrics["failed_decisions"] == 1
    assert metrics["errors_per_decision"] == 0.25
    assert metrics["techs_per_turn"] == pytest.approx(0.1)
    assert metrics["food_per_turn"] == 9.0
    assert metrics["production_per_turn"] == 5.0
    assert result["run"]["game_speed"] == "GAMESPEED_STANDARD"
    assert result["milestones"]["50"]["score"] == 150


def test_civbench_gaps_are_absent_with_reasons():
    absent = score(civbench_run(game(), rows(), calls(), cities()))["absent"]
    for name in ("cost_usd", "cost_per_decision_usd", "kills", "kill_ratio", "handler_errors"):
        assert name in absent
    assert "tool_calls" not in absent["kills"]
    assert "range" in absent["cost_usd"]


def test_agent_snapshots_ignore_other_players_and_order_by_turn():
    shuffled = list(reversed(rows()))
    snapshots = agent_snapshots(shuffled, [])
    assert [s.turn for s in snapshots] == [50, 100]
    assert snapshots[0].food_per_turn is None


def test_city_yields_keep_last_repeat_and_only_that_player():
    repeated = [*cities(), {"pid": 0, "city_id": 1, "turn": 100, "food": 7.0, "production": 3.0}]
    assert city_yields(repeated, 0) == {100: (11.0, 5.0)}


def test_missing_calls_and_cities_are_absent_not_zero():
    result = score(civbench_run(game(), rows(), [], None))
    assert "decisions" in result["absent"]
    assert "food_per_turn" in result["absent"]
    assert "decisions" not in result["metrics"]


def test_no_outcome_leaves_standing_absent():
    broken = game()
    broken["outcome"] = ""
    result = score(civbench_run(broken, rows(), calls()))
    assert "rank" in result["absent"]
    assert "no recorded outcome" in result["absent"]["won"]


def test_win_gives_rank_one():
    outcome = {
        "result": "victory",
        "victoryType": "Technology",
        "winnerCiv": "CIVILIZATION_BABYLON",
    }
    metrics = score(civbench_run(game(outcome), rows(), calls()))["metrics"]
    assert metrics["rank"] == 1
    assert metrics["won"] is True
    assert metrics["score_vs_winner"] == 1.0


def test_command_scores_our_log_as_json(tmp_path, capsys):
    log = tmp_path / "run.jsonl"
    log.write_text(
        json.dumps(
            {"turn": 1, "handler_errors": {}, "total_seconds": 3.0, "stop_reason": "turn_limit"}
        )
        + "\n",
        encoding="utf-8",
    )
    assert main([str(log)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["metrics"]["turns_played"] == 1
    assert printed["metrics"]["stop_reason"] == "turn_limit"


def test_command_writes_file_and_adds_spend(tmp_path):
    log = tmp_path / "run.jsonl"
    log.write_text(
        json.dumps({"turn": 1, "handler_errors": {}, "total_seconds": 3.0}) + "\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"event": "open", "call_id": "a", "game_id": "g", "worst_case_usd": "1"})
        + "\n"
        + json.dumps({"event": "close", "call_id": "a", "cost_usd": "0.5"})
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "score.json"
    assert main([str(log), "--ledger", str(ledger), "--game-id", "g", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["metrics"]["cost_usd"] == 0.5


def test_command_reports_bad_input(tmp_path, capsys):
    assert main([str(tmp_path / "missing.jsonl")]) == 1
    assert "FAILED" in capsys.readouterr().err
    assert main(["--civbench", "r", "--root", str(tmp_path)]) == 1
    with pytest.raises(SystemExit):
        main([])
    with pytest.raises(SystemExit):
        main([str(tmp_path / "a"), "--ledger", str(tmp_path / "b")])
