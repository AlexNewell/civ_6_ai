import asyncio
import json
from dataclasses import dataclass

import pytest

from civ6_ai.orchestrator import DoNothing, Orchestrator, StopReason, TurnLog, classify_end_turn


@dataclass
class Overview:
    turn: int


class FakeGame:
    """Advances one turn per `end_turn`, unless a scripted result says otherwise."""

    def __init__(self, results=()):
        self.turn = 1
        self.results = list(results)
        self.calls = []

    async def get_game_overview(self):
        return Overview(self.turn)

    async def end_turn(self):
        self.calls.append("end_turn")
        if self.results:
            return self.results.pop(0)
        self.turn += 1
        return f"Turn {self.turn - 1} -> {self.turn}"


class Recording:
    def __init__(self, name, error=None):
        self.name = name
        self.error = error

    async def run(self, game):
        game.calls.append(self.name)
        if self.error:
            raise self.error


def play(game, handlers, turns, tmp_path):
    turn_log = TurnLog(tmp_path / "runs" / "game.jsonl")
    reason = asyncio.run(Orchestrator(game, handlers, turn_log).play(turns))
    return reason, turn_log


@pytest.mark.parametrize(
    ("result", "after", "expected"),
    [
        ("Turn 5 -> 6", 6, None),
        ("Cannot end turn — resolve these blockers:", 5, StopReason.BLOCKED),
        ("End turn blocked (turn 5): diplomacy", 5, StopReason.BLOCKED),
        ("Turn 5 -> 6", 5, StopReason.BLOCKED),
        ("HANG:5:save|End turn requested", 5, StopReason.HUNG),
        ("GAME OVER — DEFEAT. Someone won.", 5, StopReason.GAME_OVER),
        ("RUN ABORTED — save scumming threshold exceeded.", 5, StopReason.RUN_ABORTED),
    ],
)
def test_classify_end_turn(result, after, expected):
    assert classify_end_turn(result, 5, after) is expected


def test_plays_requested_turns_and_logs_each_with_timing(tmp_path):
    game = FakeGame()
    reason, turn_log = play(game, [DoNothing()], 20, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert game.turn == 21
    lines = [json.loads(line) for line in turn_log.path.read_text().splitlines()]
    assert [line["turn"] for line in lines] == list(range(1, 21))
    for line in lines:
        assert line["total_seconds"] >= line["end_turn_seconds"] >= 0
        assert line["handler_seconds"]["do_nothing"] >= 0


def test_handlers_run_in_order_before_the_turn_ends(tmp_path):
    game = FakeGame()
    play(game, [Recording("first"), Recording("second")], 1, tmp_path)
    assert game.calls == ["first", "second", "end_turn"]


def test_failing_handler_is_logged_and_the_turn_still_ends(tmp_path):
    game = FakeGame()
    handlers = [Recording("broken", ValueError("bad policy")), Recording("next")]
    reason, turn_log = play(game, handlers, 1, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert game.calls == ["broken", "next", "end_turn"]
    assert "bad policy" in turn_log.records[0].handler_errors["broken"]


def test_lost_connection_stops_the_run(tmp_path):
    with pytest.raises(ConnectionError):
        play(FakeGame(), [Recording("broken", ConnectionError())], 1, tmp_path)


def test_blocked_turn_stops_without_retrying(tmp_path):
    game = FakeGame(results=["Cannot end turn — resolve these blockers:"])
    reason, turn_log = play(game, [DoNothing()], 20, tmp_path)
    assert reason is StopReason.BLOCKED
    assert game.calls == ["end_turn"]
    assert "Cannot end turn" in turn_log.records[-1].detail


def test_game_over_stops_without_reading_the_finished_game(tmp_path):
    class FinishedGame(FakeGame):
        async def get_game_overview(self):
            if self.calls:
                raise AssertionError("read a finished game")
            return await super().get_game_overview()

    game = FinishedGame(results=["GAME OVER — VICTORY! You won."])
    reason, _ = play(game, [DoNothing()], 20, tmp_path)
    assert reason is StopReason.GAME_OVER
