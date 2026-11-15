"""Loading a save and writing checkpoints with no one at the keyboard (issue 17)."""

import asyncio
import sys
from pathlib import Path

import pytest

from civ6_ai.orchestrator import StopReason
from civ6_ai.runner import loader
from civ6_ai.runner.loader import LoadError, build_load_lua, checkpoint_name, load_save

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class FakeConnection:
    """A tuner that goes away while the save loads and comes back after `down_for` seconds."""

    def __init__(self, world):
        self.world = world
        self.ingame_index = None

    async def connect(self):
        w = self.world
        if w.loading_since is not None and w.clock() - w.loading_since < w.down_for:
            raise ConnectionError("loading")
        if w.needs_escape and not w.escaped and w.loading_since is not None:
            raise ConnectionError("waiting on the leader screen")
        self.ingame_index = 119

    async def disconnect(self):
        pass

    async def execute_in_state(self, state, code):
        w = self.world
        self.world.sent.append(code)
        if "Network.LoadGame" in code:
            return ["QUERY_SENT"]
        if "Civ6AiLoad" in code:
            if not w.found:
                return ["NOT_FOUND"]
            w.loading_since = w.clock()
            return ["FOUND"]
        if "GetCurrentGameTurn" in code:
            return [f"TURN|{w.turn}"]
        return []


class World:
    def __init__(self, found=True, down_for=24, needs_escape=False, turn=1):
        self.clock = FakeClock()
        self.found = found
        self.down_for = down_for
        self.needs_escape = needs_escape
        self.escaped = False
        self.loading_since = None
        self.turn = turn
        self.sent: list[str] = []

    def factory(self):
        return FakeConnection(self)

    def escape(self):
        self.escaped = True
        return True

    def load(self, name="STD_T1_01", **kw):
        return asyncio.run(
            load_save(
                name,
                factory=self.factory,
                escape=self.escape,
                sleep=self.clock.sleep,
                clock=self.clock,
                **kw,
            )
        )


def test_the_load_turns_on_auto_start_and_leaves_the_open_game():
    lua = build_load_lua("STD_T1_01", in_game=True)
    assert "Automation.SetAutoStartEnabled(true)" in lua
    assert lua.index("Network.LeaveGame()") < lua.index("Network.LoadGame")
    assert "Network.LeaveGame()" not in build_load_lua("STD_T1_01", in_game=False)


def test_a_save_name_cannot_break_out_of_the_lua_string():
    with pytest.raises(ValueError):
        build_load_lua('x" end os.exit() --', in_game=True)


def test_a_load_returns_the_turn_once_the_game_is_back():
    world = World(turn=100)
    loaded = world.load("CKPT_STD_T1_01_T100")
    assert loaded.turn == 100 and not loaded.escape_pressed
    assert 24 <= loaded.seconds < 40


def test_escape_is_pressed_only_when_the_leader_screen_waits():
    world = World(needs_escape=True)
    loaded = world.load()
    assert loaded.escape_pressed
    assert loaded.seconds >= loader.ESCAPE_AFTER_SECONDS


def test_a_missing_save_is_an_error_not_a_hang():
    with pytest.raises(LoadError, match="no save named"):
        World(found=False).load("NO_SUCH_SAVE")


def test_a_game_that_never_comes_back_times_out():
    with pytest.raises(LoadError, match="did not come back"):
        World(down_for=10_000).load(timeout=120)


def test_checkpoint_names_sort_by_turn():
    names = [checkpoint_name("STD_T1_01", t) for t in (100, 50, 150)]
    assert sorted(names) == ["CKPT_STD_T1_01_T050", "CKPT_STD_T1_01_T100", "CKPT_STD_T1_01_T150"]


def test_play_with_checkpoints_saves_at_the_start_of_each_turn_asked_for():
    import run_game

    state = {"turn": 45}
    saved: list[int] = []
    chunks: list[int] = []

    async def play(n):
        chunks.append(n)
        state["turn"] += n
        return StopReason.TURN_LIMIT

    async def turn_now():
        return state["turn"]

    async def save(turn):
        saved.append(turn)
        return True

    reason = asyncio.run(run_game.play_with_checkpoints(play, turn_now, save, 60, [50, 100, 30]))
    assert reason == StopReason.TURN_LIMIT
    assert saved == [50, 100]
    assert chunks == [5, 50, 5] and sum(chunks) == 60


def test_play_with_checkpoints_stops_when_the_game_does():
    import run_game

    async def play(n):
        return StopReason.GAME_OVER

    async def turn_now():
        return 10

    async def save(turn):
        return True

    reason = asyncio.run(run_game.play_with_checkpoints(play, turn_now, save, 50, [20]))
    assert reason == StopReason.GAME_OVER
