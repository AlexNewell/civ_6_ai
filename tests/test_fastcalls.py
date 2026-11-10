import asyncio

import pytest
from civ_mcp import connection, game_lifecycle

from civ6_ai import fastcalls


@pytest.fixture(autouse=True)
def restore(monkeypatch):
    monkeypatch.setattr(connection, "tuner_client", connection.tuner_client)
    monkeypatch.setattr(game_lifecycle, "dismiss_popup", game_lifecycle.dismiss_popup)


def test_idle_waits_are_cut_and_other_waits_are_not(monkeypatch):
    seen = []

    async def fake_drain(reader, timeout=0.5):
        seen.append(timeout)
        return []

    monkeypatch.setattr(connection.tuner_client, "drain_messages", fake_drain)
    fastcalls.shorten_call_waits()
    for asked in (0.1, 0.2, 0.3):
        asyncio.run(connection.tuner_client.drain_messages(None, timeout=asked))
    assert seen == [0.02, 0.05, 0.3]
    assert connection.tuner_client.TAG_COMMAND is not None  # other names still reach the module


def test_shortening_twice_does_not_stack():
    fastcalls.shorten_call_waits()
    first = connection.tuner_client
    fastcalls.shorten_call_waits()
    assert connection.tuner_client is first


def test_popup_scan_only_sees_states_that_can_hold_an_exclusive_popup(monkeypatch):
    seen = {}

    async def fake(conn):
        seen.update(conn.lua_states)
        return "ok"

    monkeypatch.setattr(game_lifecycle, "dismiss_popup", fake)
    fastcalls.narrow_popup_scan(check=False)

    class Conn:
        lua_states = {60: "InvitePopup", 61: "HistoricMoments", 62: "EraCompletePopup", 63: "Other"}

    conn = Conn()
    assert asyncio.run(game_lifecycle.dismiss_popup(conn)) == "ok"
    assert seen == {61: "HistoricMoments", 62: "EraCompletePopup"}
    assert len(conn.lua_states) == 4  # restored


def test_popup_scan_is_restored_when_the_scan_fails(monkeypatch):
    async def fake(conn):
        raise ValueError("boom")

    monkeypatch.setattr(game_lifecycle, "dismiss_popup", fake)
    fastcalls.narrow_popup_scan(check=False)

    class Conn:
        lua_states = {60: "InvitePopup"}

    conn = Conn()
    with pytest.raises(ValueError):
        asyncio.run(game_lifecycle.dismiss_popup(conn))
    assert conn.lua_states == {60: "InvitePopup"}


def test_pinned_civ6_mcp_still_has_the_code_the_patches_rely_on():
    # These raise RuntimeError if a civ6-mcp upgrade changed the code.
    fastcalls.narrow_popup_scan()
    fastcalls.shorten_call_waits()
