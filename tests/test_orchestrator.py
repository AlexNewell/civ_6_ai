import asyncio
import json
from dataclasses import dataclass

import pytest

from civ6_ai import orchestrator
from civ6_ai.orchestrator import (
    DoNothing,
    Orchestrator,
    StopReason,
    TurnLog,
    stop_reason_in_report,
)


@dataclass
class Overview:
    turn: int


class FakeGame:
    """Advances one turn per `end_turn`, unless a scripted result says otherwise."""

    def __init__(self, results=(), defeated_on_turn=None):
        self.turn = 1
        self.results = list(results)
        self.defeated_on_turn = defeated_on_turn
        self.calls = []

    async def check_game_over(self):
        return "defeat" if self.turn == self.defeated_on_turn else None

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
    ("report", "expected"),
    [
        ("Turn 5 -> 6", None),
        ("Cannot end turn — resolve these blockers:", None),
        ("HANG:5:save|End turn requested", StopReason.HUNG),
        ("GAME OVER — DEFEAT. Someone won.", StopReason.GAME_OVER),
        ("Turn 5 -> 6\nGAME OVER - DEFEAT.", StopReason.GAME_OVER),
        ("Turn 5 -> 6\n  HANG:5:save|stuck", StopReason.HUNG),
        ("Turn 5 -> 6\nThe GAME OVER sign", None),
        ("RUN ABORTED — save scumming threshold exceeded.", StopReason.RUN_ABORTED),
    ],
)
def test_stop_reason_in_report(report, expected):
    assert stop_reason_in_report(report) is expected


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


def test_defeat_during_the_ai_turn_stops_before_reading_the_game(tmp_path):
    class DefeatScreen(FakeGame):
        async def get_game_overview(self):
            if self.turn == self.defeated_on_turn:
                raise RuntimeError("Lua can't read the defeat screen")
            return await super().get_game_overview()

    game = DefeatScreen(defeated_on_turn=3)
    handlers = [Recording("handler")]
    reason, turn_log = play(game, handlers, 20, tmp_path)
    assert reason is StopReason.GAME_OVER
    assert [record.turn for record in turn_log.records] == [1, 2]
    assert game.calls.count("handler") == 2


@pytest.mark.parametrize(
    ("report", "expected"),
    [("HANG:5:save|stuck", StopReason.HUNG), ("RUN ABORTED — stop", StopReason.RUN_ABORTED)],
)
def test_terminal_report_stops_without_touching_the_game_again(tmp_path, report, expected):
    class StuckGame(FakeGame):
        async def check_game_over(self):
            if self.calls:
                raise AssertionError("queried a game that can't answer")
            return None

        async def get_game_overview(self):
            if self.calls:
                raise AssertionError("queried a game that can't answer")
            return await super().get_game_overview()

    reason, turn_log = play(StuckGame(results=[report]), [DoNothing()], 20, tmp_path)
    assert reason is expected
    assert turn_log.records[-1].stop_reason == expected


def test_normal_turns_keep_the_end_turn_report(tmp_path):
    _, turn_log = play(FakeGame(), [DoNothing()], 1, tmp_path)
    assert turn_log.records[0].detail == "Turn 1 -> 2"


def test_game_over_stops_without_reading_the_finished_game(tmp_path):
    class FinishedGame(FakeGame):
        async def get_game_overview(self):
            if self.calls:
                raise AssertionError("read a finished game")
            return await super().get_game_overview()

    game = FinishedGame(results=["GAME OVER — VICTORY! You won."])
    reason, _ = play(game, [DoNothing()], 20, tmp_path)
    assert reason is StopReason.GAME_OVER


class PopupHeldGame(FakeGame):
    """The turn ends, but the UI keeps reading the old turn until a popup is dismissed.

    Seen live on turn 1: the report said "Turn 1 -> 2" while the game screen showed a natural
    disaster popup and the UI context still answered turn 1.
    """

    def __init__(self):
        super().__init__()
        self.held = False

    async def get_game_overview(self):
        return Overview(self.turn - 1 if self.held else self.turn)

    async def end_turn(self):
        result = await super().end_turn()
        self.held = True
        return result

    async def dismiss_popup(self):
        self.calls.append("dismiss_popup")
        self.held = False
        return "Dismissed: NaturalDisasterPopup"


def test_popup_holding_the_turn_is_dismissed_instead_of_stopping(tmp_path):
    game = PopupHeldGame()
    reason, turn_log = play(game, [DoNothing()], 2, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert game.calls.count("dismiss_popup") == 2
    assert [record.stop_reason for record in turn_log.records] == [None, None]


@pytest.fixture(autouse=True)
def no_read_delay(monkeypatch):
    monkeypatch.setattr(orchestrator, "TURN_READ_RETRY_SECONDS", 0.0)


class SlowUiGame(FakeGame):
    """The turn ends but the UI reads the old turn for a few looks, with no popup to dismiss."""

    def __init__(self, stale_reads):
        super().__init__()
        self.stale_reads = stale_reads

    async def end_turn(self):
        result = await super().end_turn()
        self.remaining = self.stale_reads
        return result

    async def get_game_overview(self):
        if getattr(self, "remaining", 0) > 0:
            self.remaining -= 1
            return Overview(self.turn - 1)
        return Overview(self.turn)


def test_a_slow_ui_that_catches_up_is_not_a_block(tmp_path):
    # Run 4, turn 318: the report said "318 -> 319", one look read 318, and the run stopped.
    reason, turn_log = play(SlowUiGame(stale_reads=3), [DoNothing()], 2, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert [record.stop_reason for record in turn_log.records] == [None, None]


def test_a_ui_that_never_catches_up_is_still_a_block(tmp_path):
    reason, _ = play(SlowUiGame(stale_reads=99), [DoNothing()], 2, tmp_path)
    assert reason is StopReason.BLOCKED
