import asyncio

from tests.test_orchestrator import FakeGame, Recording

from civ6_ai.orchestrator import Orchestrator, StopReason, TurnLog
from civ6_ai.screenshots import MAX_PER_GAME, RESERVED_FOR_STOPS, Screenshotter


class FakeCapture:
    def __init__(self, data=b"jpeg", error=None):
        self.data = data
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.data


def shooter(tmp_path, capture=None, game_id="STD_T1_01", cap=MAX_PER_GAME):
    return Screenshotter(tmp_path / "shots", game_id, cap=cap, capture=capture or FakeCapture())


def test_saves_a_named_file(tmp_path):
    path = shooter(tmp_path).take(7, "blocked")
    assert path is not None
    assert path.name == "STD_T1_01_T007_blocked.jpg"
    assert path.read_bytes() == b"jpeg"


def test_one_shot_per_turn(tmp_path):
    s = shooter(tmp_path)
    assert s.take(7, "handler-x") is not None
    assert s.take(7, "blocked", stop=True) is None
    assert s.take(8, "blocked", stop=True) is not None


def test_stops_at_the_cap_and_keeps_the_last_slots_for_stops(tmp_path):
    s = shooter(tmp_path)
    ordinary = MAX_PER_GAME - RESERVED_FOR_STOPS
    for turn in range(ordinary):
        assert s.take(turn, "handler-x") is not None
    assert s.take(ordinary, "handler-x") is None  # Only the reserved slots are left.
    for turn in range(ordinary, MAX_PER_GAME):
        assert s.take(turn, "hung", stop=True) is not None
    assert s.take(MAX_PER_GAME, "hung", stop=True) is None
    assert len(list((tmp_path / "shots").glob("*.jpg"))) == MAX_PER_GAME


def test_the_count_carries_over_to_a_new_session_of_the_same_game(tmp_path):
    for turn in range(5):
        shooter(tmp_path).take(turn, "blocked", stop=True)
    assert shooter(tmp_path).count() == 5
    assert shooter(tmp_path, game_id="OTHER_GAME").count() == 0


def test_a_failed_or_empty_capture_returns_none_without_raising(tmp_path):
    assert shooter(tmp_path, FakeCapture(error=RuntimeError("no window"))).take(1, "x") is None
    assert shooter(tmp_path, FakeCapture(data=None)).take(1, "x") is None
    assert not list((tmp_path / "shots").glob("*.jpg"))


def test_a_failed_capture_does_not_use_up_the_turn(tmp_path):
    capture = FakeCapture(data=None)
    s = shooter(tmp_path, capture)
    assert s.take(1, "x") is None
    capture.data = b"jpeg"
    assert s.take(1, "x") is not None


def test_unsafe_characters_are_kept_out_of_the_file_name(tmp_path):
    path = shooter(tmp_path).take(1, "handler-a/b:c")
    assert path is not None
    assert "/" not in path.name and ":" not in path.name


def play(game, handlers, turns, tmp_path, s):
    turn_log = TurnLog(tmp_path / "runs" / "game.jsonl")
    reason = asyncio.run(Orchestrator(game, handlers, turn_log, screenshots=s).play(turns))
    return reason, turn_log


def test_a_stopped_run_takes_one_screenshot_and_logs_it(tmp_path):
    game = FakeGame(results=["HANG:1:save|End turn requested"])
    capture = FakeCapture()
    reason, turn_log = play(game, [], 3, tmp_path, shooter(tmp_path, capture))
    assert reason is StopReason.HUNG
    assert capture.calls == 1
    assert "[screenshot: STD_T1_01_T001_hung.jpg]" in turn_log.records[-1].detail


def test_a_handler_failing_every_turn_is_shot_once(tmp_path):
    game = FakeGame()
    handlers = [Recording("bad", error=RuntimeError("boom"))]
    capture = FakeCapture()
    play(game, handlers, 4, tmp_path, shooter(tmp_path, capture))
    assert capture.calls == 1


def test_clean_turns_take_no_screenshot(tmp_path):
    capture = FakeCapture()
    play(FakeGame(), [Recording("ok")], 3, tmp_path, shooter(tmp_path, capture))
    assert capture.calls == 0


def test_no_screenshotter_changes_nothing(tmp_path):
    game = FakeGame(results=["HANG:1:save|End turn requested"])
    turn_log = TurnLog(tmp_path / "runs" / "game.jsonl")
    assert asyncio.run(Orchestrator(game, [], turn_log).play(3)) is StopReason.HUNG
    assert "screenshot" not in turn_log.records[-1].detail
