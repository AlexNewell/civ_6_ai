import asyncio
import json

import pytest
from civ_mcp.connection import LuaError

import check_saves


class FakeConnection:
    """Replays Lua status replies. An exception in the list is raised instead of returned."""

    def __init__(self, replies):
        self.replies = iter(replies)

    async def execute_in_state(self, state, code, timeout):
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


def wait(replies):
    asyncio.run(check_saves.wait_for_teardown(FakeConnection(replies), state=1))


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(check_saves, "STATUS_POLL_SECONDS", 0)
    monkeypatch.setattr(check_saves, "STATUS_CHECKS", 3)


@pytest.mark.parametrize("gone", [LuaError("ERR:Invalid Lua State"), ConnectionError()])
def test_load_counts_as_started_once_the_lua_state_is_gone(gone):
    wait([["FOUND"], gone])


def test_missing_save_is_reported():
    with pytest.raises(FileNotFoundError):
        wait([["PENDING"], ["NOT_FOUND"]])


@pytest.mark.parametrize(
    "replies",
    [
        [["PENDING"]] * 3,
        [["FOUND"]] * 3,
        [TimeoutError()] * 3,
    ],
)
def test_load_that_never_tears_down_the_game_fails(replies):
    with pytest.raises(TimeoutError):
        wait(replies)


def test_parse_info_reads_only_tagged_lines():
    lines = ["noise", "SAVEINFO|turn|1", "SAVEINFO|map_script|a|b.lua"]
    assert check_saves.parse_info(lines) == {"turn": "1", "map_script": "a|b.lua"}


def test_find_differences_reports_changed_and_missing_values():
    expected = {"map_size": "MAPSIZE_SMALL", "game_speed": "GAMESPEED_QUICK"}
    actual = {"map_size": "MAPSIZE_LARGE"}
    assert len(check_saves.find_differences(expected, actual)) == 2
    assert check_saves.find_differences(expected, expected) == []


def test_manifest_has_at_least_three_distinct_recorded_saves():
    saves = json.loads(check_saves.MANIFEST.read_text(encoding="utf-8"))["saves"]
    assert len(saves) >= 3
    assert all(save["name"].startswith("STD_T1_") for save in saves)
    assert len({save["recorded"]["map_seed"] for save in saves}) == len(saves)
    assert len({save["recorded"]["game_seed"] for save in saves}) == len(saves)
