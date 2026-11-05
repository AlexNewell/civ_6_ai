import json

import pytest

from summarise_civbench import (
    call_stats,
    final_standings,
    game_result,
    latest_rows,
    milestones,
    normalise_civ,
    render,
)


def row(pid, civ, score, turn=100, run="r", is_agent=False):
    return {
        "runId": run,
        "pid": pid,
        "civ": civ,
        "score": score,
        "turn": turn,
        "is_agent": is_agent,
        "cities": 2,
        "science": 10.0,
        "culture": 5.0,
        "techs_completed": 3,
    }


def standings_rows():
    return [
        row(0, "Babylon", 300, is_agent=True),
        row(1, "Japan", 500),
        row(2, "Korea", 400),
        row(3, "Rome", 100),
    ]


def game(result="defeat", winner="CIVILIZATION_JAPAN"):
    return {
        "runId": "r",
        "agentModel": "gpt-5.4",
        "scenarioId": "ground_control",
        "outcome": json.dumps({"result": result, "victoryType": "Culture", "winnerCiv": winner}),
    }


def test_normalise_civ_matches_both_spellings():
    assert normalise_civ("CIVILIZATION_JAPAN") == normalise_civ("Japan")


def test_latest_rows_drops_repeats_and_keeps_the_last():
    rows = latest_rows([row(0, "Babylon", 1), row(0, "Babylon", 2)])
    assert [r["score"] for r in rows] == [2]


def test_winner_ranks_first_even_behind_on_score():
    rank, players, normalised = final_standings(standings_rows(), "Rome")
    # Japan and Korea outscore the agent, and Rome won, so the agent is below all three.
    assert (rank, players) == (4, 4)
    assert normalised == 3.0


def test_agent_win_is_rank_one():
    rank, _, normalised = final_standings(standings_rows(), "Babylon")
    assert (rank, normalised) == (1, 1.0)


def test_equal_scores_share_the_better_rank():
    rows = standings_rows() + [row(4, "Mapuche", 300)]
    assert final_standings(rows, "Japan")[0] == 3


def test_eliminated_player_counts_but_cant_outrank():
    rows = standings_rows() + [row(4, "Scythia", 900, turn=50)]
    rank, players, _ = final_standings(rows, "Japan")
    assert (rank, players) == (3, 5)


def test_stray_rows_after_the_agents_last_turn_are_ignored():
    rows = standings_rows() + [row(1, "Japan", 9000, turn=331)]
    rank, _, normalised = final_standings(rows, "Japan")
    assert rank == 3
    assert normalised == pytest.approx(0.6)


def test_unknown_winner_is_an_error_not_a_bad_rank():
    with pytest.raises(ValueError):
        final_standings(standings_rows(), "Atlantis")


def test_game_result_scores_a_win_as_one():
    result = game_result(game("victory", "CIVILIZATION_BABYLON"), standings_rows())
    assert result.won and result.rank == 1 and result.normalised_score == 1.0
    assert result.final_turn == 100


def test_game_result_scores_a_defeat_against_the_winner():
    result = game_result(game(), standings_rows())
    assert not result.won and result.rank == 3
    assert result.normalised_score == pytest.approx(0.6)


def test_milestones_average_ais_within_a_game_then_take_the_median():
    rows = [
        row(0, "A", 100, turn=50, run="x", is_agent=True),
        row(1, "B", 10, turn=50, run="x"),
        row(2, "C", 30, turn=50, run="x"),
        row(1, "B", 40, turn=50, run="y"),
    ]
    by_side = {m["is_agent"]: m for m in milestones(rows, turns=(50,))}
    assert by_side[True]["runs"] == 1 and by_side[True]["score"] == 100
    assert by_side[False]["runs"] == 2 and by_side[False]["score"] == 30


def test_milestones_skip_turns_no_game_reached():
    assert [m["turn"] for m in milestones([row(0, "A", 1, turn=50)], turns=(50, 250))] == [50]


def call(run, success=True, kind="tool_call"):
    return {"runId": run, "tool": "end_turn", "category": "turn", "success": success, "type": kind}


def test_call_stats_ignore_error_and_game_over_rows():
    calls = [call("a"), call("a", success=False), call("b"), call("b", kind="error")]
    stats = call_stats(calls, {"a": 2, "b": 1})
    assert stats["total"] == 3
    assert stats["failed_share"] == pytest.approx(1 / 3)
    assert stats["per_turn"] == 1


def test_call_stats_count_a_game_with_no_calls():
    assert call_stats([call("a")], {"a": 1, "b": 1})["mean_per_game"] == 0.5


def test_render_keeps_attribution_and_caveats():
    page = render(
        [game_result(game(), standings_rows())],
        {"ground_control": milestones(standings_rows(), turns=(100,))},
        call_stats([call("r")], {"r": 100}),
    )
    assert "CC BY 4.0" in page
    assert "Andrews" in page
    assert "isn't a leaderboard" in page
