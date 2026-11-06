import argparse
import asyncio

import pytest

import spike_hold_turn
from spike_hold_turn import (
    Event,
    Spike,
    fill_hooks,
    first_of_player,
    moves_by_player,
    parse_event,
    parse_status,
)


def line(seq: int, name: str, args: str) -> str:
    return f"S1LOG|{seq}|10.5|GameCore|{name}|{args}|26|1|3/0/1/2/3/4"


def test_parse_event_reads_all_fields():
    event = parse_event(line(3, "Events.UnitMoveComplete", "1,5,6,7"))
    expected = Event(
        3, 10.5, "GameCore", "Events.UnitMoveComplete", "1,5,6,7", "26", "1", "3/0/1/2/3/4"
    )
    assert event == expected


@pytest.mark.parametrize("text", ["HOOK|x", "S1LOG|1|2", ""])
def test_parse_event_ignores_other_lines(text):
    assert parse_event(text) is None


def test_first_of_player_keeps_first_per_context_and_name():
    events = [
        parse_event(line(1, "A", "1")),
        parse_event(line(2, "A", "1")),
        parse_event(line(3, "A", "2")),
        parse_event(line(4, "B", "1,true")),
    ]
    assert [event.seq for event in first_of_player(events, 1)] == [1, 4]


def test_moves_by_player_counts_move_events_only():
    events = [
        parse_event(line(1, "Events.UnitMoved", "1,5")),
        parse_event(line(2, "Events.UnitMoveComplete", "1,5")),
        parse_event(line(3, "Events.UnitMoveComplete", "2,5")),
        parse_event(line(4, "Events.TurnEnd", "1")),
    ]
    assert moves_by_player(events) == {"1": 2, "2": 1}


def test_fill_hooks_substitutes_context_and_target():
    assert fill_hooks('"__CONTEXT__" __TARGET__', "InGame", 4) == '"InGame" 4'


@pytest.mark.parametrize(
    "context,target", [("Nope", 1), ("InGame", "1; os.exit()"), ("InGame", True)]
)
def test_fill_hooks_rejects_bad_input(context, target):
    with pytest.raises(ValueError):
        fill_hooks("x", context, target)


def test_parse_status():
    assert parse_status(["S1STAT|turn|5", "junk", "S1STAT|seed|-1"]) == {"turn": "5", "seed": "-1"}


class FakeConnection:
    gamecore_index = 3
    ingame_index = 119

    def __init__(self, ingame_index=119):
        self.ingame_index = ingame_index
        self.calls = []

    async def execute_in_state(self, state, code, timeout):
        self.calls.append((state, code))
        return []


def test_remove_does_nothing_without_a_game():
    connection = FakeConnection(ingame_index=None)
    asyncio.run(Spike(connection).remove())
    assert connection.calls == []


def test_remove_runs_in_ingame_when_a_game_is_open():
    connection = FakeConnection()
    asyncio.run(Spike(connection).remove())
    assert [state for state, _ in connection.calls] == [119]


def test_scheduled_release_uses_the_holding_context(monkeypatch):
    connection = FakeConnection()
    spike = Spike(connection)
    clock = {"now": 0.0}

    def fake_time():
        clock["now"] += 1.0
        return clock["now"]

    async def no_sleep(_):
        return None

    polls = iter(
        [
            {"hold": "held", "turn": "5", "local_active": "false"},
            {"hold": "held", "turn": "5", "local_active": "false"},
            {"hold": "held", "turn": "5", "local_active": "false"},
            {"hold": "released", "turn": "6", "local_active": "true"},
        ]
    )

    async def fake_poll(_spike, context):
        return next(polls)

    async def fake_status():
        return {"turn": "5"}

    monkeypatch.setattr(spike_hold_turn.time, "time", fake_time)
    monkeypatch.setattr(spike_hold_turn.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(spike_hold_turn, "poll_once", fake_poll)
    monkeypatch.setattr(spike, "status", fake_status)
    args = argparse.Namespace(
        poll=["GameCore"], hold_context="InGame", release_after=2, timeout=100
    )
    asyncio.run(spike_hold_turn.run_turn(spike, args))
    released_in = [state for state, code in connection.calls if "hold.release = true" in code]
    assert released_in == [119]
