"""The cost report, on logs written by the real TurnLog and Governor, so the formats can't drift."""

import json
from decimal import Decimal
from pathlib import Path

from civ6_ai.budget import BudgetedCall, Governor, Ledger, Usage, load_pricing
from civ6_ai.metrics.costs import (
    LEDGER_NAME,
    build_report,
    game_id_for_new_session,
    game_id_of_log,
    pricing_checked,
    scan,
)
from civ6_ai.orchestrator import TurnLog, TurnRecord
from cost_report import main

PRICING = load_pricing()
SONNET = "claude-sonnet-5-5"
SAVE = "STD_FIXTURE_01"
# Sonnet: 10,000 input at $2 and 1,000 output at $10 per million = $0.03, so calls fit the cap.
CALL_USAGE = Usage(input_tokens=10_000, output_tokens=1_000)
CALL_COST = Decimal("0.03")


def session(runs: Path, stamp: str, turns: range, save: str = SAVE) -> Path:
    log = TurnLog(runs / f"{save}_{stamp}.jsonl")
    for turn in turns:
        log.write(
            TurnRecord(
                turn=turn,
                handler_seconds={},
                handler_errors={},
                end_turn_seconds=1.0,
                total_seconds=2.0,
            )
        )
    return log.path


def spend(runs: Path, game_id: str, calls: int, model: str = SONNET, player: str = "p1") -> None:
    """Make calls through the real Governor. For another model, the finished ledger is rewritten
    to name it, which stands in for a model the price table doesn't have."""
    ledger = Ledger(runs / LEDGER_NAME)
    before = (runs / LEDGER_NAME).read_text() if (runs / LEDGER_NAME).exists() else ""
    for turn in range(calls):
        call = BudgetedCall(game_id, player, "strategist", SONNET, 10_000, 1_000, None, turn)
        done = Governor(PRICING, ledger).call(call, lambda: ("ok", CALL_USAGE))
        assert done.cost_usd == CALL_COST
    if model != SONNET:
        after = (runs / LEDGER_NAME).read_text()
        new = after[len(before) :].replace(SONNET, model)
        (runs / LEDGER_NAME).write_text(before + new)


def report(runs: Path, turns: int = 200, budget: Decimal | None = None) -> dict:
    games, ledger, problems = scan(runs, PRICING)
    return build_report(games, ledger, problems, pricing_checked(), turns, budget)


def only_game(runs: Path) -> dict:
    games = report(runs)["games"]
    assert len(games) == 1
    return games[0]


def test_same_total_over_ten_times_the_turns_costs_a_tenth_per_turn(tmp_path):
    short, long = tmp_path / "a", tmp_path / "b"
    for runs, turns in ((short, 10), (long, 100)):
        runs.mkdir()
        session(runs, "20260101T000000Z", range(1, turns + 1))
        spend(runs, f"{SAVE}_20260101T000000Z", 4)
    a, b = only_game(short), only_game(long)
    assert a["cost_usd"] == b["cost_usd"] == "0.120000"
    assert Decimal(a["cost_per_turn_usd"]) == Decimal("0.012")
    assert Decimal(b["cost_per_turn_usd"]) == Decimal("0.0012")
    assert Decimal(a["cost_per_turn_usd"]) == 10 * Decimal(b["cost_per_turn_usd"])
    assert Decimal(a["tokens_per_turn"]) == 10 * Decimal(b["tokens_per_turn"])
    assert Decimal(b["projected_cost_usd"]) == Decimal("0.24")  # 200 turns at $0.0012


def test_sessions_of_one_game_are_one_game_and_a_rewind_starts_another(tmp_path):
    session(tmp_path, "20260101T000000Z", range(1, 6))
    session(tmp_path, "20260101T010000Z", range(6, 9))
    session(tmp_path, "20260101T020000Z", range(10, 13))  # A gap is still the same game.
    session(tmp_path, "20260102T000000Z", range(1, 4))  # Back to turn 1: a second game.
    session(tmp_path, "20260102T010000Z", range(4, 6))
    session(tmp_path, "20260102T000000Z", range(1, 4), save="STD_OTHER_02")
    games = {g["game_id"]: g for g in report(tmp_path)["games"]}
    assert {k: (v["sessions"], v["turns"]) for k, v in games.items()} == {
        f"{SAVE}_20260101T000000Z": (3, 11),
        f"{SAVE}_20260102T000000Z": (2, 5),
        "STD_OTHER_02_20260102T000000Z": (1, 3),
    }


def test_a_game_resumed_from_a_mid_game_save_counts_only_turns_played(tmp_path):
    session(tmp_path, "20260101T000000Z", range(35, 39))
    assert only_game(tmp_path)["turns"] == 4


def test_spend_from_any_session_of_a_game_lands_on_that_game(tmp_path):
    first = session(tmp_path, "20260101T000000Z", range(1, 6)).stem
    second = session(tmp_path, "20260101T010000Z", range(6, 11)).stem
    spend(tmp_path, first, 1)
    spend(tmp_path, second, 1)
    game = only_game(tmp_path)
    assert (game["calls"], game["turns"], game["cost_usd"]) == (2, 10, "0.060000")


