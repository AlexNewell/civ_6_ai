import pytest

from spike_hold_turn import (
    Event,
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
