"""The watcher that clears a held turn while civ6-mcp's end_turn waits."""

import asyncio
from dataclasses import dataclass

import pytest

from civ6_ai import blockers
from civ6_ai.blockers import BlockerResolver, BlockerType, ResolvingGame

ADVANCED = "Turn 5 -> 6"
TIMED_OUT = "HANG:5:save|End turn requested (turn is still 5)."


@dataclass
class Overview:
    turn: int


class HeldGame:
    """A game whose end_turn waits until a new end-turn request is sent, as a held turn does."""

    def __init__(self, blocking=(), can_end=True, popup=False, advances_alone=False, session=False):
        self.session = session
        self.turn = 5
        self.blocking = list(blocking)
        self.can_end = can_end
        self.popup = popup
        self.advances_alone = advances_alone
        self.calls = []
        self.released = None

    async def get_game_overview(self):
        return Overview(self.turn)

    async def end_turn(self):
        self.calls.append("end_turn")
        self.released = asyncio.Event()
        if self.advances_alone:
            self.turn += 1  # The AI turn is done, and end_turn is still writing its report.
            await asyncio.sleep(0.05)
            return ADVANCED
        else:
            try:
                await asyncio.wait_for(self.released.wait(), 0.5)
            except TimeoutError:
                return TIMED_OUT
        self.turn += 1
        return ADVANCED

    async def dismiss_popup(self):
        if self.popup:
            self.popup = False
            self.released.set()  # The popup was holding the AI turn, which now carries on.
            return "Dismissed: WorldCongressPopup"
        return "No popups to dismiss."

    async def get_world_congress(self):
        return Congress(self.session)

    async def queue_wc_votes(self, votes):
        self.calls.append("register voter")
        return "OK:QUEUED"

    async def skip_remaining_units(self):
        self.calls.append("skip")
        self.blocking.remove(BlockerType.UNITS)
        return "OK:SKIPPED|1 units"

    async def execute_lua(self, code, context="gamecore"):
        if "UI.CanEndTurn" in code:
            return f"CAN_END_TURN|{str(self.can_end).lower()}"
        if "WORLD_CONGRESS_SUBMIT_TURN" in code:
            self.calls.append("submit")
            self.session = False
            return "OK"
        if "ACTION_ENDTURN" in code:
            self.calls.append("ask again")
            self.released.set()
            return "OK:TURN_ENDED"
        return "\n".join(f"BLOCKING|{found}|message" for found in self.blocking) or "NONE"


@dataclass
class Congress:
    is_in_session: bool
    resolutions: tuple = ()
    turns_until_next: int = 20


@pytest.fixture(autouse=True)
def quick_checks(monkeypatch):
    monkeypatch.setattr(blockers, "WATCH_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(blockers, "WATCH_EVERY_SECONDS", 0.01)


def end_turn(game):
    return asyncio.run(ResolvingGame(game, BlockerResolver()).end_turn())


def test_a_unit_left_without_orders_is_cleared_and_the_turn_asked_again():
    game = HeldGame(blocking=[BlockerType.UNITS])
    assert end_turn(game) == ADVANCED
    assert game.calls == ["end_turn", "skip", "ask again"]


def test_a_popup_holding_the_turn_is_closed_without_a_new_request():
    game = HeldGame(popup=True)
    assert end_turn(game) == ADVANCED
    assert game.calls == ["end_turn"]


def test_no_new_request_while_the_turn_cannot_be_ended():
    # The AI is mid-turn, so a new request would be a duplicate that can skip a turn.
    game = HeldGame(blocking=[BlockerType.UNITS], can_end=False)
    assert end_turn(game) == TIMED_OUT
    assert "ask again" not in game.calls


def test_an_open_session_the_game_does_not_list_is_submitted_and_the_turn_asked_again():
    # Run 3, turn 156: the voter was registered after the session opened, so it never fired,
    # and the game's blocking list stayed empty while the session held the turn.
    game = HeldGame(session=True)
    assert end_turn(game) == ADVANCED
    assert game.calls == ["end_turn", "register voter", "submit", "ask again"]


def test_blockers_seen_once_as_the_turn_changes_are_left_alone():
    # Run 3, turn 199: the turn still read as 199 while turn 200's blockers showed. Clearing
    # them and asking again ended turn 200 before the bot played it.
    class Changing(HeldGame):
        async def execute_lua(self, code, context="gamecore"):
            result = await super().execute_lua(code, context)
            if "BLOCKING" in result:
                self.turn += 1  # The new turn arrives just after the blockers were read.
            return result

    game = Changing(blocking=[BlockerType.UNITS])
    assert end_turn(game) == TIMED_OUT
    assert "skip" not in game.calls and "ask again" not in game.calls


def test_nothing_to_clear_means_no_new_request():
    game = HeldGame()
    assert end_turn(game) == TIMED_OUT
    assert game.calls == ["end_turn"]


def test_a_turn_that_advances_by_itself_is_left_alone():
    game = HeldGame(blocking=[BlockerType.UNITS], advances_alone=True)
    assert end_turn(game) == ADVANCED
    assert "ask again" not in game.calls


def test_a_lost_connection_while_watching_stops_the_wait():
    class Lost(HeldGame):
        async def dismiss_popup(self):
            raise ConnectionError("gone")

    with pytest.raises(ConnectionError):
        end_turn(Lost(popup=True))