def test_a_game_with_no_calls_is_listed_at_zero_and_counted(tmp_path):
    session(tmp_path, "20260101T000000Z", range(1, 31))
    result = report(tmp_path)
    assert result["games"][0]["cost_per_turn_usd"] == "0.000000"
    assert result["zero_call_games"] == [{"game_id": f"{SAVE}_20260101T000000Z", "turns": 30}]
    assert result["ledger"]["present"] is False


def test_missing_or_zero_turns_are_unknown_not_cheap(tmp_path):
    spend(tmp_path, "no_turn_log_game", 2)  # Calls, and no turn log for them.
    empty = tmp_path / f"{SAVE}_20260101T000000Z.jsonl"
    empty.write_text("")
    spend(tmp_path, empty.stem, 1)
    result = report(tmp_path)
    assert len(result["games"]) == 2
    for game in result["games"]:
        assert game["turns"] is None
        assert game["cost_per_turn_usd"] is None
        assert game["tokens_per_turn"] is None
        assert game["projected_cost_usd"] is None
    assert result["totals"]["games_with_known_turns"] == 0
    assert result["totals"]["cost_per_turn_known_games_usd"] is None
    assert any("no turn log" in sample for sample in result["problems"]["samples"])


def test_an_unknown_model_is_flagged_unpriced_and_not_free(tmp_path):
    game_id = session(tmp_path, "20260101T000000Z", range(1, 11)).stem
    spend(tmp_path, game_id, 3, model="claude-future-9")
    game = only_game(tmp_path)
    assert game["unpriced_calls"] == 3
    assert game["tokens"] == 33_000
    assert game["cost_usd"] == "0.000000"
    assert report(tmp_path)["totals"]["unpriced_calls"] == 3


def test_split_by_player_and_model(tmp_path):
    game_id = session(tmp_path, "20260101T000000Z", range(1, 11)).stem
    spend(tmp_path, game_id, 1, player="p1")
    spend(tmp_path, game_id, 2, player="p2")
    game = only_game(tmp_path)
    assert {k: v["cost_usd"] for k, v in game["players"].items()} == {
        "p1": "0.030000",
        "p2": "0.060000",
    }
    assert list(game["models"]) == [SONNET]
    assert game["players"]["p2"]["cost_per_turn_usd"] == "0.006000"


def test_a_call_that_never_closed_counts_at_its_worst_case(tmp_path):
    game_id = session(tmp_path, "20260101T000000Z", range(1, 11)).stem
    Ledger(tmp_path / LEDGER_NAME).reserve_within(
        Decimal(1),
        game_id=game_id,
        player_id="p1",
        tier="t",
        model=SONNET,
        worst_case_usd=Decimal("0.25"),
        turn=3,
    )
    game = only_game(tmp_path)
    assert (game["calls"], game["unsettled_calls"], game["unsettled_usd"]) == (1, 1, "0.250000")
    assert game["cost_per_turn_usd"] == "0.025000"


def test_corrupt_and_torn_lines_are_reported_and_the_rest_is_read(tmp_path):
    log = session(tmp_path, "20260101T000000Z", range(1, 4))
    with log.open("a") as file:
        file.write("not json\n")
        file.write('{"no_turn": 1}\n')
        file.write('{"turn": 4, "torn')
    spend(tmp_path, log.stem, 2)
    with (tmp_path / LEDGER_NAME).open("a") as file:
        file.write("garbage\n")
        file.write('{"event": "close", "call_id": "nope", "cost_usd": "1"}\n')
        file.write('{"event": "open", "call')
    result = report(tmp_path)
    assert result["games"][0]["turns"] == 3
    assert result["games"][0]["calls"] == 2
    assert result["problems"]["count"] == 6
    text = "\n".join(result["problems"]["samples"])
    assert "torn" in text and "no open" in text


def test_the_report_is_repeatable_and_ignores_the_other_logs_of_a_session(tmp_path):
    log = session(tmp_path, "20260101T000000Z", range(1, 6))
    for suffix in ("_events.jsonl", "_settle.jsonl", "_bot.log"):
        (tmp_path / f"{log.stem}{suffix}").write_text('{"turn": 1}\n' * 7)
    spend(tmp_path, log.stem, 1)
    first = report(tmp_path)
    assert first == report(tmp_path)
    assert (first["games"][0]["turns"], len(first["games"])) == (5, 1)


def test_ids_for_the_ledger_follow_the_same_rule(tmp_path):
    first = session(tmp_path, "20260101T000000Z", range(1, 6)).stem
    later = f"{SAVE}_20260101T010000Z"
    assert game_id_for_new_session(tmp_path, SAVE, 6, later) == first
    assert game_id_for_new_session(tmp_path, SAVE, 3, later) == later
    assert game_id_of_log(tmp_path, tmp_path / f"{first}.jsonl") == first
    assert game_id_of_log(tmp_path, tmp_path / "missing.jsonl") is None


