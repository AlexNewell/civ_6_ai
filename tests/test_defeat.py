import asyncio
from dataclasses import dataclass

import pytest

from civ6_ai import defeat
from civ6_ai.defeat import PlayerStatus, is_defeated, parse_status
from civ6_ai.orchestrator import DoNothing, Orchestrator, StopReason, TurnLog


def line(alive="false", win=-1, team=0, units=0, cities=0, screen="unknown"):
    return f"DEFEAT_STATE|{alive}|{win}|{team}|{units}|{cities}|{screen}\n---END---"


class LuaGame:
    """Answers `execute_lua` per context, recording what it was asked."""

    def __init__(self, ingame=None, gamecore=None):
        self.answers = {"ingame": ingame, "gamecore": gamecore}
        self.asked = []

    async def execute_lua(self, code, context="gamecore"):
        self.asked.append((context, code))
        answer = self.answers[context]
        if isinstance(answer, Exception):
            raise answer
        return answer or "(no output)"


def check(game):
    return asyncio.run(is_defeated(game))


def test_eliminated_with_nothing_left_is_defeated():
    assert check(LuaGame(ingame=line(alive="false", units=0, cities=0)))


def test_eliminated_with_the_screen_up_is_defeated_even_with_units():
    assert check(LuaGame(ingame=line(alive="false", units=2, screen="shown")))


def test_not_alive_alone_is_not_trusted_while_units_remain():
    assert not check(LuaGame(ingame=line(alive="false", units=1, cities=0, screen="hidden")))
    assert not check(LuaGame(ingame=line(alive="false", units=1, cities=0, screen="unknown")))


def test_alive_player_is_not_defeated():
    assert not check(LuaGame(ingame=line(alive="true", units=0, cities=0, screen="hidden")))


def test_someone_winning_ends_the_game_for_us():
    assert check(LuaGame(ingame=line(alive="true", win=3, units=4, cities=2)))


def test_falls_back_to_gamecore_when_ingame_fails_or_is_unreadable():
    assert check(LuaGame(ingame=RuntimeError("nil compare"), gamecore=line()))
    assert check(LuaGame(ingame=None, gamecore=line()))
    assert check(LuaGame(ingame=line(alive="?"), gamecore=line()))


def test_unreadable_everywhere_is_not_defeated():
    assert not check(LuaGame(ingame=RuntimeError("x"), gamecore=RuntimeError("y")))


def test_connection_loss_is_not_swallowed():
    with pytest.raises(ConnectionError):
        check(LuaGame(ingame=ConnectionError("gone")))


def test_only_fixed_lua_is_sent():
    game = LuaGame(ingame=line())
    check(game)
    assert {code for _, code in game.asked} == {defeat._STATUS_LUA}


def test_status_lua_ends_with_the_sentinel():
    assert defeat._STATUS_LUA.rstrip().endswith('print("---END---")')


def test_parse_rejects_malformed_lines():
    assert parse_status("DEFEAT_STATE|true|x|0|0|0|shown") is None
    assert parse_status("DEFEAT_STATE|true|-1|0|0") is None
    assert parse_status("nothing here") is None
    assert parse_status(line(alive="true", screen="shown")) == PlayerStatus(True, -1, 0, 0, 0, True)


@dataclass
class Overview:
    turn: int


class StuckGame:
    """A game whose turn never advances, with a scripted end_turn report."""

    def __init__(self, report="HANG:39:save|End turn requested", raises=None):
        self.report = report
        self.raises = raises

    async def check_game_over(self):
        return None  # Blind with no city, as at the pin.

    async def get_game_overview(self):
        return Overview(39)

    async def end_turn(self):
        if self.raises:
            raise self.raises
        return self.report


def run(game, tmp_path, defeated=None):
    async def check_defeat():
        return defeated

    turn_log = TurnLog(tmp_path / "game.jsonl")
    orchestrator = Orchestrator(game, [DoNothing()], turn_log, defeat_check=check_defeat)
    return asyncio.run(orchestrator.play(3)), turn_log


def test_hang_with_a_defeated_player_is_game_over(tmp_path):
    reason, log = run(StuckGame(), tmp_path, defeated=True)
    assert reason is StopReason.GAME_OVER
    assert log.records[0].stop_reason == "game_over"


def test_a_real_hang_stays_hung(tmp_path):
    reason, _ = run(StuckGame(), tmp_path, defeated=False)
    assert reason is StopReason.HUNG


def test_a_turn_that_does_not_advance_is_game_over_when_defeated(tmp_path):
    game = StuckGame(report="Turn 39 -> 40")  # Says it moved, but the turn is still 39.
    assert run(game, tmp_path, defeated=True)[0] is StopReason.GAME_OVER
    assert run(game, tmp_path, defeated=False)[0] is StopReason.BLOCKED


def test_an_end_turn_that_raises_is_game_over_when_defeated(tmp_path):
    game = StuckGame(raises=RuntimeError("no player"))
    assert run(game, tmp_path, defeated=True)[0] is StopReason.GAME_OVER
    with pytest.raises(RuntimeError):
        run(game, tmp_path, defeated=False)


def test_prefixed_game_over_report_is_game_over(tmp_path):
    game = StuckGame(report="Turn 39 -> 40\nGAME OVER - DEFEAT.")
    assert run(game, tmp_path, defeated=False)[0] is StopReason.GAME_OVER


def test_no_defeat_check_keeps_the_old_behaviour(tmp_path):
    turn_log = TurnLog(tmp_path / "game.jsonl")
    reason = asyncio.run(Orchestrator(StuckGame(), [DoNothing()], turn_log).play(3))
    assert reason is StopReason.HUNG


def test_a_failing_defeat_check_does_not_hide_the_stop(tmp_path):
    async def broken():
        raise RuntimeError("lua")

    turn_log = TurnLog(tmp_path / "game.jsonl")
    orchestrator = Orchestrator(StuckGame(), [DoNothing()], turn_log, defeat_check=broken)
    assert asyncio.run(orchestrator.play(3)) is StopReason.HUNG


def test_status_with_no_winning_team_reads_as_minus_one():
    # The Lua now prints -1 where the game returned nil (no winner yet), as seen live.
    status = parse_status("DEFEAT_STATE|true|-1|0|1|1|hidden")
    assert status is not None
    assert not status.is_game_over
