import pytest
from civ_mcp import lua as lq
from civ_mcp.lua import cities as mcp_cities

from civ6_ai import city_focus


def test_favoring_food_follows_the_city_panel_flags():
    lua = city_focus.build_set_yield_focus(65537, "food")
    # CityPanel.lua: PARAM_FLAGS 0 is "favored", 1 is "ignored", PARAM_DATA0 is on/off.
    assert "send(0, want.Index, 1)" in lua
    assert "send(1, yr.Index, 0)" in lua  # An ignored yield is un-ignored first.
    assert 'GameInfo.Yields["YIELD_FOOD"]' in lua
    assert "PARAM_DATA0" in lua
    assert "---END---" in lua


def test_default_clears_every_favored_and_ignored_yield_and_favors_none():
    lua = city_focus.build_set_yield_focus(1, "DEFAULT")
    assert "local want = nil" in lua
    assert "send(0, yr.Index, 0)" in lua
    assert "if want ~= nil and not citz:IsFavoredYield" in lua  # Never favors when want is nil.


def test_a_bad_yield_name_is_not_written_into_the_lua():
    with pytest.raises(ValueError):
        city_focus.build_set_yield_focus(1, 'food"] os.exit() --')


def test_fix_replaces_the_builder_game_state_calls(monkeypatch):
    monkeypatch.setattr(lq, "build_set_yield_focus", mcp_cities.build_set_yield_focus)
    city_focus.fix_set_yield_focus()
    assert lq.build_set_yield_focus is city_focus.build_set_yield_focus


def test_fix_raises_if_civ6_mcp_changed(monkeypatch):
    monkeypatch.delattr(lq, "build_set_yield_focus")
    with pytest.raises(RuntimeError):
        city_focus.fix_set_yield_focus()