def test_the_ledgers_own_cost_matches_the_recomputed_cost(tmp_path):
    game_id = session(tmp_path, "20260101T000000Z", range(1, 6)).stem
    spend(tmp_path, game_id, 2)
    game = only_game(tmp_path)
    assert (game["cost_mismatches"], game["calls_without_turn"]) == (0, 0)
    assert [e.turn for e in Ledger(tmp_path / LEDGER_NAME).entries] == [0, 1]  # Turns round-trip.


# The command


def run(tmp_path, capsys, *args, budget=None) -> tuple[int, str]:
    extra = []
    if budget is not None:
        file = tmp_path / "budget.json"
        file.write_text(json.dumps(budget))
        extra = ["--budget-file", str(file)]
    code = main(["--runs", str(tmp_path), *extra, *args])
    return code, capsys.readouterr().out


def test_the_text_report_shows_the_zero_game_and_the_projection(tmp_path, capsys):
    session(tmp_path, "20260101T000000Z", range(1, 31))
    code, out = run(tmp_path, capsys, "--full-game-turns", "50")
    assert code == 0
    assert "$0.00" in out and "50-turn game" in out
    assert "NOT FOUND" in out and "zero LLM calls and at least one turn: 1" in out


def test_json_output_parses(tmp_path, capsys):
    session(tmp_path, "20260101T000000Z", range(1, 6))
    code, out = run(tmp_path, capsys, "--json")
    assert code == 0 and json.loads(out)["games"][0]["turns"] == 5


def test_budget_lines_exist_only_with_a_budget_file_and_fail_when_over(tmp_path, capsys):
    game_id = session(tmp_path, "20260101T000000Z", range(1, 11)).stem
    spend(tmp_path, game_id, 3, player="p1")  # $0.09
    code, out = run(tmp_path, capsys, "--fail-over-budget")
    assert code == 0 and "Budget per player" not in out
    under = {"per_player_per_game_usd": "5"}
    code, out = run(tmp_path, capsys, "--fail-over-budget", budget=under)
    assert code == 0 and "p1: under" in out
    over = {"per_player_per_game_usd": "0.05"}
    code, out = run(tmp_path, capsys, budget=over)
    assert code == 0 and "p1: over" in out  # Over, but only the flag makes it a failure.
    assert run(tmp_path, capsys, "--fail-over-budget", budget=over)[0] == 1


def test_an_unreadable_budget_file_is_an_error_not_a_skip(tmp_path, capsys):
    session(tmp_path, "20260101T000000Z", range(1, 3))
    file = tmp_path / "budget.json"
    file.write_text('{"wrong": 1}')
    assert main(["--runs", str(tmp_path), "--budget-file", str(file)]) == 2
    assert "per_player_per_game_usd" in capsys.readouterr().err


def test_strict_fails_on_problems_and_unpriced_models(tmp_path, capsys):
    log = session(tmp_path, "20260101T000000Z", range(1, 4))
    assert run(tmp_path, capsys, "--strict")[0] == 0
    log.write_text(log.read_text() + "junk\n")
    assert run(tmp_path, capsys)[0] == 0
    assert run(tmp_path, capsys, "--strict")[0] == 1


def test_strict_fails_on_an_unpriced_model(tmp_path, capsys):
    game_id = session(tmp_path, "20260101T000000Z", range(1, 4)).stem
    spend(tmp_path, game_id, 1, model="claude-future-9")
    code, out = run(tmp_path, capsys, "--strict")
    assert code == 1 and "UNPRICED" in out


def test_history_accumulates_and_is_stable(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    history = tmp_path / "history.csv"
    old = session(runs, "20260101T000000Z", range(1, 6))
    assert run(runs, capsys, "--history", str(history))[0] == 0
    first = history.read_text()
    assert run(runs, capsys, "--history", str(history))[0] == 0
    assert history.read_text() == first
    old.unlink()
    session(runs, "20260102T000000Z", range(1, 4))
    assert run(runs, capsys, "--history", str(history))[0] == 0
    ids = [line.split(",")[0] for line in history.read_text().splitlines()[1:]]
    assert ids == [f"{SAVE}_20260101T000000Z", f"{SAVE}_20260102T000000Z"]


def test_history_refuses_to_overwrite_a_file_that_is_not_one(tmp_path, capsys):
    session(tmp_path, "20260101T000000Z", range(1, 3))
    other = tmp_path / "other.csv"
    other.write_text("a,b\n1,2\n")
    assert run(tmp_path, capsys, "--history", str(other))[0] == 2
    assert other.read_text() == "a,b\n1,2\n"


def test_no_runs_folder_is_an_empty_report(tmp_path, capsys):
    assert main(["--runs", str(tmp_path / "absent")]) == 0
    assert "no games found" in capsys.readouterr().out
